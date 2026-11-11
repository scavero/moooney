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
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Cambios sobre tablas ya creadas (CREATE IF NOT EXISTS no añade columnas)."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    if "dedup_key" not in cols:
        conn.execute("ALTER TABLE alerts ADD COLUMN dedup_key TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_dedup ON alerts(dedup_key)"
    )


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
    force: bool = False,
) -> bool:
    """Guarda la clasificación de una clave respetando la precedencia de fuentes.

    Devuelve False si una fuente de mayor rango (p. ej. user) ya la fijó. `force`
    salta la precedencia (reintentos y segunda opinión sobre claves de un modelo),
    pero nunca sobre una clave corregida por el usuario.
    """
    existing = get_merchant(conn, key)
    if existing is not None:
        prev = SOURCE_PRIORITY.get(existing["source"], 0)
        if existing["source"] == "user" and source != "user":
            return False
        if not force and prev > SOURCE_PRIORITY.get(source, 0):
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
    if flow_type == "card_settlement":
        mark_card_settlements(conn)
    apply_reimbursements(conn)  # una devolución emparejada no vuelve a ser ingreso
    return cur.rowcount


def apply_reimbursements(conn: sqlite3.Connection) -> int:
    """Los ingresos emparejados con un gasto (gastos compartidos, analytics/shared.py) cuentan
    como gasto en positivo de la categoría de ese gasto. Idempotente."""
    with conn:
        cur = conn.execute(
            """UPDATE transactions SET flow_type = 'expense', category = e.category,
                   subcategory = e.subcategory, confidence = 1.0
               FROM reimbursements r JOIN transactions e ON e.id = r.expense_id
               WHERE transactions.id = r.income_id
                 AND (transactions.flow_type IS NOT 'expense' OR transactions.category IS NOT e.category
                      OR transactions.subcategory IS NOT e.subcategory)"""
        )
    return cur.rowcount


# ---------- tarjeta de crédito: compras sueltas frente a la liquidación ----------

CARD_ACCOUNT_PREFIX = "tarjeta"  # cuentas de extractos de tarjeta (perfil con account: card)
COVERED_FLOW = "card_settlement_covered"


def mark_card_settlements(conn: sqlite3.Connection) -> int:
    """Las liquidaciones de tarjeta cuyas compras ya están importadas dejan de ser gasto.

    Una liquidación del día D paga, más o menos, el mes anterior. Está cubierta si algún
    extracto de tarjeta abarca desde D-30 hasta D-3: entonces su flujo pasa a
    `card_settlement_covered` (fuera de los gastos) y cuentan las compras una a una. Sin
    extracto de tarjeta para ese periodo sigue siendo `card_settlement` y cuenta como gasto,
    así que no se pierde nada. Se recalcula entera cada vez: es idempotente.
    """
    from datetime import date, timedelta

    rangos = conn.execute(
        "SELECT MIN(date) AS lo, MAX(date) AS hi FROM transactions WHERE account LIKE ? GROUP BY account",
        (CARD_ACCOUNT_PREFIX + "%",),
    ).fetchall()
    filas = conn.execute(
        "SELECT id, date, flow_type FROM transactions WHERE flow_type IN ('card_settlement', ?)",
        (COVERED_FLOW,),
    ).fetchall()
    cambios = []
    for f in filas:
        d = date.fromisoformat(f["date"])
        desde, hasta = (d - timedelta(days=30)).isoformat(), (d - timedelta(days=3)).isoformat()
        cubierta = any(r["lo"] <= desde and r["hi"] >= hasta for r in rangos)
        quiero = COVERED_FLOW if cubierta else "card_settlement"
        if quiero != f["flow_type"]:
            cambios.append((quiero, f["id"]))
    if cambios:
        with conn:
            conn.executemany("UPDATE transactions SET flow_type = ? WHERE id = ?", cambios)
    return len(cambios)


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


MAX_TAG_CHARS = 40


def normalize_tag(tag: str) -> str:
    """'  Viaje  China ' → 'viaje china'. Vacía si no queda nada útil."""
    s = " ".join(str(tag or "").strip().lower().split())
    return s[:MAX_TAG_CHARS].strip(" ,;|")


def add_tag(conn: sqlite3.Connection, transaction_id: int, tag: str, *, now: str) -> str | None:
    """Añade una etiqueta a un movimiento. Devuelve la etiqueta normalizada o None si no vale."""
    t = normalize_tag(tag)
    if not t:
        return None
    exists = conn.execute("SELECT 1 FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if exists is None:
        return None
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO transaction_tags (transaction_id, tag, created_at) VALUES (?, ?, ?)",
            (transaction_id, t, now),
        )
    return t


def remove_tag(conn: sqlite3.Connection, transaction_id: int, tag: str) -> bool:
    with conn:
        cur = conn.execute(
            "DELETE FROM transaction_tags WHERE transaction_id = ? AND tag = ?",
            (transaction_id, normalize_tag(tag)),
        )
    return cur.rowcount > 0


def tags_for(conn: sqlite3.Connection, transaction_ids: list[int]) -> dict[int, list[str]]:
    """{id: [etiquetas]} para un conjunto de movimientos (una consulta)."""
    out: dict[int, list[str]] = {}
    if not transaction_ids:
        return out
    ph = ",".join("?" for _ in transaction_ids)
    rows = conn.execute(
        f"SELECT transaction_id, tag FROM transaction_tags WHERE transaction_id IN ({ph}) ORDER BY tag",
        list(transaction_ids),
    ).fetchall()
    for r in rows:
        out.setdefault(int(r["transaction_id"]), []).append(r["tag"])
    return out


def tags_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cada etiqueta con cuántos movimientos agrupa y su suma neta."""
    return conn.execute(
        """
        SELECT g.tag, COUNT(*) AS n, COALESCE(SUM(t.amount), 0) AS total, MAX(t.date) AS last_date
        FROM transaction_tags g JOIN transactions t ON t.id = g.transaction_id
        GROUP BY g.tag ORDER BY MAX(t.date) DESC, g.tag
        """
    ).fetchall()


def accounts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Cuentas presentes, con movimientos, rango y último saldo conocido de cada una."""
    return conn.execute(
        """
        SELECT t.account, COUNT(*) AS n, MIN(t.date) AS first_date, MAX(t.date) AS last_date,
               (SELECT balance FROM transactions b WHERE b.account = t.account AND b.balance IS NOT NULL
                ORDER BY b.date DESC, b.id DESC LIMIT 1) AS last_balance
        FROM transactions t GROUP BY t.account ORDER BY n DESC
        """
    ).fetchall()


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
