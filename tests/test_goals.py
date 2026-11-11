"""Metas de ahorro y simulador. Movimientos inventados."""

from pathlib import Path

import pytest

from moooney.analytics import goals
from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.db import repo
from tests.test_analytics import _monthly, _tx


@pytest.fixture()
def db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    rows = _monthly("NOMINA", 2000.0, "Ingresos", "income", 1, 8)
    rows += _monthly("ALQUILER FALSO", -700.0, "Vivienda", "expense", 2, 8)
    rows += _monthly("STREAMING FALSO", -15.0, "Suscripciones", "expense", 5, 8)
    rows += _monthly("SUPER FALSO", -300.0, "Alimentacion", "expense", 10, 8)
    rows += [("2025-07-14", "ULTIMO", -1.0, "Otros", "expense")]  # julio a medias: fuera de la base
    _tx(conn, rows)
    store_recurring(conn, detect_recurring(conn, "2025-07-14"), "t")
    return conn


def test_base_con_meses_completos_y_fijo_separado(db):
    b = goals.baseline(db)
    assert len(b.months) == 6 and b.months[-1] == "2025-06"
    assert b.income == pytest.approx(2000) and b.net == pytest.approx(2000 - 1015)
    cats = {c.category: c for c in b.categories}
    assert cats["Vivienda"].fixed == pytest.approx(cats["Vivienda"].total, rel=0.05)  # el alquiler es fijo
    assert cats["Vivienda"].variable < 40
    assert {r.key for r in b.recurring} >= {"ALQUILER FALSO", "STREAMING FALSO"}


def test_plan():
    g = goals.Goal(1, "Viaje", 3000, 1000, "2026-01")
    p = goals.plan(g, 500, "2025-07-14")
    assert (p.months_to_goal, p.eta, p.months_left) == (4, "2025-11", 6)
    assert p.on_track and p.needed == pytest.approx(2000 / 6)
    assert not goals.plan(g, 200, "2025-07-14").on_track
    assert goals.plan(g, -50, "2025-07-14").months_to_goal is None
    assert goals.plan(goals.Goal(2, "Hecha", 100, 150, None), 10, "2025-07-14").months_to_goal == 0


def test_guardar_y_validar(db):
    gid = goals.add_goal(db, "  Viaje   de verano ", 3000, 500, "2099-06")
    assert [g.name for g in goals.list_goals(db)] == ["Viaje de verano"]
    goals.set_saved(db, gid, 800)
    assert goals.list_goals(db)[0].saved == 800
    for bad in [("", 10, 0, None), ("x", 0, 0, None), ("x", 10, 0, "2020-01"), ("x", 10, 0, "junio")]:
        with pytest.raises(ValueError):
            goals.add_goal(db, *bad)
    goals.delete_goal(db, gid)
    assert goals.list_goals(db) == []


def test_pagina(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from moooney.demo import build_demo
    from moooney.web.app import create_app

    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=Path(__file__).resolve().parent.parent / "config",
                               months=8)
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/goals")
        assert r.status_code == 200 and "¿Qué pasaría si…?" in r.text and "sim-cut" in r.text
        r = c.post("/goals", data={"name": "Colchón", "target": "5000", "saved": "100", "deadline": "2099-12"})
        assert r.status_code == 200 and "Colchón" in r.text and "llegas en" in r.text
        r = c.post("/goals", data={"name": "Mal", "target": "0"})
        assert "mayor que cero" in r.text
