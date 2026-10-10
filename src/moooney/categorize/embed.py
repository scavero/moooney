"""Embeddings (bge-m3) para heredar categorías entre claves parecidas.

Si una clave nueva tiene similitud coseno > 0.92 con una clave ya clasificada
por user o rule, hereda su categoría con source=embedding. Las claves ya
clasificadas por Clef o LLM no sirven de referencia: podrían arrastrar errores.
"""

import sqlite3
from collections.abc import Sequence

import numpy as np

from moooney.llm.anonymize import assert_safe_state
from moooney.llm.client import LLMClient

DEFAULT_THRESHOLD = 0.92
LABELED_SOURCES = ("user", "rule")


def _to_blob(vector: Sequence[float]) -> bytes:
    return np.asarray(vector, dtype=np.float32).tobytes()


def _from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


EMBED_BATCH = 64


def embed_keys(
    client: LLMClient, model: str, keys: Sequence[str]
) -> dict[str, np.ndarray]:
    """Embedde en lotes de EMBED_BATCH llamadas. Las claves ya pasan por anonymize."""
    safe = [assert_safe_state(key) for key in keys]
    out: dict[str, np.ndarray] = {}
    for i in range(0, len(safe), EMBED_BATCH):
        batch = safe[i : i + EMBED_BATCH]
        vectors = client.embed(model=model, texts=batch, purpose="embedding")
        for k, v in zip(batch, vectors, strict=True):
            out[k] = np.asarray(v, dtype=np.float32)
    return out


def store_embedding(
    conn: sqlite3.Connection, *, key: str, model: str, vector: np.ndarray, updated_at: str
) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO merchant_embeddings (key, model, vector, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET model = excluded.model,
                vector = excluded.vector, updated_at = excluded.updated_at
            """,
            (key, model, _to_blob(vector), updated_at),
        )


def get_embedding(conn: sqlite3.Connection, key: str, model: str) -> np.ndarray | None:
    row = conn.execute(
        "SELECT vector FROM merchant_embeddings WHERE key = ? AND model = ?", (key, model)
    ).fetchone()
    return _from_blob(row["vector"]) if row else None


def find_similar_labeled(
    conn: sqlite3.Connection,
    vector: np.ndarray,
    *,
    model: str | None = None,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[sqlite3.Row, float] | None:
    """Clave más parecida clasificada por user o rule, si supera el umbral."""
    if model is None:
        where = "m.source IN ('user', 'rule')"
        params: tuple = ()
    else:
        where = "m.source IN ('user', 'rule') AND e.model = ?"
        params = (model,)
    rows = conn.execute(
        f"""
        SELECT m.*, e.vector AS vec FROM merchant_embeddings e
        JOIN merchants m ON m.key = e.key
        WHERE {where}
        """,
        params,
    ).fetchall()
    best: tuple[sqlite3.Row, float] | None = None
    for row in rows:
        sim = cosine(vector, _from_blob(row["vec"]))
        if best is None or sim > best[1]:
            best = (row, sim)
    if best is not None and best[1] >= threshold:
        return best
    return None
