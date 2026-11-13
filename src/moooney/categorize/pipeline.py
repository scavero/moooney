"""Cascada de categorización: reglas → memoria por embedding → Clef/Qwen.

Reglas y métricas son deterministas; el modelo solo interpreta claves que
ninguna regla ni la memoria de comercios han podido resolver.
"""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

from moooney.categorize import embed
from moooney.categorize.normalize import operation_type
from moooney.categorize.rules import Rule, match_rules
from moooney.categorize.taxonomy import Taxonomy
from moooney.db import repo
from moooney.llm.client import LLMError
from moooney.llm.decide import Answer, Decider


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class KeyJob:
    """Estadísticas agregadas de una clave pendiente (lo único que ve el modelo)."""

    key: str
    n: int
    avg_amount: float
    gap_days: float | None
    op_type: str | None = None  # recibo domiciliado, compra con tarjeta… (ver normalize)


def pending_jobs(conn: sqlite3.Connection) -> list[KeyJob]:
    """Claves sin entrada en merchants, con sus agregados."""
    rows = conn.execute(
        """
        SELECT t.concept_clean AS k, COUNT(*) AS n, AVG(t.amount) AS avg_amount,
               MIN(t.date) AS dmin, MAX(t.date) AS dmax, MIN(t.concept) AS concept
        FROM transactions t
        LEFT JOIN merchants m ON m.key = t.concept_clean
        WHERE t.concept_clean IS NOT NULL AND m.key IS NULL
        GROUP BY t.concept_clean
        ORDER BY n DESC
        """
    ).fetchall()
    return _jobs_from_rows(rows)


def _jobs_from_rows(rows) -> list[KeyJob]:
    jobs = []
    for r in rows:
        gap = None
        if r["n"] > 1 and r["dmin"] and r["dmax"]:
            d0 = date.fromisoformat(r["dmin"])
            d1 = date.fromisoformat(r["dmax"])
            gap = (d1 - d0).days / (r["n"] - 1)
        concept = r["concept"] if "concept" in r.keys() else None
        jobs.append(KeyJob(key=r["k"], n=r["n"], avg_amount=float(r["avg_amount"]), gap_days=gap,
                           op_type=operation_type(concept) if concept else None))
    return jobs


def pending_jobs_for_keys(conn: sqlite3.Connection, keys: list[str]) -> list[KeyJob]:
    """Agregados de claves concretas, tengan o no entrada en merchants."""
    if not keys:
        return []
    placeholders = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"""
        SELECT concept_clean AS k, COUNT(*) AS n, AVG(amount) AS avg_amount,
               MIN(date) AS dmin, MAX(date) AS dmax, MIN(concept) AS concept
        FROM transactions
        WHERE concept_clean IN ({placeholders})
        GROUP BY concept_clean
        """,
        keys,
    ).fetchall()
    return _jobs_from_rows(rows)


def dudosa_jobs(conn: sqlite3.Connection, *, threshold: float = 0.7,
                sources: tuple[str, ...] = ("clef", "clef_web", "llm")) -> list[KeyJob]:
    """Claves dudosas (confianza < umbral u Otros) que vienen de un modelo, las de más euros primero."""
    ph = ",".join("?" for _ in sources)
    rows = conn.execute(
        f"""
        SELECT t.concept_clean AS k, COUNT(*) AS n, AVG(t.amount) AS avg_amount,
               MIN(t.date) AS dmin, MAX(t.date) AS dmax, MIN(t.concept) AS concept
        FROM merchants m JOIN transactions t ON t.concept_clean = m.key
        WHERE m.source IN ({ph}) AND (m.confidence < ? OR m.category = 'Otros')
        GROUP BY t.concept_clean
        ORDER BY ABS(SUM(t.amount)) DESC
        """,
        [*sources, threshold],
    ).fetchall()
    return _jobs_from_rows(rows)


def refresh_rules(
    conn: sqlite3.Connection, rules: list[Rule], *, dry_run: bool = False
) -> tuple[int, int]:
    """Reaplica las reglas a las claves ya categorizadas por rule/embedding/clef.

    Sirve cuando se corrige o amplía rules.yaml: una clave que ahora casa con una
    regla distinta se actualiza. Nunca toca source=user. Devuelve
    (claves_actualizadas, claves_examinadas).
    """
    rows = conn.execute(
        "SELECT key, category, subcategory, flow_type FROM merchants WHERE source != 'user'"
    ).fetchall()
    actualizadas = 0
    for r in rows:
        match = match_rules(r["key"], rules)
        if match is None:
            continue
        if (
            match.category == r["category"]
            and match.subcategory == r["subcategory"]
            and match.flow_type == r["flow_type"]
        ):
            continue
        if _save(
            conn,
            key=r["key"],
            category=match.category,
            subcategory=match.subcategory,
            flow_type=match.flow_type,
            source="rule",
            confidence=1.0,
            dry_run=dry_run,
        ):
            actualizadas += 1
    return actualizadas, len(rows)


