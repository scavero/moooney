"""Empezar de cero con los movimientos (decisión 2026-11-08).

Se borra todo lo que sale de los extractos: movimientos, importaciones, etiquetas, alertas,
recurrentes e informes, los Excel de la carpeta de entrada y sus CSV limpios. Los Excel se borran
porque, si se quedasen, la vigilancia de la carpeta los volvería a importar en el acto.

Se conserva lo que no depende de un extracto concreto: la memoria de comercios (categorías
aprendidas y correcciones a mano, así al volver a importar todo sale categorizado sin llamar al
modelo), sus embeddings y enriquecimiento, los presupuestos, la configuración (`config/`,
`data/.env`) y los registros de llamadas a modelos y a la web.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from moooney.config import Settings
from moooney.db.backup import make_backup
from moooney.ingest.detect import list_excel_files

# Orden: primero lo que apunta a transactions, después transactions e imports.
TABLES = ["transaction_tags", "reimbursements", "reimbursement_dismissed", "alerts", "transactions", "imports", "recurring", "reports"]
CONFIRM_WORD = "BORRAR"


@dataclass
class ResetOutcome:
    movimientos: int
    importaciones: int
    excel: int
    csv: int
    informes: int
    copia: Path | None = None  # copia de la base hecha justo antes de borrar


def summary(conn: sqlite3.Connection, settings: Settings) -> dict[str, int]:
    """Lo que se borraría, para enseñarlo antes de confirmar."""
    q = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
    return {
        "movimientos": q("transactions"),
        "importaciones": q("imports"),
        "excel": len(list_excel_files(settings.paths.excel_dir)) if settings.paths.excel_dir.exists() else 0,
    }


def reset_data(conn: sqlite3.Connection, settings: Settings) -> ResetOutcome:
    """Borra movimientos y extractos. Antes guarda una copia de la base (motivo «antes-de-borrar»):
    los movimientos se pueden recuperar con `moooney backup --restore`; los Excel borrados, no."""
    copia = None
    if settings.paths.db_path.exists():
        copia = make_backup(settings.paths.db_path, settings.paths.backups_dir, reason="antes-de-borrar")
    reports_dir = settings.paths.reports_dir
    excel_dir, clean_dir = settings.paths.excel_dir, settings.paths.clean_dir
    # Primero los Excel: si la vigilancia da una vuelta a mitad, ya no encuentra nada que importar.
    excel = list_excel_files(excel_dir) if excel_dir.exists() else []
    for f in excel:
        f.unlink(missing_ok=True)
    csvs = list(clean_dir.glob("*.csv")) if clean_dir.exists() else []
    for f in csvs:
        f.unlink(missing_ok=True)
    md = list(reports_dir.glob("*.md")) if reports_dir.exists() else []
    for f in md:
        f.unlink(missing_ok=True)

    movimientos = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    importaciones = conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
    with conn:
        for t in TABLES:
            conn.execute(f"DELETE FROM {t}")
    conn.execute("VACUUM")  # que el fichero de la base no siga guardando lo borrado
    return ResetOutcome(movimientos, importaciones, len(excel), len(csvs), len(md), copia)
