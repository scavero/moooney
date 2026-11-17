"""Movimientos apartados (borrados o duplicados). Todo inventado, generado en tmp_path."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moooney.db import repo
from moooney.demo import build_demo
from moooney.ingest.load import ingest_directory
from moooney.ingest.reset import reset_data
from moooney.web.app import create_app
from tests.conftest import make_santander_xlsx
from tests.test_overlap import CARD_ROWS, make_card_xlsx


def _n(conn, table="transactions"):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_apartado_no_vuelve_al_reimportar_y_se_restaura(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_santander_xlsx(d / "a.xlsx")
    [o] = ingest_directory(d, conn, settings)
    total = o.rows_new
    tx = conn.execute("SELECT id, fingerprint FROM transactions ORDER BY id LIMIT 1").fetchone()
    with pytest.raises(ValueError):
        repo.remove_transaction(conn, tx["id"], "porque sí", now="t")
    assert repo.remove_transaction(conn, tx["id"], "borrado", now="t")
    assert not repo.remove_transaction(conn, tx["id"], "borrado", now="t")  # ya no está
    assert _n(conn) == total - 1 and _n(conn, "transactions_removed") == 1
    # el mismo extracto con otro nombre (otra huella de fichero) se vuelve a leer: lo apartado no vuelve
    make_santander_xlsx(d / "b.xlsx", sheet="Otra hoja")
    outs = ingest_directory(d, conn, settings)
    assert sum(o.rows_new for o in outs if not o.skipped) == 0 and _n(conn) == total - 1
    [r] = repo.removed_transactions(conn)
    assert r["reason"] == "borrado" and r["fingerprint"] == tx["fingerprint"]
    assert repo.restore_transaction(conn, r["id"]) and not repo.restore_transaction(conn, r["id"])
    assert _n(conn) == total and _n(conn, "transactions_removed") == 0
    # Empezar de cero también vacía los apartados
    repo.remove_transaction(conn, conn.execute("SELECT id FROM transactions LIMIT 1").fetchone()[0], "duplicado", now="t")
    reset_data(conn, settings)
    assert _n(conn, "transactions_removed") == 0


def test_duplicado_de_tarjeta_apartado_no_vuelve(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_card_xlsx(d / "a.xlsx")  # trae dos compras iguales el mismo día
    ingest_directory(d, conn, settings)
    dup = conn.execute("SELECT id FROM transactions WHERE fingerprint LIKE '%#2'").fetchone()[0]
    repo.remove_transaction(conn, dup, "duplicado", now="t")
    make_card_xlsx(d / "b.xlsx", rows=[("07/11/2026", "OTRA TIENDA INVENTADA", "-10,00€", "EUR"), *CARD_ROWS])
    outs = ingest_directory(d, conn, settings)
    assert [o.rows_new for o in outs if not o.skipped] == [1]
    assert conn.execute("SELECT COUNT(*) FROM transactions WHERE amount = -4.99").fetchone()[0] == 1


def test_web_borrar_y_restaurar(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=Path(__file__).parent.parent / "config", months=3)
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    total = _n(conn)
    tx_id = conn.execute("SELECT id FROM transactions ORDER BY date DESC, id DESC LIMIT 1").fetchone()[0]
    conn.close()
    with TestClient(create_app(settings_path)) as c:
        page = c.get("/transactions").text
        assert f'action="/transactions/{tx_id}/remove"' in page and 'id="apartados"' not in page
        assert c.post(f"/transactions/{tx_id}/remove", data={"reason": "otro"}).status_code == 400
        r = c.post(f"/transactions/{tx_id}/remove", data={"reason": "duplicado", "next": "https://ejemplo.invalid/"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/transactions"  # solo vuelve a Movimientos
        assert c.post(f"/transactions/{tx_id}/remove", data={"reason": "duplicado"}).status_code == 404
        page = c.get("/transactions").text
        assert 'id="apartados"' in page and "1 movimiento apartado " in page and f'id="tx-{tx_id}"' not in page
        assert c.post("/transactions/removed/1/restore", data={"next": "/transactions?q=x"},
                      follow_redirects=False).headers["location"] == "/transactions?q=x"
        assert 'id="apartados"' not in c.get("/transactions").text
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    assert _n(conn) == total
