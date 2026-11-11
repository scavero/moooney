"""Acciones de la web: las mismas operaciones que la CLI, sin pasar por `rich`.

Cada función recibe su propia conexión (los trabajos corren en hilos) y un
`log` al que escribe líneas legibles. Nada aquí imprime por pantalla.
"""

import sqlite3
from datetime import datetime
from pathlib import Path

import yaml

from moooney.analytics import alerts as alerts_mod
from moooney.analytics import budgets as budgets_mod
from moooney.analytics import periods
from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.categorize.keys import backfill_clean_keys, rebuild_clean_keys
from moooney.categorize.pipeline import (
    apply_decider,
    apply_embeddings,
    apply_rules,
    build_questions,
    pending_jobs,
    refresh_rules,
)
from moooney.categorize.rules import load_rules
from moooney.categorize.taxonomy import Taxonomy, load_taxonomy
from moooney.config import Settings
from moooney.db import repo
from moooney.enrich.providers import build_providers, nominatim_url
from moooney.enrich.runner import run_enrichment, select_candidates
from moooney.enrich.settings import EnrichmentSettings, load_enrichment_settings
from moooney.ingest.load import ingest_directory
from moooney.llm.client import (
    CallRecorder,
    LLMClient,
    LLMError,
    LLMSettings,
    load_llm_settings,
    offline_enabled,
)
from moooney.llm.decide import build_decider
from moooney.web.jobs import Logger, report, stage


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Paths:
    """Rutas de configuración junto a settings.yaml (por defecto, config/)."""

    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.llm = config_dir / "llm.yaml"
        self.enrichment = config_dir / "enrichment.yaml"
        self.budgets = config_dir / "budgets.yaml"
        self.categories = config_dir / "categories.yaml"
        self.rules = config_dir / "rules.yaml"


def taxonomy_for(paths: Paths) -> Taxonomy:
    return load_taxonomy(paths.categories)


def llm_settings_or_none(paths: Paths) -> LLMSettings | None:
    try:
        return load_llm_settings(paths.llm)
    except FileNotFoundError:
        return None


def enrichment_settings_or_none(paths: Paths) -> EnrichmentSettings | None:
    try:
        return load_enrichment_settings(paths.enrichment)
    except FileNotFoundError:
        return None


def test_llm_connection(paths: Paths, *, transport=None) -> dict:
    """Comprueba clave y proxy con GET /models. Nunca registra ni devuelve la clave."""
    if offline_enabled():
        return {"ok": False, "message": "Modo offline: desactívalo en esta página para probar."}
    llm = llm_settings_or_none(paths)
    if llm is None:
        return {"ok": False, "message": "Falta config/llm.yaml."}
    try:
        modelos = LLMClient(llm, transport=transport).list_models()
    except LLMError as e:
        return {"ok": False, "message": f"No funciona: {e}"}
    usados = set(llm.models.values())
    faltan = sorted(usados - set(modelos))
    if faltan:
        return {"ok": False, "message": f"Conecta, pero el proxy no ofrece: {', '.join(faltan)}."}
    return {"ok": True, "message": f"Conexión correcta: {len(modelos)} modelos disponibles, los {len(usados)} que usa moooney incluidos."}


# ---------- ediciones directas (rápidas, en la petición) ----------


def set_merchant_category(
    conn: sqlite3.Connection, taxonomy: Taxonomy, key: str, category: str, flow_type: str | None
) -> int:
    """Corrección manual: source=user, confianza 1. Devuelve transacciones afectadas."""
    if category not in taxonomy.categories:
        raise ValueError(f"categoría desconocida: {category}")
    if not flow_type or flow_type not in taxonomy.flow_types:
        flow_type = taxonomy.default_flow_type(category)
    repo.upsert_merchant(
        conn, key=key, category=category, subcategory=None, flow_type=flow_type,
        source="user", confidence=1.0, updated_at=_now(),
    )
    return repo.propagate_to_transactions(
        conn, key=key, category=category, subcategory=None, flow_type=flow_type, confidence=1.0
    )


def set_budget(conn: sqlite3.Connection, paths: Paths, category: str, amount: float) -> None:
    limits = budgets_mod.load_budgets_config(paths.budgets)
    if amount <= 0:
        limits.pop(category, None)
    else:
        limits[category] = amount
    budgets_mod.save_budgets_config(paths.budgets, limits, meta={"source": "user"})
    as_of = periods.as_of_date(conn)
    if as_of and amount > 0:
        budgets_mod.materialize_month(conn, periods.month_of(as_of), {category: amount}, source="user")
    elif as_of:
        with conn:
            conn.execute(
                "DELETE FROM budgets WHERE category = ? AND month = ?",
                (category, periods.month_of(as_of)),
            )