def build_state(job: KeyJob) -> str:
    """Redacción del estado para Clef. Importe redondeado a la decena: ni
    saldos ni importes exactos salen de la máquina."""
    media = round(job.avg_amount / 10) * 10
    tipo = "gasto" if job.avg_amount < 0 else "abono"
    gap = f", intervalo medio entre apariciones {round(job.gap_days)} dias" if job.gap_days else ""
    op = f" Tipo de operacion: {job.op_type}." if job.op_type else ""
    return (
        f"Concepto de un apunte: {job.key}. "
        f"Importe medio: {media:+.0f} EUR ({tipo}) en {job.n} apariciones{gap}.{op}"
    )


def build_questions(taxonomy: Taxonomy) -> dict:
    return {
        "category": {
            "type": "choice",
            "instructions": (
                "Categoria del movimiento. Si no reconoces el nombre comercial"
                " y no puedes deducir el gasto, elige Otros"
            ),
            "criteria": {name: spec.description for name, spec in taxonomy.categories.items()},
        },
        "flow_type": {
            "type": "choice",
            "instructions": "Tipo de flujo",
            "criteria": dict(taxonomy.flow_types),
        },
        "is_recurring": {
            "type": "noul",
            "instructions": "Parece un cargo recurrente o periodico?",
        },
    }


def decide_all(decider: Decider, state: str, questions: dict, taxonomy: Taxonomy) -> dict[str, Answer]:
    """Categoría y, ya decidida, su subcategoría en una segunda pregunta corta (2 a 6 opciones)."""
    answers = decider.decide(state, questions)
    category = answers.get("category")
    spec = taxonomy.categories.get(category.choice) if category else None
    if spec and len(spec.subcategories) > 1:
        try:
            sub = decider.decide(state, sub_question(category.choice, spec)).get("subcategory")
        except (LLMError, ValueError):
            sub = None  # sin subcategoría antes que perder la categoría ya decidida
        if sub is not None:
            answers["subcategory"] = sub
    return answers


def sub_question(category: str, spec) -> dict:
    return {"subcategory": {
        "type": "choice",
        "instructions": f"Subcategoria del movimiento dentro de {category}",
        "criteria": {s: s for s in spec.subcategories},
    }}


def fill_subcategories(conn: sqlite3.Connection, decider: Decider, taxonomy: Taxonomy, *,
                       max_workers: int = 8, limit: int = 0) -> tuple[int, int, int]:
    """Rellena la subcategoría de los comercios ya categorizados que no la tienen.

    Solo hace la pregunta corta de subcategoría: categoría, flujo, origen y confianza no se tocan.
    Devuelve (examinadas, rellenadas, fallidas).
    """
    pend: dict[str, str] = {}
    for r in conn.execute("SELECT key, category, subcategory, source FROM merchants"):
        spec = taxonomy.categories.get(r["category"])
        if spec is None or len(spec.subcategories) < 2:
            continue
        por_defecto = r["subcategory"] == "otros" and r["source"] not in ("user", "rule")
        if r["subcategory"] not in spec.subcategories or por_defecto:
            pend[r["key"]] = r["category"]
    jobs = pending_jobs_for_keys(conn, list(pend))
    if limit:
        jobs = jobs[:limit]
    if not jobs:
        return 0, 0, 0

    def ask(job: KeyJob) -> Answer | None:
        cat = pend[job.key]
        return decider.decide(build_state(job), sub_question(cat, taxonomy.categories[cat])).get("subcategory")

    hechas = fallidas = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
        futures = {pool.submit(ask, job): job for job in jobs}
        for future, job in futures.items():
            try:
                sub = future.result()
            except (LLMError, ValueError):
                sub = None
            if sub is None or sub.choice not in taxonomy.categories[pend[job.key]].subcategories:
                fallidas += 1
                continue
            conn.execute("UPDATE merchants SET subcategory = ? WHERE key = ?", (sub.choice, job.key))
            conn.execute("UPDATE transactions SET subcategory = ? WHERE concept_clean = ? AND category = ?",
                         (sub.choice, job.key, pend[job.key]))
            hechas += 1
    conn.commit()
    return len(jobs), hechas, fallidas


def subcategory_of(answers: dict[str, Answer], spec) -> str | None:
    """La subcategoría elegida si es válida; si no, la única posible, «otros» o ninguna."""
    sub = answers.get("subcategory")
    if sub is not None and sub.choice in spec.subcategories:
        return sub.choice
    if len(spec.subcategories) == 1:
        return spec.subcategories[0]
    return "otros" if "otros" in spec.subcategories else None


