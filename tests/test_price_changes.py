"""Recibos que suben de precio. Importes inventados."""

import pytest

from moooney.analytics.alerts import price_change_alerts
from moooney.analytics.price_changes import detect_price_changes, find_step, find_trend
from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.db import repo
from tests.test_analytics import _monthly, _tx


def test_escalon():
    assert find_step([12.99, 12.99, 12.99, 15.99]) == (12.99, 15.99, 3)
    assert find_step([12.99, 12.99, 15.99, 15.99, 15.99]) == (12.99, 15.99, 2)  # sigue viéndose
    assert find_step([12.99] * 5) is None
    assert find_step([15.99, 15.99, 12.99]) is None  # bajada
    assert find_step([40.0, 55.0, 38.0, 61.0]) is None  # variable: el precio viejo no era estable
    assert find_step([10.0, 10.0, 10.1]) is None  # 1 %: ruido
    assert find_step([2.0, 2.0, 2.1]) is None  # 5 % pero 10 céntimos


def test_tendencia():
    assert find_trend([50, 52, 48, 51, 70, 75, 72]) == pytest.approx((50.25, 72.333), rel=1e-3)
    assert find_trend([50, 52, 48, 51, 53, 50, 52]) is None
    assert find_trend([50, 70, 75, 72]) is None  # poca historia


@pytest.fixture()
def db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    rows = _monthly("STREAMING FALSO", -12.99, "Suscripciones", "expense", 5, 8)
    rows = [(d, k, -15.99 if d >= "2025-06" else a, c, f) for d, k, a, c, f in rows]
    luz = [-50, -52, -48, -51, -55, -70, -75, -72]
    rows += [(d, k, luz[i], c, f) for i, (d, k, _a, c, f) in
             enumerate(sorted(_monthly("LUZ FALSA", -1, "Vivienda", "expense", 12, 8)))]
    rows += _monthly("GIMNASIO FIJO", -30.0, "Ocio", "expense", 3, 8)
    rows += [(d, "TARJETA", -a, "Tarjeta de credito", "card_settlement")
             for d, a in zip([f"2025-{m:02d}-01" for m in range(1, 8)], [300, 310, 290, 305, 600, 650, 700], strict=True)]
    _tx(conn, rows)
    store_recurring(conn, detect_recurring(conn, "2025-07-20"), "t")
    return conn


def test_detecta_escalon_y_recibo_al_alza(db):
    found = {p.key: p for p in detect_price_changes(db)}
    assert set(found) == {"STREAMING FALSO", "LUZ FALSA"}  # ni el gimnasio fijo ni la tarjeta
    s = found["STREAMING FALSO"]
    assert (s.kind, s.old, s.new, s.since, s.n_since) == ("step", 12.99, 15.99, "2025-06-05", 2)
    assert s.delta_year == pytest.approx(3 * 365 / s.period_days)
    luz = found["LUZ FALSA"]
    assert luz.kind == "trend" and luz.new > luz.old * 1.15
    assert list(found) == sorted(found, key=lambda k: -found[k].delta_year)


def test_una_alerta_por_subida(db):
    alerts = price_change_alerts(db)
    tipos = {a.type: a for a in alerts}
    assert set(tipos) == {"price_increase", "bill_rising"}
    assert tipos["price_increase"].dedup_key == "price_increase|STREAMING FALSO|2025-06-05"
    assert "al año" in tipos["price_increase"].message


def test_pagina_de_recurrentes(tmp_path, monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from moooney.demo import build_demo
    from moooney.web.app import create_app

    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=Path(__file__).resolve().parent.parent / "config",
                               months=6)
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/recurring")
        assert r.status_code == 200


def test_las_subidas_que_ya_no_se_cumplen_se_retiran(db):
    from moooney.analytics.alerts import Alert, store_alerts

    vieja = Alert(type="price_increase", severity="warning", date="2025-01-01",
                  message="FORMATO VIEJO: último cargo", dedup_key="price_increase|FORMATO VIEJO|2025-01-01")
    otra = Alert(type="bank_fee", severity="info", date="2025-01-01", message="X: comisión", dedup_key="bank_fee|X")
    store_alerts(db, [vieja, otra])
    store_alerts(db, price_change_alerts(db) + [otra])
    claves = {r[0] for r in db.execute("SELECT dedup_key FROM alerts")}
    assert "price_increase|FORMATO VIEJO|2025-01-01" not in claves
    assert "bank_fee|X" in claves and "price_increase|STREAMING FALSO|2025-06-05" in claves
