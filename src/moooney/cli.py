"""CLI de moooney: ingesta, categorización, enriquecimiento y revisión."""

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from moooney import envfile
from moooney.analytics import alerts as alerts_mod
from moooney.analytics import budgets as budgets_mod
from moooney.analytics import periods
from moooney.analytics.metrics import build_report
from moooney.analytics.recurring import detect_recurring, load_recurring, store_recurring
from moooney.categorize.evaluate import CALIBRATION_BINS, evaluate, load_labels
from moooney.categorize.keys import backfill_clean_keys, key_stats, rebuild_clean_keys
from moooney.categorize.pipeline import (
    apply_decider,
    apply_embeddings,
    apply_rules,
    build_questions,
    pending_jobs,
    refresh_rules,
)
from moooney.categorize.rules import load_rules
from moooney.categorize.taxonomy import load_taxonomy
from moooney.config import Settings, load_settings
from moooney.db import repo
from moooney.enrich.providers import build_providers, nominatim_url
from moooney.enrich.runner import EnrichSummary, run_enrichment, select_candidates
from moooney.enrich.settings import EnrichmentSettings, load_enrichment_settings
from moooney.ingest.load import ingest_directory
from moooney.ingest.parse_santander import ParseError, inspect_file
from moooney.llm.client import (
    CallRecorder,
    LLMClient,
    LLMError,
    load_llm_settings,
    offline_enabled,
)
from moooney.llm.decide import build_decider

app = typer.Typer(help="Pipeline local de finanzas personales.")
console = Console()

CONFIG_OPTION = typer.Option(None, "--config", help="Ruta a settings.yaml.")


def _settings(config: Path | None) -> Settings:
    try:
        settings = load_settings(config)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    # Claves: variable de entorno > data/.env (guardado desde la web) > ./.env
    envfile.load_env_files(settings.paths.db_path.parent)
    return settings


