"""Previsión y diagnóstico sobre la base sintética de la demo y la de analítica."""

import pytest

from moooney.analytics.forecast import (
    forecast_month_end,
    runway_months,
    savings_summary,
    savings_track,
)
from moooney.analytics.insights import build_insights, spend_share_12m
from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.db import repo
from moooney.demo import build_demo

REPO_CONFIG = __import__("pathlib").Path(__file__).resolve().parent.parent / "config"


@pytest.fixture()
def demo_db(tmp_path):
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=10)
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    yield conn
    conn.close()
    assert settings_path.exists()


def test_forecast_fin_de_mes_coherente(demo_db):
    as_of = "2026-10-10"
    store_recurring(demo_db, detect_recurring(demo_db, as_of), "t")
    f = forecast_month_end(demo_db, as_of)
    assert f.month == "2026-10" and f.day == 10 and f.days_in_month == 31
    assert f.history_months == 3
    assert f.expenses >= f.spent
    assert f.fixed_spent + f.variable_spent == pytest.approx(f.spent)
    assert f.fixed_pending >= 0 and f.variable_expected_rest > 0
    assert f.income == f.income_so_far + f.income_pending


def test_runway_y_saldo_proyectado(demo_db):
    as_of = "2026-10-10"
    store_recurring(demo_db, detect_recurring(demo_db, as_of), "t")
    rw = runway_months(demo_db, as_of)
    assert rw is not None and rw > 0


def test_insights_ordenados_y_con_enlaces(demo_db):
    store_recurring(demo_db, detect_recurring(demo_db, "2026-10-10"), "t")
    items, report = build_insights(demo_db)
    assert report.month == "2026-10"
    kinds = {i.kind for i in items}
    assert "month_end" in kinds
    assert "fixed_total" in kinds and "subscriptions" in kinds
    assert "fees" in kinds  # la demo tiene comisiones
    assert "runway" in kinds
    severities = [i.severity for i in items]
    order = {"bad": 0, "warn": 1, "good": 2, "info": 3}
    assert [order[s] for s in severities] == sorted(order[s] for s in severities)
    for i in items:
        assert i.title and i.detail
        assert i.tone in ("ok", "info", "warn", "crit")
    share = spend_share_12m(demo_db, "2026-10-10")
    assert share and share[0][0] == "Vivienda"
    assert sum(p for _, _, p in share) == pytest.approx(100.0)


def test_insights_mes_anterior_sin_prevision(demo_db):
    store_recurring(demo_db, detect_recurring(demo_db, "2026-10-10"), "t")
    items, report = build_insights(demo_db, "2026-09")
    assert report.month == "2026-09"
    assert not any(i.kind == "month_end" for i in items)


def test_mes_a_medias_no_extrapola(demo_db):
    """Con el mes en curso solo cuenta lo ya gastado de más; nada de multiplicar por los días que faltan."""
    store_recurring(demo_db, detect_recurring(demo_db, "2026-10-10"), "t")
    items, report = build_insights(demo_db)
    assert not report.summary.full_month
    assert not any(i.kind == "category_down" for i in items)
    cats = {c.category: c for c in report.categories}
    for i in (i for i in items if i.kind == "category_up"):
        c = cats[i.title.split(" ya supera")[0]]
        assert i.impact == pytest.approx(c.amount - c.avg_prev)
        assert c.amount > c.avg_prev



def test_ahorro_acumulado_separa_cuenta_e_inversion(tmp_path):
    from moooney.db import repo
    from tests.test_analytics import _tx

    conn = repo.connect(tmp_path / "t.db")
    filas = []
    for m in range(1, 5):
        filas += [(f"2026-{m:02d}-01", "NOMINA", 2000.0, "Ingresos", "income"),
                  (f"2026-{m:02d}-05", "ALQUILER", -1200.0, "Vivienda", "expense"),
                  (f"2026-{m:02d}-06", "BROKER INVENTADO", -300.0, "Inversion", "investment")]
    _tx(conn, filas)
    t = savings_track(conn, "2026-04-30", first="2026-01-01")
    assert t["months"] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    # ahorra 800 al mes: 300 van a inversión y 500 se quedan en la cuenta
    assert t["invertido"] == [300, 600, 900, 1200] and t["cuenta"] == [500, 1000, 1500, 2000]
    assert (t["media_cuenta"], t["media_inversion"], t["meses_media"]) == (500, 300, 4)
    # la proyección para los modelos: sin intereses es lo aportado; con ellos, más
    p = savings_summary(conn, "2026-04-30", first="2026-01-01", years=1, rate=0.0)["proyeccion_si_sigue_igual"]
    assert (p["en_la_cuenta"], p["inversion_solo_aportado"], p["inversion_con_intereses"]) == (8000, 4800, 4800)
    assert savings_summary(conn, "2026-04-30", first="2026-01-01", years=1, rate=0.05)["proyeccion_si_sigue_igual"]["inversion_con_intereses"] > 4800
