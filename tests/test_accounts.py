"""Varias cuentas en la misma base: saldo total, filtro por cuenta y etiquetas (sintético)."""

from moooney.analytics.forecast import last_balance
from moooney.db import repo
from moooney.web.queries import transactions_page, transactions_totals


def _db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    rows = [
        ("2026-10-01", "A UNO", "****1111", -10.0, 990.0),
        ("2026-10-05", "A DOS", "****1111", -20.0, 970.0),
        ("2026-10-03", "B UNO", "bbva", -5.0, 495.0),
        ("2026-10-02", "B CERO", "bbva", 500.0, 500.0),
    ]
    with conn:
        for i, (d, c, acc, a, b) in enumerate(rows):
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount, balance, account, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-10-11T00:00:00')",
                (f"fp{i}", d, c, c, a, b, acc),
            )
    return conn


def test_saldo_total_es_la_suma_del_ultimo_saldo_de_cada_cuenta(tmp_path):
    conn = _db(tmp_path)
    assert last_balance(conn) == 970.0 + 495.0
    cuentas = {r["account"]: r for r in repo.accounts(conn)}
    assert cuentas["****1111"]["last_balance"] == 970.0 and cuentas["bbva"]["last_balance"] == 495.0
    assert cuentas["****1111"]["n"] == 2 and cuentas["bbva"]["first_date"] == "2026-10-02"


def test_filtro_por_cuenta_y_etiqueta(tmp_path):
    conn = _db(tmp_path)
    assert transactions_page(conn, account="bbva").total == 2
    assert transactions_totals(conn, account="****1111")["expenses"] == 0.0  # sin flow_type no cuenta como gasto
    ids = [r["id"] for r in transactions_page(conn).rows]
    assert repo.add_tag(conn, ids[0], "Reforma", now="2026-10-11T00:00:00") == "reforma"
    assert repo.add_tag(conn, ids[1], "reforma", now="2026-10-11T00:00:00") == "reforma"
    assert repo.add_tag(conn, ids[1], "   ", now="x") is None
    assert transactions_page(conn, tag="REFORMA").total == 2
    assert repo.tags_summary(conn)[0]["n"] == 2
    assert repo.remove_tag(conn, ids[1], "reforma") and transactions_page(conn, tag="reforma").total == 1
    assert repo.tags_for(conn, ids) == {ids[0]: ["reforma"]}
