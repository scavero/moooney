"""CLI de moooney: inspect, ingest y status. Solo agregados, nunca datos personales."""

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from moooney.config import Settings, load_settings
from moooney.db import repo
from moooney.ingest.load import ingest_directory
from moooney.ingest.parse_santander import ParseError, inspect_file

app = typer.Typer(help="Pipeline local de finanzas personales (fase 1: ingesta).")
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


if __name__ == "__main__":
    app()
