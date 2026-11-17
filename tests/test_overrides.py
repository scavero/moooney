"""Claves de transferencia por destinatario. Todo inventado."""

from pathlib import Path

from moooney.categorize.keys import rebuild_clean_keys
from moooney.db import repo

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def _cat(conn, tx_id):
    r = conn.execute("SELECT category, flow_type FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return r["category"], r["flow_type"]


def test_reconstruir_claves_divide_las_transferencias_por_destinatario(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    filas = [("2025-03-01", "TRANSFERENCIA A FAVOR DE Casero Inventado CONCEPTO: ALQUILER MARZO", -500.0),
             ("2025-04-01", "TRANSFERENCIA INMEDIATA A FAVOR DE Casero Inventado CONCEPTO ALQUILER ABRIL", -500.0),
             ("2025-03-05", "TRANSFERENCIA A FAVOR DE Yo Mismo Ficticio CONCEPTO: AHORRO", -200.0)]
    for i, (d, concept, amount) in enumerate(filas):
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount, account, flow_type, category,"
            " created_at) VALUES (?, ?, ?, 'TRANSFERENCIA A PARTICULAR', ?, 'c', 'expense', 'Vivienda', 't')",
            (f"f{i}", d, concept, amount))
    repo.upsert_merchant(conn, key="TRANSFERENCIA A PARTICULAR", category="Vivienda", subcategory=None,
                         flow_type="expense", source="user", confidence=1.0, updated_at="t")
    cambiadas, migradas = rebuild_clean_keys(conn)
    assert (cambiadas, migradas) == (3, 2)
    claves = [r[0] for r in conn.execute("SELECT DISTINCT concept_clean FROM transactions ORDER BY 1")]
    assert len(claves) == 2 and all(k.startswith("TRANSFERENCIA A PARTICULAR ") for k in claves)
    # las dos claves heredan la corrección del usuario: reconstruir no cambia ninguna categoría
    assert [tuple(r) for r in conn.execute("SELECT category, source FROM merchants")] == [("Vivienda", "user")] * 2
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category = 'Vivienda'").fetchone()[0] == 3