def propose_and_save_budgets(conn: sqlite3.Connection, paths: Paths, *, months: int, cut: float) -> dict:
    refresh_recurring(conn)
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return {}
    month = periods.month_of(as_of)
    proposal = budgets_mod.propose_budgets(
        conn, month, months=months, cut=cut, as_of=as_of, first=periods.first_date(conn)
    )
    if proposal:
        limits = {c: v["limit"] for c, v in proposal.items()}
        budgets_mod.save_budgets_config(
            paths.budgets, limits, meta={"source": "proposal", "months": months, "cut": cut, "as_of": as_of}
        )
        budgets_mod.materialize_month(conn, month, limits, source="proposal")
    return proposal


def refresh_recurring(conn: sqlite3.Connection) -> int:
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return 0
    return store_recurring(conn, detect_recurring(conn, as_of), _now())


def refresh_alerts(conn: sqlite3.Connection, paths: Paths) -> tuple[int, int]:
    """Recalcula alertas. Devuelve (detectadas, nuevas)."""
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return 0, 0
    refresh_recurring(conn)
    month = periods.month_of(as_of)
    limits_cfg = budgets_mod.load_budgets_config(paths.budgets)
    rows = None
    if limits_cfg:
        limits = budgets_mod.limits_for_month(conn, month, limits_cfg)
        rows = budgets_mod.budget_status(conn, month, limits, as_of=as_of)
    found = alerts_mod.run_alerts(conn, as_of, rows, month)
    return len(found), alerts_mod.store_alerts(conn, found)


def set_enrichment_enabled(paths: Paths, enabled: bool) -> None:
    data = yaml.safe_load(paths.enrichment.read_text(encoding="utf-8")) or {}
    data["enabled"] = bool(enabled)
    # Se reescribe el YAML entero: los comentarios del fichero original se pierden,
    # por eso el README documenta cada clave.
    paths.enrichment.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


# ---------- trabajos en segundo plano ----------


def job_ingest(settings: Settings, log: Logger) -> None:
    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    try:
        report(log, "Leyendo los Excel de la carpeta y cargando los movimientos")
        outcomes = ingest_directory(settings.paths.excel_dir, conn, settings)
        if not outcomes:
            log(f"no hay ficheros Excel en {settings.paths.excel_dir}")
        for o in outcomes:
            if o.error:
                log(f"{o.filename}: no se ha podido leer ({o.error}); los demás siguen")
            elif o.skipped:
                log(f"{o.filename}: ya importado, se omite")
            else:
                log(f"{o.filename}: {o.rows_total} filas, {o.rows_new} nuevas ({o.date_min} … {o.date_max})")
        report(log, "Calculando la clave de comercio de los movimientos nuevos")
        n = backfill_clean_keys(conn)
        if n:
            log(f"claves de comercio calculadas para {n} movimientos nuevos")
    finally:
        conn.close()


