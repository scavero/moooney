"""CLI de moooney: ingesta, categorización, enriquecimiento y revisión."""

from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

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
        return load_settings(config)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1) from None


@app.command()
def inspect(
    ruta: Path = typer.Argument(..., help="Fichero .xlsx a comprobar."),
    config: Path = CONFIG_OPTION,
) -> None:
    """Comprueba si el parser entiende un Excel. No muestra conceptos ni importes."""
    settings = _settings(config)
    try:
        result = inspect_file(ruta, settings.parser)
    except ParseError as e:
        console.print(f"[red]No se ha podido interpretar {ruta.name}: {e}[/red]")
        raise typer.Exit(code=1) from None

    df = result.df
    assert df is not None
    console.print(f"[bold]Hoja:[/bold] {result.sheet_name}")
    console.print(f"[bold]Fila de cabecera:[/bold] {result.header_row}")
    console.print(f"[bold]Cuenta:[/bold] {'IBAN detectado' if result.account_found else 'principal'}")

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


if __name__ == "__main__":
    app()
