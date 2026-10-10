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
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # El esquema es todo CREATE ... IF NOT EXISTS: aplicarlo siempre añade
    # tablas nuevas (p. ej. merchant_embeddings) a bases de datos ya creadas.
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


# ---------- merchants (memoria de claves de comercio) ----------

# Precedencia al recategorizar: user > rule > embedding > clef > llm.
SOURCE_PRIORITY = {"user": 5, "rule": 4, "embedding": 3, "clef": 2, "llm": 1}


def get_merchant(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM merchants WHERE key = ?", (key,)).fetchone()


def upsert_merchant(
    conn: sqlite3.Connection,
    *,
    key: str,
    category: str,
    subcategory: str,
    flow_type: str,
    source: str,
    confidence: float,
    updated_at: str,
) -> bool:
    """Guarda la clasificación de una clave respetando la precedencia de fuentes.

    Devuelve False si una fuente de mayor rango (p. ej. user) ya la fijó.
    """
    existing = get_merchant(conn, key)
    if existing is not None:
        prev = SOURCE_PRIORITY.get(existing["source"], 0)
        if prev > SOURCE_PRIORITY.get(source, 0):
            return False
    with conn:
        conn.execute(
            """
            INSERT INTO merchants (key, category, subcategory, flow_type, source, confidence, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                category = excluded.category,
                subcategory = excluded.subcategory,
                flow_type = excluded.flow_type,
                source = excluded.source,
                confidence = excluded.confidence,
                updated_at = excluded.updated_at
            """,
            (key, category, subcategory, flow_type, source, confidence, updated_at),
        )
    return True


def propagate_to_transactions(
    conn: sqlite3.Connection,
    *,
    key: str,
    category: str,
    subcategory: str,
    flow_type: str,
    confidence: float,
) -> int:
    """Aplica la categoría de una clave a todas sus transacciones. Devuelve filas."""
    with conn:
        cur = conn.execute(
            "UPDATE transactions SET category = ?, subcategory = ?, flow_type = ?, confidence = ?"
            " WHERE concept_clean = ?",
            (category, subcategory, flow_type, confidence, key),
        )
    return cur.rowcount


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