def job_categorize(settings: Settings, paths: Paths, log: Logger, *, no_llm: bool = False,
                   refresh: bool = False, rebuild: bool = False, retry: bool = False,
                   second_opinion: bool = False) -> None:
    taxonomy = taxonomy_for(paths)
    rules = load_rules(paths.rules, taxonomy=taxonomy)
    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    recorder: CallRecorder | None = None
    try:
        if rebuild:
            n_rows, n_mig = rebuild_clean_keys(conn)
            log(f"claves reconstruidas: {n_rows} movimientos cambiaron de clave, {n_mig} entradas migradas")
        backfill_clean_keys(conn)
        if refresh:
            n_upd, n_exam = refresh_rules(conn, rules)
            log(f"reglas reaplicadas: {n_upd} claves cambiadas de {n_exam}")
        report(log, "Buscando los comercios sin categoría")
        jobs = pending_jobs(conn)
        log(f"{len(jobs)} claves pendientes")
        report(log, "Aplicando tus reglas")
        n_rules, restantes = apply_rules(conn, jobs, rules)
        log(f"reglas: {n_rules} resueltas, {len(restantes)} restantes")
        llm = llm_settings_or_none(paths)
        if restantes and llm is not None and not offline_enabled():
            recorder = CallRecorder(conn)
            cliente = LLMClient(llm, recorder=recorder)
            try:
                report(log, f"Buscando comercios parecidos a los ya conocidos ({len(restantes)} claves)")
                n_emb, restantes = apply_embeddings(conn, restantes, cliente, llm.models["embeddings"])
                log(f"embeddings: {n_emb} heredadas, {len(restantes)} restantes")
            except LLMError as e:
                log(f"embeddings no disponibles ({e}); sigo con el decider")
            if restantes and not no_llm:
                decider = build_decider(llm, cliente)
                n_clef, n_fail = apply_decider(
                    conn, restantes, decider, taxonomy, build_questions(taxonomy),
                    max_workers=llm.max_concurrency,
                    on_count=lambda k, n: report(log, "Preguntando al modelo por cada comercio", k, n),
                )
                log(f"decider ({llm.decider}): {n_clef} resueltas, {n_fail} fallidas")
        elif restantes:
            motivo = "modo offline" if offline_enabled() else "sin config/llm.yaml" if llm is None else "sin modelo"
            log(f"{len(restantes)} claves quedan pendientes ({motivo})")
        if (retry or second_opinion) and not no_llm:
            if llm is None or offline_enabled():
                log("reintento/segunda opinión omitidos: " + ("modo offline" if offline_enabled() else "sin config/llm.yaml"))
            else:
                from moooney.categorize.reask import reask_dudosas
                from moooney.categorize.second_opinion import run_second_opinion

                if recorder is None:
                    recorder = CallRecorder(conn)
                cliente = LLMClient(llm, recorder=recorder)
                if retry:
                    report(log, "Volviendo a preguntar por las dudosas")
                    rs = reask_dudosas(
                        conn, build_decider(llm, cliente), taxonomy, build_questions(taxonomy),
                        max_workers=llm.max_concurrency, on_progress=lambda k, m: log(f"  {k}: {m}"),
                    )
                    log(f"reintento: {rs.examined} dudosas, {rs.improved} mejoran "
                        f"({rs.changed} cambian de categoría), {rs.failed} fallidas")
                if second_opinion:
                    recorder.flush()
                    report(log, "El modelo de texto revisa las dudosas por lotes")
                    ss = run_second_opinion(conn, cliente, llm, taxonomy, on_progress=lambda k, m: log(f"  {k}: {m}"))
                    log(f"segunda opinión: {ss.examined} dudosas, {ss.changed} cambian, {ss.confirmed} confirmadas, "
                        f"{ss.kept} se quedan, {ss.failed} fallidas")
        repo.mark_card_settlements(conn)
        sin = conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE category IS NULL").fetchone()["n"]
        log(f"movimientos sin categoría: {sin}")
    finally:
        if recorder is not None:
            recorder.flush()
        conn.close()


def enrich_preview(conn: sqlite3.Connection, paths: Paths) -> list[dict]:
    enr = enrichment_settings_or_none(paths)
    if enr is None:
        return []
    out = []
    for c in select_candidates(conn, enr):
        out.append({
            "key": c.key, "category": c.category, "confidence": c.confidence,
            "query": c.query, "url": nominatim_url(c.query, enr.nominatim_country_codes) if c.query else None,
        })
    return out


def job_enrich(settings: Settings, paths: Paths, log: Logger, *, limit: int | None = None) -> None:
    enr = enrichment_settings_or_none(paths)
    if enr is None:
        log("sin config/enrichment.yaml")
        return
    if not enr.enabled:
        log("el enriquecimiento está desactivado (enabled: false)")
        return
    if offline_enabled():
        log("modo offline: no se hace ninguna consulta")
        return
    llm = llm_settings_or_none(paths)
    if llm is None:
        log("sin config/llm.yaml: no hay decider para la segunda ronda")
        return
    taxonomy = taxonomy_for(paths)
    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    recorder = CallRecorder(conn)
    try:
        cliente = LLMClient(llm, recorder=recorder)
        candidates = select_candidates(conn, enr)
        con_consulta = sum(1 for c in candidates if c.query)
        log(f"{len(candidates)} candidatas, {con_consulta} con consulta")
        total = min(len(candidates), limit) if limit else len(candidates)
        hechas = [0]

        def avance(key: str, prov: str) -> None:
            hechas[0] += 1
            log(f"  {key}: {prov}")
            report(log, "Consultando fuera las dudosas", min(hechas[0], total), total)

        report(log, "Consultando fuera las dudosas", 0, total)
        summary = run_enrichment(
            conn, enr, build_providers(enr), build_decider(llm, cliente), taxonomy,
            build_questions(taxonomy), candidates=candidates, limit=limit, on_progress=avance,
        )
        log(f"consultadas {summary.queried}, con resultado {summary.hits}, errores {summary.errors}")
        log(f"mejoraron {summary.improved}, cambiaron de categoría {summary.changed_category}, "
            f"siguen dudosas {len(summary.still_low)}")
    finally:
        recorder.flush()
        conn.close()