@app.command()
def inspect(
    ruta: Path = typer.Argument(..., help="Fichero .xlsx a comprobar."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Comprueba si el parser entiende un Excel. No muestra conceptos ni importes."""
    settings = _settings(config)
    try:
        result = inspect_file(ruta, settings.profiles)
    except ParseError as e:
        console.print(f"[red]No se ha podido interpretar {ruta.name}: {e}[/red]")
        raise typer.Exit(code=1) from None

    df = result.df
    assert df is not None
    console.print(f"[bold]Banco:[/bold] {result.bank}")
    console.print(f"[bold]Hoja:[/bold] {result.sheet_name}")
    console.print(f"[bold]Fila de cabecera:[/bold] {result.header_row}")
    console.print(f"[bold]Cuenta:[/bold] {'IBAN detectado' if result.account_found else 'etiqueta fija'}")

    table = Table(title="Mapeo de columnas")
    table.add_column("Columna destino")
    table.add_column("Cabecera original")
    for target, original in result.mapping.items():
        table.add_row(target, original)
    for original in result.unmapped:
        table.add_row("[dim]—[/dim]", f"[dim]{original}[/dim]")
    console.print(table)

    console.print(f"[bold]Filas de datos:[/bold] {len(df)}")
    if len(df):
        console.print(f"[bold]Fecha mínima:[/bold] {df['date'].min()}")
        console.print(f"[bold]Fecha máxima:[/bold] {df['date'].max()}")
        n_pos = int((df["amount"] > 0).sum())
        n_neg = int((df["amount"] < 0).sum())
        console.print(f"[bold]Suma de control:[/bold] {n_pos} importes positivos, {n_neg} negativos")


@app.command()
def ingest(
    dir: Path = typer.Option(None, "--dir", help="Directorio con ficheros Excel."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Importa los Excel nuevos a data/clean y a SQLite. Idempotente."""
    settings = _settings(config)
    directory = dir or settings.paths.excel_dir
    conn = repo.connect(settings.paths.db_path)
    try:
        outcomes = ingest_directory(directory, conn, settings)
    finally:
        conn.close()

    table = Table(title="Ingesta")
    table.add_column("Fichero")
    table.add_column("Filas totales", justify="right")
    table.add_column("Filas nuevas", justify="right")
    table.add_column("Rango de fechas")
    for o in outcomes:
        if o.skipped:
            table.add_row(o.filename, "[dim]—[/dim]", "[dim]0 (repetido)[/dim]", "[dim]—[/dim]")
        else:
            rng = f"{o.date_min} … {o.date_max}" if o.date_min else "—"
            table.add_row(o.filename, str(o.rows_total), str(o.rows_new), rng)
    console.print(table)


@app.command()
def status(config: Path = CONFIG_OPTION) -> None:
    """Estado de la base de datos. Solo agregados."""
    settings = _settings(config)
    conn = repo.connect(settings.paths.db_path)
    try:
        st = repo.get_status(conn)
    finally:
        conn.close()

    table = Table(title="moooney status")
    table.add_column("Métrica")
    table.add_column("Valor", justify="right")
    rng = (
        f"{st['date_min']} … {st['date_max']}"
        if st["date_min"]
        else "sin datos"
    )
    table.add_row("Transacciones", f"{st['transactions']:,}")
    table.add_row("Rango de fechas", rng)
    table.add_row("Sin categoría", f"{st['uncategorized']:,}")
    table.add_row("Imports", str(st["imports"]))
    table.add_row("Último import", st["last_import_at"] or "—")
    console.print(table)


@app.command()
def merchants(
    limit: int = typer.Option(60, "--limit", help="Máximo de claves a mostrar."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Claves de comercio normalizadas: transacciones e importe total por clave."""
    settings = _settings(config)
    conn = repo.connect(settings.paths.db_path)
    try:
        updated = backfill_clean_keys(conn)
        stats = key_stats(conn)
    finally:
        conn.close()

    if updated:
        console.print(f"[dim]concept_clean calculado para {updated} filas[/dim]")
    table = Table(title=f"Claves de comercio ({len(stats)} distintas)")
    table.add_column("Clave")
    table.add_column("Tx", justify="right")
    table.add_column("Importe total", justify="right")
    table.add_column("Categoría")
    for row in stats[:limit]:
        cat = row["category"] or "[yellow]sin categorizar[/yellow]"
        table.add_row(row["key"], str(row["n"]), f"{row['total']:+,.2f} €", cat)
    console.print(table)


def _llm_config_path(config: Path | None) -> Path:
    """llm.yaml vive junto a settings.yaml (por defecto, config/llm.yaml)."""
    return (config.parent if config else Path("config")) / "llm.yaml"


@app.command()
def categorize(
    limit: int = typer.Option(0, "--limit", help="Máximo de claves nuevas a procesar (0 = todas)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="No escribe en la DB ni llama a modelos."),
    no_llm: bool = typer.Option(False, "--no-llm", help="Solo reglas y embeddings; sin Clef/Qwen."),
    refresh: bool = typer.Option(
        False, "--refresh-rules", help="Reaplica rules.yaml a claves ya categorizadas (no user)."
    ),
    enrich: bool = typer.Option(
        False, "--enrich", help="Tras la cascada, enriquece por web lo que quede dudoso (fase 2.5)."
    ),
    rebuild: bool = typer.Option(
        False, "--rebuild-keys",
        help="Recalcula todas las claves de comercio (tras cambiar la normalización), migrando la memoria.",
    ),
    retry_dudosas: bool = typer.Option(
        False, "--retry-dudosas",
        help="Vuelve a preguntar al decider por las dudosas con tipo de operación y resumen web.",
    ),
    second_opinion: bool = typer.Option(
        False, "--second-opinion",
        help="Segunda opinión del modelo de texto, por lotes, sobre las dudosas que sigan siéndolo.",
    ),
    config: Path = CONFIG_OPTION,
) -> None:
    """Categoriza las claves nuevas en cascada: reglas -> embeddings -> Clef."""
    settings = _settings(config)
    try:
        taxonomy = load_taxonomy()
        rules = load_rules(taxonomy=taxonomy)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None

    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    try:
        if rebuild and not dry_run:
            n_rows, n_mig = rebuild_clean_keys(conn)
            console.print(
                f"claves reconstruidas: [green]{n_rows}[/green] transacciones cambiaron de clave, "
                f"{n_mig} entradas de memoria migradas"
            )
        backfill_clean_keys(conn)
        if refresh:
            n_upd, n_exam = refresh_rules(conn, rules, dry_run=dry_run)
            console.print(
                f"reglas reaplicadas: [green]{n_upd}[/green] claves cambiadas de {n_exam} examinadas"
            )
        jobs = pending_jobs(conn)
        if limit:
            jobs = jobs[:limit]
        console.print(f"[bold]{len(jobs)}[/bold] claves pendientes de categorizar")

        n_rules, restantes = apply_rules(conn, jobs, rules, dry_run=dry_run)
        console.print(f"reglas: [green]{n_rules}[/green] claves, {len(restantes)} restantes")

        n_embed = 0
        cliente: LLMClient | None = None
        recorder: CallRecorder | None = None
        llm = None
        try:
            llm = load_llm_settings(_llm_config_path(config))
        except FileNotFoundError:
            console.print("[dim]sin config/llm.yaml: se omiten embeddings y decider[/dim]")
        if llm is not None and not dry_run and not offline_enabled():
            recorder = CallRecorder(conn)
            cliente = LLMClient(llm, recorder=recorder)

        if cliente is not None and restantes:
            try:
                n_embed, restantes = apply_embeddings(
                    conn, restantes, cliente, llm.models["embeddings"], dry_run=dry_run
                )
            except LLMError as e:
                console.print(f"[yellow]embeddings no disponibles ({e}); sigo con el decider[/yellow]")
            console.print(f"embeddings: [green]{n_embed}[/green] claves, {len(restantes)} restantes")
        elif restantes and dry_run:
            console.print(f"[dim]dry-run: {len(restantes)} claves irían a embeddings y decider[/dim]")

        n_clef = n_fallos = 0
        if restantes and cliente is not None and not no_llm:
            decider = build_decider(llm, cliente)
            questions = build_questions(taxonomy)
            n_clef, n_fallos = apply_decider(
                conn,
                restantes,
                decider,
                taxonomy,
                questions,
                max_workers=llm.max_concurrency,
                dry_run=dry_run,
            )
            console.print(
                f"decider: [green]{n_clef}[/green] claves, [red]{n_fallos} fallidas[/red]"
            )
        elif restantes and not dry_run and (no_llm or cliente is None):
            console.print(f"[yellow]{len(restantes)} claves quedan pendientes (sin decider)[/yellow]")

        if dry_run:
            console.print("[dim]dry-run: no se ha escrito nada[/dim]")
            return

        if enrich:
            try:
                enr = load_enrichment_settings(_enrichment_config_path(config))
            except (FileNotFoundError, ValueError) as e:
                console.print(f"[red]{e}[/red]")
                raise typer.Exit(code=1) from None
            if not enr.enabled:
                console.print(
                    "[yellow]enriquecimiento desactivado[/yellow] (enabled: false en "
                    "config/enrichment.yaml); usa 'moooney enrich --dry-run' para ver que saldria"
                )
            elif cliente is None or no_llm:
                console.print("[yellow]enriquecimiento omitido: no hay decider disponible[/yellow]")
            else:
                if recorder is not None:
                    recorder.flush()
                _run_enrich(conn, enr, llm, cliente, taxonomy, limit=None, force=False, key=None)

        if retry_dudosas or second_opinion:
            if cliente is None or no_llm:
                console.print("[yellow]reintento/segunda opinión omitidos: no hay modelo disponible[/yellow]")
            else:
                from moooney.categorize.reask import reask_dudosas
                from moooney.categorize.second_opinion import run_second_opinion

                questions = build_questions(taxonomy)
                if retry_dudosas:
                    rs = reask_dudosas(
                        conn, build_decider(llm, cliente), taxonomy, questions,
                        max_workers=llm.max_concurrency,
                        on_progress=lambda k, m: console.print(f"  [dim]{k}[/dim]: {m}"),
                    )
                    console.print(
                        f"reintento: {rs.examined} dudosas, [green]{rs.improved}[/green] mejoran "
                        f"({rs.changed} cambian de categoría), {rs.failed} fallidas"
                    )
                if second_opinion:
                    recorder.flush()
                    ss = run_second_opinion(
                        conn, cliente, llm, taxonomy,
                        on_progress=lambda k, m: console.print(f"  [dim]{k}[/dim]: {m}"),
                    )
                    console.print(
                        f"segunda opinión: {ss.examined} dudosas, [green]{ss.changed}[/green] cambian, "
                        f"{ss.confirmed} confirmadas con más confianza, {ss.kept} se quedan, {ss.failed} fallidas"
                    )

        _print_categorizacion(conn)
    finally:
        if recorder is not None:
            recorder.flush()  # los workers solo encolan; aqui se graban los llm_calls
        conn.close()


def _enrichment_config_path(config: Path | None) -> Path:
    return (config.parent if config else Path("config")) / "enrichment.yaml"


def _run_enrich(conn, enr: EnrichmentSettings, llm, cliente: LLMClient, taxonomy, *,
                limit: int | None, force: bool, key: str | None) -> EnrichSummary:
    candidates = select_candidates(conn, enr, force=force, only_key=key)
    providers = build_providers(enr)
    decider = build_decider(llm, cliente)
    questions = build_questions(taxonomy)
    summary = run_enrichment(
        conn, enr, providers, decider, taxonomy, questions, candidates=candidates, limit=limit
    )
    _print_enrich_summary(summary)
    return summary


def _print_enrich_summary(summary: EnrichSummary) -> None:
    table = Table(title="Enriquecimiento externo")
    table.add_column("Métrica")
    table.add_column("Valor", justify="right")
    table.add_row("Claves candidatas", str(summary.candidates))
    table.add_row("Descartadas (sin consulta válida)", str(summary.skipped_no_query))
    table.add_row("Consultadas", str(summary.queried))
    for prov, n in sorted(summary.by_provider.items()):
        table.add_row(f"  peticiones a {prov}", str(n))
    table.add_row("Con resultado", str(summary.hits))
    table.add_row("Sin resultado", str(summary.misses))
    table.add_row("Errores de proveedor/modelo", str(summary.errors))
    table.add_row("Mejoraron la confianza", f"[green]{summary.improved}[/green]")
    table.add_row("Cambiaron de categoría", str(summary.changed_category))
    table.add_row("Siguen dudosas (para review)", str(len(summary.still_low)))
    console.print(table)


@app.command()
def enrich(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Lista las consultas que se enviarían, sin enviar nada."
    ),
    limit: int = typer.Option(0, "--limit", help="Máximo de consultas en esta ejecución (0 = config)."),
    force: bool = typer.Option(False, "--force", help="Reconsulta claves ya en caché."),
    key: str = typer.Option(None, "--key", help="Solo esta clave de comercio."),
    out: Path = typer.Option(
        None, "--out", help="Con --dry-run, escribe la lista a este fichero en vez de a pantalla."
    ),
    config: Path = CONFIG_OPTION,
) -> None:
    """Fase 2.5: busca en Nominatim/buscadores qué es un comercio dudoso y repregunta a Clef."""
    settings = _settings(config)
    try:
        taxonomy = load_taxonomy()
        enr = load_enrichment_settings(_enrichment_config_path(config))
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None

    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    recorder: CallRecorder | None = None
    try:
        candidates = select_candidates(conn, enr, force=force, only_key=key)
        con_consulta = [c for c in candidates if c.query]
        tope = min(enr.max_queries_per_run, limit or enr.max_queries_per_run)

        if dry_run:
            lines = [
                f"# moooney enrich --dry-run · {len(candidates)} candidatas, "
                f"{len(con_consulta)} con consulta, tope {tope} por ejecución",
                f"# proveedores en orden: {', '.join(enr.providers)}",
                "# formato: clave | categoría actual (conf) | consulta | URL de ejemplo (Nominatim)",
            ]
            for c in con_consulta[:tope]:
                lines.append(
                    f"{c.key} | {c.category} ({c.confidence:.2f}) | {c.query} | "
                    f"{nominatim_url(c.query, enr.nominatim_country_codes)}"
                )
            descartadas = [c.key for c in candidates if not c.query]
            if descartadas:
                lines.append(
                    f"# descartadas sin consulta ({len(descartadas)}): {', '.join(descartadas)}"
                )
            if out is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text("\n".join(lines) + "\n", encoding="utf-8")
                console.print(
                    f"[bold]{len(con_consulta[:tope])}[/bold] consultas listadas en {out} "
                    f"({len(candidates)} candidatas, {len(descartadas)} descartadas). Nada enviado."
                )
            else:
                for line in lines:
                    console.print(line, markup=False, highlight=False)
                console.print("[dim]dry-run: nada enviado[/dim]")
            return

        if not enr.enabled:
            console.print(
                "[red]El enriquecimiento está desactivado.[/red] Revisa primero "
                "'moooney enrich --dry-run' y, si estás de acuerdo con lo que saldría, pon "
                "'enabled: true' en config/enrichment.yaml."
            )
            raise typer.Exit(code=1)
        if offline_enabled():
            console.print("[red]MOOONEY_OFFLINE=1: enrich necesita red[/red]")
            raise typer.Exit(code=1)
        try:
            llm = load_llm_settings(_llm_config_path(config))
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1) from None
        recorder = CallRecorder(conn)
        cliente = LLMClient(llm, recorder=recorder)
        _run_enrich(conn, enr, llm, cliente, taxonomy, limit=limit or None, force=force, key=key)
    finally:
        if recorder is not None:
            recorder.flush()
        conn.close()


def _print_categorizacion(conn) -> None:
    """Resumen por source y por categoría, solo agregados."""
    src_table = Table(title="Categorización por source")
    src_table.add_column("Source")
    src_table.add_column("Claves", justify="right")
    src_table.add_column("Transacciones", justify="right")
    for row in conn.execute(
        """
        SELECT m.source AS s, COUNT(DISTINCT m.key) AS claves,
               COUNT(t.concept_clean) AS tx
        FROM merchants m
        LEFT JOIN transactions t ON t.concept_clean = m.key
        GROUP BY m.source ORDER BY tx DESC
        """
    ):
        src_table.add_row(row["s"], str(row["claves"]), f"{row['tx']:,}")
    console.print(src_table)

    cat_table = Table(title="Transacciones por categoría")
    cat_table.add_column("Categoría")
    cat_table.add_column("Tx", justify="right")
    cat_table.add_column("Gasto", justify="right")
    total_tx = 0
    total_gasto = 0.0
    gasto_otros = 0.0
    for row in conn.execute(
        """
        SELECT COALESCE(category, '(sin categorizar)') AS c, COUNT(*) AS n,
               SUM(CASE WHEN amount < 0 THEN -amount ELSE 0 END) AS gasto
        FROM transactions GROUP BY category ORDER BY n DESC
        """
    ):
        gasto = row["gasto"] or 0.0
        total_tx += row["n"]
        total_gasto += gasto
        if row["c"] == "Otros":
            gasto_otros = gasto
        cat_table.add_row(row["c"], f"{row['n']:,}", f"{gasto:,.0f} €")
    console.print(cat_table)

    sin_cat = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE category IS NULL"
    ).fetchone()["n"]
    pct_cat = 100.0 * (total_tx - sin_cat) / total_tx if total_tx else 0.0
    pct_otros = 100.0 * gasto_otros / total_gasto if total_gasto else 0.0
    console.print(
        f"categorizadas: [bold]{pct_cat:.1f} %[/bold] de las transacciones · "
        f"gasto en Otros: [bold]{pct_otros:.1f} %[/bold]"
    )


@app.command()
def review(
    threshold: float = typer.Option(0.7, "--threshold", help="Confianza por debajo de la cual revisar."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Revisa claves con confianza baja o en Otros y las corrige a mano."""
    settings = _settings(config)
    try:
        taxonomy = load_taxonomy()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None

    conn = repo.connect(settings.paths.db_path)
    try:
        backfill_clean_keys(conn)
        pendientes = conn.execute(
            """
            SELECT m.key, m.category, m.flow_type, m.source, m.confidence,
                   COUNT(t.concept_clean) AS n, COALESCE(SUM(t.amount), 0) AS total
            FROM merchants m LEFT JOIN transactions t ON t.concept_clean = m.key
            WHERE m.confidence < ? OR m.category = 'Otros'
            GROUP BY m.key
            ORDER BY ABS(COALESCE(SUM(t.amount), 0)) DESC
            """,
            (threshold,),
        ).fetchall()
        if not pendientes:
            console.print("[green]Nada que revisar:[/green] todas las claves superan el umbral.")
            return

        table = Table(title=f"Claves para revisar (umbral {threshold})")
        table.add_column("#", justify="right")
        table.add_column("Clave")
        table.add_column("Tx", justify="right")
        table.add_column("Total", justify="right")
        table.add_column("Categoría actual")
        table.add_column("Conf.")
        table.add_column("Source")
        for i, row in enumerate(pendientes, start=1):
            table.add_row(
                str(i),
                row["key"][:40],
                str(row["n"]),
                f"{row['total']:+,.0f} €",
                row["category"] or "—",
                f"{row['confidence']:.2f}",
                row["source"],
            )
        console.print(table)

        categorias = taxonomy.category_names
        cat_menu = Table(box=None, pad_edge=False)
        cat_menu.add_column("#", justify="right")
        cat_menu.add_column("Categoría")
        cat_menu.add_column("flow_type por defecto")
        for i, nombre in enumerate(categorias, start=1):
            cat_menu.add_row(str(i), nombre, taxonomy.default_flow_type(nombre))

        while True:
            opcion = console.input("\n[cyan]nº de clave a corregir (q para salir):[/cyan] ").strip()
            if opcion.lower() in {"q", "quit", "salir", ""}:
                break
            if not opcion.isdigit() or not 1 <= int(opcion) <= len(pendientes):
                console.print("[red]opción fuera de rango[/red]")
                continue
            fila = pendientes[int(opcion) - 1]
            console.print(f"[bold]{fila['key']}[/bold]  (ahora: {fila['category']} · {fila['flow_type']})")
            console.print(cat_menu)
            eleccion = console.input("[cyan]nº de categoría:[/cyan] ").strip()
            if not eleccion.isdigit() or not 1 <= int(eleccion) <= len(categorias):
                console.print("[red]opción fuera de rango, no se cambia nada[/red]")
                continue
            categoria = categorias[int(eleccion) - 1]
            flujo = console.input(
                f"[cyan]flow_type ({', '.join(taxonomy.flow_type_names)} "
                f"[Enter = {taxonomy.default_flow_type(categoria)}]):[/cyan] "
            ).strip()
            if flujo not in taxonomy.flow_types:
                flujo = taxonomy.default_flow_type(categoria)
            repo.upsert_merchant(
                conn,
                key=fila["key"],
                category=categoria,
                subcategory=None,
                flow_type=flujo,
                source="user",
                confidence=1.0,
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
            repo.propagate_to_transactions(
                conn,
                key=fila["key"],
                category=categoria,
                subcategory=None,
                flow_type=flujo,
                confidence=1.0,
            )
            console.print(
                f"[green]guardado[/green] {fila['key']} -> {categoria} ({flujo}) · source=user"
            )
    finally:
        conn.close()


@app.command(name="eval-clef")
def eval_clef(
    etiquetas: Path = typer.Option(
        Path("data/eval/merchants_labeled.csv"),
        "--labels",
        help="CSV con columnas key,category,flow_type (fuera de git).",
    ),
    config: Path = CONFIG_OPTION,
) -> None:
    """Mide al decider contra claves etiquetadas a mano. No escribe en merchants."""
    settings = _settings(config)
    try:
        taxonomy = load_taxonomy()
        llm = load_llm_settings(_llm_config_path(config))
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    if offline_enabled():
        console.print("[red]MOOONEY_OFFLINE=1: eval-clef necesita llamar al modelo[/red]")
        raise typer.Exit(code=1)

    try:
        labels = load_labels(etiquetas)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    console.print(f"[bold]{len(labels)}[/bold] claves etiquetadas en {etiquetas}")

    conn = repo.connect(settings.paths.db_path, check_same_thread=False)
    recorder = CallRecorder(conn)
    try:
        cliente = LLMClient(llm, recorder=recorder)
        decider = build_decider(llm, cliente)
        questions = build_questions(taxonomy)
        report = evaluate(
            conn, labels, decider, taxonomy, questions, max_workers=llm.max_concurrency
        )
    finally:
        recorder.flush()
        conn.close()

    table = Table(title="Calibración del decider")
    table.add_column("Confianza")
    table.add_column("Aciertos", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Precisión", justify="right")
    for nombre, _, _ in CALIBRATION_BINS:
        ok, n = report.cat_bins[nombre]
        tabla = f"{ok / n:.0%}" if n else "—"
        table.add_row(nombre, str(ok), str(n), tabla)
    console.print(table)

    if report.misses:
        mt = Table(title="Fallos de categoría")
        mt.add_column("Clave")
        mt.add_column("Esperada")
        mt.add_column("Dicha")
        mt.add_column("Conf.", justify="right")
        for clave, esperada, dicha, conf in report.misses:
            mt.add_row(clave[:40], esperada, dicha, f"{conf:.2f}")
        console.print(mt)

    if report.flow_misses:
        ft = Table(title="Fallos de flow_type")
        ft.add_column("Clave")
        ft.add_column("Esperado")
        ft.add_column("Dicho")
        for clave, esperado, dicho in report.flow_misses:
            ft.add_row(clave[:40], esperado, dicho)
        console.print(ft)

    for clave in report.missing:
        console.print(f"[yellow]clave no encontrada en la DB:[/yellow] {clave}")
    if report.errors:
        console.print(f"[yellow]{report.errors} llamadas fallidas (no cuentan)[/yellow]")

    cat_ok = report.category_accuracy() > 0.85
    flow_ok = report.flow_accuracy() > 0.95
    console.print(
        f"categoría: [bold]{report.category_accuracy():.1%}[/bold] (>85 % "
        f"{'[green]OK[/green]' if cat_ok else '[red]FALLO[/red]'}) · "
        f"flow_type: [bold]{report.flow_accuracy():.1%}[/bold] (>95 % "
        f"{'[green]OK[/green]' if flow_ok else '[red]FALLO[/red]'}) · "
        f"evaluadas: {report.evaluated}/{report.total}"
    )
    if not (cat_ok and flow_ok):
        raise typer.Exit(code=1)


# ---------- fase 3: analítica ----------


def _eur(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f} €"


def _delta(v: float | None) -> str:
    if v is None:
        return "—"
    color = "red" if v > 0 else "green"
    return f"[{color}]{v:+,.0f} €[/{color}]"


def _budgets_path(config: Path | None) -> Path:
    return (config.parent if config else Path("config")) / "budgets.yaml"


def _refresh_recurring(conn) -> int:
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return 0
    items = detect_recurring(conn, as_of)
    return store_recurring(conn, items, datetime.now().isoformat(timespec="seconds"))


@app.command()
def metrics(
    month: str = typer.Option(None, "--month", help="Mes YYYY-MM (por defecto, el del último movimiento)."),
    months: int = typer.Option(6, "--months", help="Meses de serie a mostrar."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Métricas del mes: ingresos, gastos, ahorro, fijo/variable y gasto por categoría."""
    settings = _settings(config)
    conn = repo.connect(settings.paths.db_path)
    try:
        _refresh_recurring(conn)
        try:
            report = build_report(conn, month, n_months=months)
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(code=1) from None
    finally:
        conn.close()

    s = report.summary
    aviso = "" if s.full_month else " [dim](mes incompleto)[/dim]"
    console.print(f"[bold]Mes {report.month}[/bold]{aviso} · datos hasta {report.as_of}")
    t = Table(title="Resumen del mes")
    t.add_column("Métrica")
    t.add_column("Valor", justify="right")
    t.add_row("Ingresos", _eur(s.income))
    t.add_row("Gastos", _eur(s.expenses))
    t.add_row("  fijos (recurrentes)", _eur(s.fixed_expenses))
    t.add_row("  variables", _eur(s.variable_expenses))
    t.add_row("Ahorro neto (ingresos - gastos)", _eur(s.net))
    t.add_row("Tasa de ahorro", "—" if s.savings_rate is None else f"{s.savings_rate:.0%}")
    t.add_row("Aportado a inversión", _eur(s.investment))
    t.add_row("Movimientos", str(s.n_tx))
    console.print(t)

    serie = Table(title=f"Últimos {len(report.series)} meses")
    for col in ("Mes", "Ingresos", "Gastos", "Fijos", "Ahorro", "Tasa", "Inversión"):
        serie.add_column(col, justify="right" if col != "Mes" else "left")
    for m in report.series:
        serie.add_row(
            m.month + ("" if m.full_month else "*"), _eur(m.income), _eur(m.expenses),
            _eur(m.fixed_expenses), _eur(m.net),
            "—" if m.savings_rate is None else f"{m.savings_rate:.0%}", _eur(m.investment),
        )
    console.print(serie)
    if any(not m.full_month for m in report.series):
        console.print("[dim]* mes incompleto en los datos[/dim]")

    cat = Table(title="Gasto por categoría")
    for col in ("Categoría", "Importe", "%", "Fijo", "vs mes ant.", "vs media"):
        cat.add_column(col, justify="right" if col != "Categoría" else "left")
    for c in report.categories:
        cat.add_row(c.category, _eur(c.amount), f"{c.pct:.0f} %", _eur(c.fixed_amount),
                    _delta(c.delta_prev), _delta(c.delta_avg))
    console.print(cat)
    n_hist = report.categories[0].n_prev_months if report.categories else 0
    console.print(f"[dim]vs media: {n_hist} meses completos anteriores[/dim]")


@app.command()
def recurring(
    all_items: bool = typer.Option(False, "--all", help="Incluye recurrentes inactivos."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Detecta cargos e ingresos recurrentes (periodo, importe, próxima fecha)."""
    settings = _settings(config)
    conn = repo.connect(settings.paths.db_path)
    try:
        n = _refresh_recurring(conn)
        rows = load_recurring(conn, active_only=not all_items)
    finally:
        conn.close()
    t = Table(title=f"Recurrentes ({len(rows)} de {n} detectados)")
    for col in ("Clave", "Categoría", "Periodo", "Importe", "Fijo", "Último", "Próximo", "Activo"):
        t.add_column(col)
    for r in rows:
        t.add_row(
            r["key"][:36], r["category"] or "—", r["period_label"],
            f"{r['amount_avg']:+,.2f} €", "sí" if r["amount_cv"] <= 0.15 else "varía",
            r["last_seen"], r["next_expected"], "sí" if r["active"] else "no",
        )
    console.print(t)


budget_app = typer.Typer(help="Presupuestos mensuales por categoría.")
app.add_typer(budget_app, name="budget")


@budget_app.command("init")
def budget_init(
    months: int = typer.Option(6, "--months", help="Meses completos de histórico para la mediana."),
    cut: float = typer.Option(0.10, "--cut", help="Recorte objetivo en categorías variables (0.10 = 10 %)."),
    force: bool = typer.Option(False, "--force", help="Sobrescribe config/budgets.yaml si existe."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Propone límites por categoría (mediana del histórico menos un recorte) y los guarda."""
    settings = _settings(config)
    path = _budgets_path(config)
    if path.exists() and not force:
        console.print(f"[yellow]{path} ya existe; usa --force para regenerarlo[/yellow]")
        raise typer.Exit(code=1)
    conn = repo.connect(settings.paths.db_path)
    try:
        _refresh_recurring(conn)
        as_of = periods.as_of_date(conn)
        if as_of is None:
            console.print("[red]no hay transacciones[/red]")
            raise typer.Exit(code=1)
        month = periods.month_of(as_of)
        proposal = budgets_mod.propose_budgets(
            conn, month, months=months, cut=cut, as_of=as_of, first=periods.first_date(conn)
        )
        if not proposal:
            console.print("[red]no hay meses completos suficientes para proponer presupuestos[/red]")
            raise typer.Exit(code=1)
        limits = {cat: v["limit"] for cat, v in proposal.items()}
        budgets_mod.save_budgets_config(
            path, limits, meta={"source": "proposal", "months": months, "cut": cut, "as_of": as_of}
        )
        budgets_mod.materialize_month(conn, month, limits, source="proposal")
    finally:
        conn.close()
    t = Table(title=f"Propuesta de presupuesto (mediana de {months} meses completos)")
    for col in ("Categoría", "Mediana", "% fijo", "Recorte", "Límite"):
        t.add_column(col, justify="right" if col != "Categoría" else "left")
    for cat, v in proposal.items():
        t.add_row(cat, _eur(v["median"]), f"{v['fixed_share']:.0%}",
                  f"{v['cut_applied']:.0%}", f"[bold]{_eur(v['limit'])}[/bold]")
    console.print(t)
    console.print(f"guardado en {path}; ajústalo a mano o con [bold]moooney budget set[/bold]")


@budget_app.command("set")
def budget_set(
    categoria: str = typer.Argument(..., help="Categoría tal como aparece en categories.yaml."),
    importe: float = typer.Argument(..., help="Límite mensual en euros."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Fija el límite de una categoría (para el mes actual y los siguientes)."""
    settings = _settings(config)
    try:
        taxonomy = load_taxonomy()
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None
    if categoria not in taxonomy.categories:
        console.print(f"[red]categoría desconocida: {categoria!r}[/red]")
        raise typer.Exit(code=1)
    path = _budgets_path(config)
    limits = budgets_mod.load_budgets_config(path)
    limits[categoria] = importe
    budgets_mod.save_budgets_config(path, limits, meta={"source": "user"})
    conn = repo.connect(settings.paths.db_path)
    try:
        as_of = periods.as_of_date(conn)
        if as_of:
            budgets_mod.materialize_month(
                conn, periods.month_of(as_of), {categoria: importe}, source="user"
            )
    finally:
        conn.close()
    console.print(f"[green]{categoria}: {importe:,.0f} €/mes[/green]")


@budget_app.command("show")
def budget_show(
    month: str = typer.Option(None, "--month", help="Mes YYYY-MM (por defecto, el actual)."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Gastado frente a presupuesto, con proyección a fin de mes."""
    settings = _settings(config)
    limits_cfg = budgets_mod.load_budgets_config(_budgets_path(config))
    conn = repo.connect(settings.paths.db_path)
    try:
        as_of = periods.as_of_date(conn)
        if as_of is None:
            console.print("[red]no hay transacciones[/red]")
            raise typer.Exit(code=1)
        month = month or periods.month_of(as_of)
        limits = budgets_mod.limits_for_month(conn, month, limits_cfg)
        if not limits:
            console.print("[yellow]sin presupuestos: ejecuta `moooney budget init`[/yellow]")
            raise typer.Exit(code=1)
        if month == periods.month_of(as_of) and limits_cfg:
            budgets_mod.materialize_month(conn, month, limits_cfg, source="config")
            limits = limits_cfg
        rows = budgets_mod.budget_status(conn, month, limits, as_of=as_of)
    finally:
        conn.close()
    t = Table(title=f"Presupuesto {month} (datos hasta {as_of})")
    for col in ("Categoría", "Límite", "Gastado", "%", "Restante", "Proyección"):
        t.add_column(col, justify="right" if col != "Categoría" else "left")
    for r in rows:
        color = "red" if r.pct >= 100 else "yellow" if r.pct >= 80 else "green"
        t.add_row(r.category, _eur(r.limit), _eur(r.spent), f"[{color}]{r.pct:.0f} %[/{color}]",
                  _eur(r.remaining), _eur(r.projected))
    console.print(t)


@app.command()
def alerts(
    ack: int = typer.Option(None, "--ack", help="Marca como vista la alerta con este id."),
    ack_all: bool = typer.Option(False, "--ack-all", help="Marca todas como vistas."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Detecta alertas (presupuesto, anomalías, duplicados, recurrentes, saldo, comisiones)."""
    settings = _settings(config)
    limits_cfg = budgets_mod.load_budgets_config(_budgets_path(config))
    conn = repo.connect(settings.paths.db_path)
    try:
        if ack is not None or ack_all:
            n = alerts_mod.acknowledge(conn, None if ack_all else ack)
            console.print(f"[green]{n} alerta(s) marcadas como vistas[/green]")
            return
        as_of = periods.as_of_date(conn)
        if as_of is None:
            console.print("[red]no hay transacciones[/red]")
            raise typer.Exit(code=1)
        _refresh_recurring(conn)
        month = periods.month_of(as_of)
        budget_rows = None
        if limits_cfg:
            limits = budgets_mod.limits_for_month(conn, month, limits_cfg)
            budget_rows = budgets_mod.budget_status(conn, month, limits, as_of=as_of)
        found = alerts_mod.run_alerts(conn, as_of, budget_rows, month)
        nuevas = alerts_mod.store_alerts(conn, found)
        pendientes = alerts_mod.pending_alerts(conn)
    finally:
        conn.close()
    console.print(f"{len(found)} alertas detectadas, [bold]{nuevas}[/bold] nuevas, "
                  f"{len(pendientes)} pendientes de ver (datos hasta {as_of})")
    if not pendientes:
        return
    t = Table(title="Alertas pendientes")
    for col in ("id", "Nivel", "Fecha", "Tipo", "Mensaje"):
        t.add_column(col)
    colores = {"critical": "red", "warning": "yellow", "info": "cyan"}
    for a in pendientes:
        c = colores.get(a["severity"], "white")
        t.add_row(str(a["id"]), f"[{c}]{a['severity']}[/{c}]", a["date"], a["type"], a["message"])
    console.print(t)
    console.print("[dim]moooney alerts --ack ID  ·  --ack-all[/dim]")


# ---------- fase 5: informe y vigilancia ----------


@app.command()
def report(
    month: str = typer.Option(None, "--month", help="Mes YYYY-MM (por defecto, el del último movimiento)."),
    no_model: bool = typer.Option(False, "--no-model", help="Sin modelo: solo los hallazgos deterministas."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Informe mensual con consejos (Markdown en reports/YYYY-MM.md y en la web)."""
    from moooney.web import service

    settings = _settings(config)
    paths = service.Paths(config.parent if config else Path("config"))
    service.job_report(settings, paths, lambda line: console.print(f"[dim]{line}[/dim]"),
                       month=month, use_model=not no_model)
    conn = repo.connect(settings.paths.db_path)
    try:
        from moooney.report.advice import latest_report

        m = month or periods.month_of(periods.as_of_date(conn) or "")
        row = latest_report(conn, m)
    finally:
        conn.close()
    if row:
        console.print(row["content"], markup=False, highlight=False)


@app.command()
def watch(
    interval: float = typer.Option(60.0, "--interval", help="Segundos entre comprobaciones."),
    once: bool = typer.Option(False, "--once", help="Comprueba una vez y sale (para tareas programadas)."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Vigila data/excel y corre la pipeline entera cuando aparece un Excel nuevo."""
    from moooney.ingest.watch import watch as watch_loop

    settings = _settings(config)
    config_dir = config.parent if config else Path("config")
    console.print(f"vigilando {settings.paths.excel_dir} cada {interval:.0f} s (Ctrl+C para parar)")
    try:
        runs = watch_loop(settings, config_dir, lambda line: console.print(line, markup=False),
                          interval=interval, once=once)
    except KeyboardInterrupt:
        console.print("parado")
        return
    if once:
        console.print(f"pipeline ejecutada {runs} vez/veces")


# ---------- fase 4: web ----------


@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Solo local por defecto. 0.0.0.0 para exponerlo en la red."),
    port: int = typer.Option(8765, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Abre el navegador al arrancar."),
    reload: bool = typer.Option(False, "--reload", help="Recarga automática (desarrollo)."),
    demo: bool = typer.Option(False, "--demo", help="Fuerza los datos de demostración aunque haya datos reales."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Interfaz web local. Sin datos reales muestra la demo (marcada como tal) hasta que importes."""
    import os
    import threading
    import webbrowser

    import uvicorn

    from moooney.web.app import create_app

    if config is not None:
        os.environ["MOOONEY_CONFIG"] = str(config)
    url = f"http://{host}:{port}/"
    if open_browser and not reload:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    console.print(f"[bold]moooney[/bold] en {url}  (Ctrl+C para parar)")
    if demo:
        os.environ["MOOONEY_FORCE_DEMO"] = "1"
    if reload:
        uvicorn.run("moooney.web.app:app_from_env", host=host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(create_app(config, force_demo=demo), host=host, port=port)


@app.command()
def demo(
    target: Path = typer.Option(Path("data/demo"), "--target", help="Carpeta de la demo (ignorada por git)."),
    serve: bool = typer.Option(False, "--serve", help="Arranca la web sobre la demo tras generarla."),
    port: int = typer.Option(8765, "--port"),
    open_browser: bool = typer.Option(True, "--open/--no-open"),
) -> None:
    """Genera una base sintética (sin datos reales) para probar la web y los comandos."""
    from moooney.demo import build_demo

    settings_path = build_demo(target)
    console.print(f"[green]demo generada[/green] en {target} · config: {settings_path}")
    console.print(f"pruébala con: [bold]uv run moooney web --config {settings_path}[/bold]")
    if serve:
        web(host="127.0.0.1", port=port, open_browser=open_browser, reload=False, config=settings_path)


@app.command()
def export(
    out: Path = typer.Option(Path("data/clean/movimientos.csv"), "--out", help="Fichero CSV de salida."),
    month: str | None = typer.Option(None, "--month", help="Solo un mes (YYYY-MM)."),
    category: str | None = typer.Option(None, "--category", help="Solo una categoría."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Exporta los movimientos categorizados a CSV (separador ;, UTF-8 con BOM, para Excel)."""
    from moooney.export import write_csv

    settings = _settings(config)
    conn = repo.connect(settings.paths.db_path)
    try:
        n = write_csv(conn, out, month=month, category=category)
    finally:
        conn.close()
    console.print(f"[green]{n} movimientos[/green] exportados a {out}")


@app.command()
def tag(
    movimiento: int | None = typer.Argument(None, help="Id del movimiento (lo ves en la web o en el CSV)."),
    etiqueta: str | None = typer.Argument(None, help="Etiqueta libre: viaje china, reforma, boda…"),
    remove: bool = typer.Option(False, "--remove", help="Quita la etiqueta en vez de ponerla."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Etiqueta movimientos sueltos para agruparlos (sin argumentos: lista las etiquetas)."""
    settings = _settings(config)
    conn = repo.connect(settings.paths.db_path)
    try:
        if movimiento is None or etiqueta is None:
            table = Table(title="Etiquetas")
            table.add_column("Etiqueta")
            table.add_column("Mov.", justify="right")
            table.add_column("Neto", justify="right")
            table.add_column("Último")
            for r in repo.tags_summary(conn):
                table.add_row(r["tag"], str(r["n"]), _eur(float(r["total"])), r["last_date"] or "")
            console.print(table)
            return
        if remove:
            ok = repo.remove_tag(conn, movimiento, etiqueta)
            console.print("[green]quitada[/green]" if ok else "[yellow]no tenía esa etiqueta[/yellow]")
        else:
            t = repo.add_tag(conn, movimiento, etiqueta, now=datetime.now().isoformat(timespec="seconds"))
            console.print(f"[green]etiquetado[/green] como «{t}»" if t else "[red]movimiento o etiqueta no válidos[/red]")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
