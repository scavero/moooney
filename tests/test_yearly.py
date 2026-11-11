"""Resumen anual. Movimientos inventados."""

from pathlib import Path

import pytest

from moooney.analytics import yearly
from moooney.db import repo
from tests.test_analytics import _tx


@pytest.fixture()
def db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    rows = []
    for y, alquiler, ocio in [(2024, -600.0, -50.0), (2025, -650.0, -80.0)]:
        for m in range(1, 13):
            if y == 2025 and m > 6:
                break
            rows += [(f"{y}-{m:02d}-01", "NOMINA", 2000.0, "Ingresos", "income"),
                     (f"{y}-{m:02d}-02", "ALQUILER FALSO", alquiler, "Vivienda", "expense"),
                     (f"{y}-{m:02d}-20", "CINE FALSO", ocio, "Ocio", "expense")]
    rows.append(("2025-03-15", "VIAJE FALSO", -900.0, "Viajes", "expense"))
    rows.append(("2024-08-10", "VIAJE FALSO", -500.0, "Viajes", "expense"))  # después del corte: no cuenta
    _tx(conn, rows)
    return conn


def test_compara_con_el_mismo_periodo(db):
    y = yearly.year_summary(db, 2025)
    assert y.partial and y.cutoff == "2025-06-20"
    assert y.totals.income == 12000 and y.prev_totals.income == 12000
    assert y.totals.expenses == pytest.approx(6 * 730 + 900)
    assert y.prev_totals.expenses == pytest.approx(6 * 650)  # hasta el 20 de junio de 2024, sin el viaje de agosto
    cats = {c.category: c for c in y.categories}
    assert cats["Viajes"].prev is None  # el viaje de 2024 fue después del corte: nuevo este año
    assert cats["Vivienda"].delta == pytest.approx(300)
    assert y.costliest.month == "2025-03" and y.costliest.top_category == "Viajes"
    assert [m.month for m in y.months] == [f"2025-{m:02d}" for m in range(1, 7)]


def test_sin_anio_anterior_completo_no_compara(db):
    y = yearly.year_summary(db, 2024)
    assert not y.partial and y.prev_totals is None
    assert all(c.prev is None for c in y.categories)
    assert yearly.years_available(db) == [2025, 2024]


def test_pagina(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from moooney.demo import build_demo
    from moooney.web.app import create_app

    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=Path(__file__).resolve().parent.parent / "config",
                               months=14)
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/year")
        assert r.status_code == 200 and "Por categoría" in r.text and "Mes a mes" in r.text
        r = c.get("/year?year=1999")
        assert r.status_code == 200


def test_el_mes_a_medias_no_es_el_mas_barato(db):
    y = yearly.year_summary(db, 2025)
    junio = y.months[-1]
    assert junio.month == "2025-06" and not junio.full
    assert y.cheapest.month != "2025-06"