def job_report(settings: Settings, paths: Paths, log: Logger, *, month: str | None = None,
               use_model: bool = True) -> None:
    from moooney.report import advice

    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    recorder: CallRecorder | None = None
    try:
        report(log, "Preparando los agregados del mes")
        refresh_recurring(conn)
        limits = budgets_mod.load_budgets_config(paths.budgets)
        payload = advice.build_payload(conn, month, budgets_limits=limits or None)
        m = payload["mes"]
        log(f"agregados de {m} preparados ({len(advice.payload_text(payload))} caracteres)")
        llm = llm_settings_or_none(paths)
        if use_model and llm is not None and not offline_enabled():
            recorder = CallRecorder(conn)
            cliente = LLMClient(llm, recorder=recorder)
            try:
                report(log, "El modelo está redactando el informe")
                content = advice.generate_report(cliente, llm, payload)
                model = llm.models.get("redaccion", "?")
                kind = "modelo"
                log(f"informe redactado por {model}")
            except LLMError as e:
                log(f"el modelo falló ({e}); se guarda la versión sin modelo")
                content, model, kind = advice.fallback_report(payload), None, "reglas"
        else:
            motivo = "modo offline" if offline_enabled() else "sin modelo"
            log(f"{motivo}: informe a partir de los hallazgos deterministas")
            content, model, kind = advice.fallback_report(payload), None, "reglas"
        advice.store_report(conn, month=m, kind=kind, model=model, content=content)
        out_dir = settings.paths.reports_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / f"{m}.md").write_text(content + "\n", encoding="utf-8")
        log(f"guardado en reports/{m}.md")
    finally:
        if recorder is not None:
            recorder.flush()
        conn.close()


def job_repair_overlaps(settings: Settings, log: Logger) -> int:
    """Arregla lo que cargaron las versiones anteriores a 1.5.1: quita los movimientos que
    entraron dos veces por solaparse extractos y recupera los que se juntaron en uno.
    Devuelve cuántos movimientos se recuperaron (hay que categorizarlos)."""
    from moooney.ingest.load import (
        find_overlap_duplicates,
        recover_missing,
        remove_overlap_duplicates,
    )

    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    try:
        report(log, "Buscando movimientos repetidos entre extractos que se solapan")
        n = remove_overlap_duplicates(conn, find_overlap_duplicates(conn))
        log(f"movimientos repetidos quitados: {n}" if n else "ningún movimiento repetido entre extractos")
        report(log, "Releyendo los Excel de la carpeta por si falta algún movimiento")
        r = recover_missing(conn, settings)
        log(f"movimientos recuperados: {r}" if r else "no faltaba ningún movimiento")
        if r:
            backfill_clean_keys(conn)
        repo.mark_card_settlements(conn)
        return r
    finally:
        conn.close()


def job_repair(real: Settings, settings: Settings, paths: Paths, log: Logger) -> None:
    """Revisar extractos solapados y dejar la analítica al día con lo que cambie."""
    stage(log, "Revisar extractos")
    recuperados = job_repair_overlaps(real, log)
    if recuperados:
        stage(log, "Categorizar")
        job_categorize(settings, paths, log)
    stage(log, "Recalcular alertas")
    job_alerts(settings, paths, log)


def job_alerts(settings: Settings, paths: Paths, log: Logger) -> None:
    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    try:
        report(log, "Buscando pagos recurrentes")
        n = refresh_recurring(conn)
        log(f"recurrentes detectados: {n}")
        report(log, "Comprobando presupuestos y alertas")
        found, nuevas = refresh_alerts(conn, paths)
        log(f"alertas detectadas: {found}, nuevas: {nuevas}")
    finally:
        conn.close()


def job_backup(settings: Settings, log: Logger) -> None:
    """Copia de seguridad de la base del usuario (ver db/backup.py)."""
    from moooney.db.backup import make_backup

    stage(log, "Copia de seguridad")
    copia = make_backup(settings.paths.db_path, settings.paths.backups_dir)
    log(f"copia guardada: {copia.name} ({copia.stat().st_size / 1e6:.1f} MB)")
    log("se guardan las 14 más recientes; para volver a una: moooney backup --restore <fichero>")


def job_reset(settings: Settings, log: Logger) -> None:
    """Empezar de cero: borra movimientos y extractos y conserva memoria de comercios,
    presupuestos y configuración (ver ingest/reset.py)."""
    from moooney.ingest.reset import reset_data

    stage(log, "Borrar movimientos")
    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    try:
        report(log, "Borrando Excel, movimientos, alertas e informes")
        r = reset_data(conn, settings)
    finally:
        conn.close()
    log(f"borrados {r.movimientos} movimientos de {r.importaciones} importaciones")
    log(f"borrados {r.excel} Excel de la carpeta de entrada, {r.csv} CSV limpios y {r.informes} informes")
    log("se conservan la memoria de comercios, los presupuestos y la configuración")
    if r.copia is not None:
        log(f"copia de la base antes de borrar: {r.copia.name} (moooney backup --restore para volver)")
