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