def _save(
    conn: sqlite3.Connection,
    *,
    key: str,
    category: str,
    subcategory: str | None,
    flow_type: str,
    source: str,
    confidence: float,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True
    saved = repo.upsert_merchant(
        conn,
        key=key,
        category=category,
        subcategory=subcategory,
        flow_type=flow_type,
        source=source,
        confidence=confidence,
        updated_at=_now(),
    )
    if saved:
        repo.propagate_to_transactions(
            conn,
            key=key,
            category=category,
            subcategory=subcategory,
            flow_type=flow_type,
            confidence=confidence,
        )
    return saved


def apply_rules(
    conn: sqlite3.Connection,
    jobs: list[KeyJob],
    rules: list[Rule],
    *,
    dry_run: bool = False,
) -> tuple[int, list[KeyJob]]:
    """Pasa las reglas por las claves pendientes. Devuelve (resueltas, restantes)."""
    resueltas = 0
    restantes: list[KeyJob] = []
    for job in jobs:
        match = match_rules(job.key, rules)
        if match and _save(
            conn,
            key=job.key,
            category=match.category,
            subcategory=match.subcategory,
            flow_type=match.flow_type,
            source="rule",
            confidence=1.0,
            dry_run=dry_run,
        ):
            resueltas += 1
        elif not match:
            restantes.append(job)
    return resueltas, restantes


def apply_embeddings(
    conn: sqlite3.Connection,
    jobs: list[KeyJob],
    client,
    model: str,
    *,
    dry_run: bool = False,
) -> tuple[int, list[KeyJob]]:
    """Hereda categorías de claves user/rule parecidas (>0.92). Sin cliente (None) salta."""
    if client is None:
        return 0, jobs
    vectors = embed.embed_keys(client, model, [j.key for j in jobs])
    resueltas = 0
    restantes: list[KeyJob] = []
    for job in jobs:
        vector = vectors[job.key]
        if not dry_run:
            embed.store_embedding(conn, key=job.key, model=model, vector=vector, updated_at=_now())
        hit = embed.find_similar_labeled(conn, vector, model=model)
        if hit is not None:
            merchant, sim = hit
            if _save(
                conn,
                key=job.key,
                category=merchant["category"],
                subcategory=merchant["subcategory"],
                flow_type=merchant["flow_type"],
                source="embedding",
                confidence=sim,
                dry_run=dry_run,
            ):
                resueltas += 1
                continue
        restantes.append(job)
    return resueltas, restantes


def apply_decider(
    conn: sqlite3.Connection,
    jobs: list[KeyJob],
    decider: Decider,
    taxonomy: Taxonomy,
    questions: dict,
    *,
    max_workers: int = 8,
    dry_run: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
    on_count: Callable[[int, int], None] | None = None,
) -> tuple[int, int]:
    """Consulta el decider para las claves restantes, en paralelo.

    Devuelve (resueltas, fallidas). Las escritas van a la DB desde este hilo
    principal; los workers solo hacen red.
    """
    if not jobs:
        return 0, 0

    def ask(job: KeyJob) -> dict[str, Answer]:
        return decide_all(decider, build_state(job), questions, taxonomy)

    resueltas = fallidas = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
        futures = {pool.submit(ask, job): job for job in jobs}
        for i, (future, job) in enumerate(futures.items(), start=1):
            if on_count:  # cuántas claves van respondidas de cuántas, para la barra de la web
                on_count(i - 1, len(jobs))
            try:
                answers = future.result()
            except (LLMError, ValueError):
                fallidas += 1
                continue
            saved = _save_answers(
                conn, job, answers, taxonomy, source="clef", dry_run=dry_run
            )
            if saved:
                resueltas += 1
                if on_progress:
                    on_progress(job.key, "clef")
            else:
                fallidas += 1
    return resueltas, fallidas


def _save_answers(
    conn: sqlite3.Connection,
    job: KeyJob,
    answers: dict[str, Answer],
    taxonomy: Taxonomy,
    *,
    source: str,
    dry_run: bool,
) -> bool:
    category = answers.get("category")
    if category is None or category.choice not in taxonomy.categories:
        return False
    flow = answers.get("flow_type")
    flow_type = (
        flow.choice
        if flow is not None and flow.choice in taxonomy.flow_types
        else taxonomy.default_flow_type(category.choice)
    )
    spec = taxonomy.categories[category.choice]
    subcategory = subcategory_of(answers, spec)
    return _save(
        conn,
        key=job.key,
        category=category.choice,
        subcategory=subcategory,
        flow_type=flow_type,
        source=source,
        confidence=category.confidence,
        dry_run=dry_run,
    )
