"""Acceso a SQLite con la biblioteca estándar, sin ORM."""

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 1


def connect(db_path: Path) -> sqlite3.Connection:
    """Abre (o crea) la base de datos y aplica el esquema si no existe."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    table = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='imports'"
    ).fetchone()
    if table is None:
        schema = files("moooney.db").joinpath("schema.sql").read_text(encoding="utf-8")
        conn.executescript(schema)
        conn.commit()
    return conn


# ---------- imports ----------


def get_import_by_hash(conn: sqlite3.Connection, sha256: str) -> sqlite3.Row | None:
    cur = conn.execute("SELECT * FROM imports WHERE sha256 = ?", (sha256,))
    row = cur.fetchone()
    return row


def insert_import(
    conn: sqlite3.Connection,
    *,
    sha256: str,
    filename: str,
    imported_at: str,
    rows_total: int,
    rows_new: int,
    date_min: str | None,
    date_max: str | None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO imports (sha256, filename, imported_at, rows_total, rows_new, date_min, date_max)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (sha256, filename, imported_at, rows_total, rows_new, date_min, date_max),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------- transactions ----------


def insert_transactions(
    conn: sqlite3.Connection, rows: list[dict], import_id: int
) -> int:
    """Inserta transacciones ignorando huellas repetidas. Devuelve cuántas entraron."""
    inserted = 0
    for r in rows:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO transactions
                (fingerprint, date, value_date, concept, amount, balance, account,
                 import_id, created_at)
            VALUES (:fingerprint, :date, :value_date, :concept, :amount, :balance,
                    :account, :import_id, :created_at)
            """,
            {**r, "import_id": import_id},
        )
        inserted += cur.rowcount
    conn.commit()
    return inserted


# ---------- status ----------


def get_status(conn: sqlite3.Connection) -> dict:
    """Solo agregados; nunca filas individuales."""
    tx = conn.execute(
        "SELECT COUNT(*) AS n, MIN(date) AS dmin, MAX(date) AS dmax,"
        " SUM(CASE WHEN category IS NULL THEN 1 ELSE 0 END) AS sin_cat"
        " FROM transactions"
    ).fetchone()
    im = conn.execute(
        "SELECT COUNT(*) AS n, MAX(imported_at) AS last FROM imports"
    ).fetchone()
    return {
        "transactions": tx[0] or 0,
        "date_min": tx[1],
        "date_max": tx[2],
        "uncategorized": tx[3] or 0,
        "imports": im[0] or 0,
        "last_import_at": im[1],
    }
