"""Rellenado de claves normalizadas y utilidades de consulta por clave."""

import sqlite3

from moooney.categorize.normalize import clean_concept


def backfill_clean_keys(conn: sqlite3.Connection) -> int:
    """Calcula concept_clean donde falte. Devuelve cuántas filas actualizó."""
    rows = conn.execute(
        "SELECT id, concept FROM transactions WHERE concept_clean IS NULL OR concept_clean = ''"
    ).fetchall()
    with conn:
        conn.executemany(
            "UPDATE transactions SET concept_clean = ? WHERE id = ?",
            [(clean_concept(c), i) for i, c in rows],
        )
    return len(rows)


def rebuild_clean_keys(conn: sqlite3.Connection) -> tuple[int, int]:
    """Recalcula concept_clean de TODAS las filas (tras cambiar la normalización).

    Si una clave antigua tenía entrada en merchants y la nueva no, se copia la
    entrada (fuente y confianza incluidas): las correcciones del usuario no se
    pierden por renombrar la clave. Devuelve (filas_cambiadas, claves_migradas).
    """
    rows = conn.execute("SELECT id, concept, concept_clean FROM transactions").fetchall()
    cambios: list[tuple[str, int]] = []
    renombres: dict[str, str] = {}
    for r in rows:
        nueva = clean_concept(r["concept"])
        if nueva != r["concept_clean"]:
            cambios.append((nueva, r["id"]))
            if r["concept_clean"]:
                renombres.setdefault(r["concept_clean"], nueva)
    migradas = 0
    with conn:
        conn.executemany("UPDATE transactions SET concept_clean = ? WHERE id = ?", cambios)
        for vieja, nueva in renombres.items():
            old = conn.execute("SELECT * FROM merchants WHERE key = ?", (vieja,)).fetchone()
            if old is None:
                continue
            existe = conn.execute("SELECT 1 FROM merchants WHERE key = ?", (nueva,)).fetchone()
            if existe is None:
                conn.execute(
                    "INSERT INTO merchants (key, category, subcategory, flow_type, source,"
                    " confidence, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (nueva, old["category"], old["subcategory"], old["flow_type"],
                     old["source"], old["confidence"], old["updated_at"]),
                )
                migradas += 1
            conn.execute(
                "UPDATE transactions SET category = ?, subcategory = ?, flow_type = ?,"
                " confidence = ? WHERE concept_clean = ? AND category IS NULL",
                (old["category"], old["subcategory"], old["flow_type"], old["confidence"], nueva),
            )
    return len(cambios), migradas


def key_stats(conn: sqlite3.Connection) -> list[dict]:
    """Agregados por clave de comercio: nunca conceptos crudos sueltos."""
    cur = conn.execute(
        """
        SELECT t.concept_clean AS key, COUNT(*) AS n, SUM(t.amount) AS total,
               AVG(t.amount) AS avg_amount,
               m.category AS category, m.source AS source, m.confidence AS confidence
        FROM transactions t
        LEFT JOIN merchants m ON m.key = t.concept_clean
        WHERE t.concept_clean IS NOT NULL
        GROUP BY t.concept_clean
        ORDER BY n DESC, total ASC
        """
    )
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]
