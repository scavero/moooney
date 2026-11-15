"""Categoría de un solo movimiento y claves de transferencia por destinatario. Todo inventado."""

from pathlib import Path

from fastapi.testclient import TestClient

from moooney.categorize.keys import rebuild_clean_keys
from moooney.categorize.taxonomy import load_taxonomy
from moooney.db import repo
from moooney.demo import build_demo
from moooney.web import service
from moooney.web.app import create_app
from tests.test_analytics import _tx

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def _cat(conn, tx_id):
    r = conn.execute("SELECT category, flow_type FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    return r["category"], r["flow_type"]


def test_categoria_de_un_solo_movimiento_sobrevive_a_corregir_el_comercio(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    tax = load_taxonomy(REPO_CONFIG / "categories.yaml")
    _tx(conn, [("2025-03-01", "TIENDA FALSA", -10.0, "Compras", "expense"),
               ("2025-03-02", "TIENDA FALSA", -20.0, "Compras", "expense")])
    a, b = (r[0] for r in conn.execute("SELECT id FROM transactions ORDER BY id"))
    assert service.set_transaction_category(conn, tax, a, "Vivienda")
    assert _cat(conn, a) == ("Vivienda", tax.default_flow_type("Vivienda")) and _cat(conn, b)[0] == "Compras"
    assert repo.overrides_for(conn, [a, b]) == {a}
    # corregir el comercio cambia el otro movimiento, no el que tiene categoría propia
    service.set_merchant_category(conn, tax, "TIENDA FALSA", "Ocio", None)
    assert _cat(conn, a)[0] == "Vivienda" and _cat(conn, b)[0] == "Ocio"
    # quitarla lo devuelve a la de su comercio
    assert service.set_transaction_category(conn, tax, a, "")
    assert _cat(conn, a)[0] == "Ocio" and repo.overrides_for(conn, [a]) == set()
    assert not service.set_transaction_category(conn, tax, 9999, "Vivienda")


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
    ahorro = conn.execute("SELECT id FROM transactions WHERE amount = -200").fetchone()[0]
    repo.set_override(conn, ahorro, category="Transferencias", subcategory=None, flow_type="transfer_internal", now="t")
    cambiadas, migradas = rebuild_clean_keys(conn)
    assert (cambiadas, migradas) == (3, 2)
    claves = [r[0] for r in conn.execute("SELECT DISTINCT concept_clean FROM transactions ORDER BY 1")]
    assert len(claves) == 2 and all(k.startswith("TRANSFERENCIA A PARTICULAR ") for k in claves)
    # las dos claves heredan la corrección del usuario: reconstruir no cambia ninguna categoría
    assert [tuple(r) for r in conn.execute("SELECT category, source FROM merchants")] == [("Vivienda", "user")] * 2
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE category = 'Vivienda'").fetchone()[0] == 2
    assert _cat(conn, ahorro) == ("Transferencias", "transfer_internal")  # la categoría propia se respeta


def test_web_categoria_solo_de_este_movimiento(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    tx = conn.execute("SELECT id, category, concept_clean FROM transactions ORDER BY date DESC, id DESC LIMIT 1").fetchone()
    otra = "Vivienda" if tx["category"] != "Vivienda" else "Ocio"
    conn.close()
    with TestClient(create_app(settings_path)) as c:
        assert "categoría solo de este movimiento" not in c.get("/transactions").text
        assert c.post(f"/transactions/{tx['id']}/category", data={"category": "No existe"}).status_code == 400
        r = c.post(f"/transactions/{tx['id']}/category", data={"category": otra, "next": "/transactions?q=x"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/transactions?q=x"
        assert "categoría solo de este movimiento" in c.get("/transactions").text
        c.post(f"/transactions/{tx['id']}/category", data={"category": ""})
        assert "categoría solo de este movimiento" not in c.get("/transactions").text
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    assert _cat(conn, tx["id"])[0] == tx["category"]
