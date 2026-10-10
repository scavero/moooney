"""Acceso a SQLite con la biblioteca estándar, sin ORM."""

import sqlite3
from importlib.resources import files
from pathlib import Path

SCHEMA_VERSION = 1


def connect(db_path: Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Abre (o crea) la base de datos y aplica el esquema si no existe."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # El esquema es todo CREATE ... IF NOT EXISTS: aplicarlo siempre añade
    # tablas nuevas (p. ej. merchant_embeddings) a bases de datos ya creadas.
    # La caché de embeddings cambió de definición (se quito la FK); es datos
    # reconstructibles, asi que se recrea sin perder nada importante.
    old = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'merchant_embeddings'"
    ).fetchone()
    if old is not None and "REFERENCES" in (old["sql"] or ""):
        conn.execute("DROP TABLE merchant_embeddings")
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

# Precedencia al recategorizar: user > rule > embedding > clef_web > clef > llm.
SOURCE_PRIORITY = {"user": 6, "rule": 5, "embedding": 4, "clef_web": 3, "clef": 2, "llm": 1}


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


# ---------- enriquecimiento externo (fase 2.5) ----------


def get_enrichment(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM merchant_enrichment WHERE key = ?", (key,)).fetchone()


def upsert_enrichment(
    conn: sqlite3.Connection,
    *,
    key: str,
    provider: str | None,
    query: str,
    summary: str | None,
    place_types: str | None,
    url: str | None,
    helped: int | None,
    fetched_at: str,
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO merchant_enrichment
                (key, provider, query, summary, place_types, url, helped, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                provider = excluded.provider, query = excluded.query,
                summary = excluded.summary, place_types = excluded.place_types,
                url = excluded.url, helped = excluded.helped, fetched_at = excluded.fetched_at
            """,
            (key, provider, query, summary, place_types, url, helped, fetched_at),
        )


def set_enrichment_helped(conn: sqlite3.Connection, key: str, helped: int) -> None:
    with conn:
        conn.execute("UPDATE merchant_enrichment SET helped = ? WHERE key = ?", (helped, key))


def insert_web_call(
    conn: sqlite3.Connection,
    *,
    ts: str,
    provider: str,
    query: str,
    status: str,
    payload_hash: str | None,
) -> int:
    cur = conn.execute(
        "INSERT INTO web_calls (ts, provider, query, status, payload_hash) VALUES (?, ?, ?, ?, ?)",
        (ts, provider, query, status, payload_hash),
    )
    conn.commit()
    return int(cur.lastrowid)


# ---------- llm_calls ----------


def insert_llm_call(
    conn: sqlite3.Connection,
    *,
    ts: str,
    model: str,
    purpose: str | None,
    payload_hash: str | None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO llm_calls (ts, model, purpose, input_tokens, output_tokens, payload_hash)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (ts, model, purpose, input_tokens, output_tokens, payload_hash),
    )
    conn.commit()
    return int(cur.lastrowid)


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
