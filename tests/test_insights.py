"""Previsión y diagnóstico sobre la base sintética de la demo y la de analítica."""

import pytest

from moooney.analytics.forecast import (
    forecast_month_end,
    forecast_next_months,
    runway_months,
    savings_projection,
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


def test_forecast_proximos_meses_y_runway(demo_db):
    as_of = "2026-10-10"
    store_recurring(demo_db, detect_recurring(demo_db, as_of), "t")
    nxt = forecast_next_months(demo_db, as_of, n=3)
    assert [m.month for m in nxt] == ["2026-11", "2026-12", "2027-01"]
    assert all(m.fixed > 0 and m.variable > 0 and m.income > 0 for m in nxt)
    assert nxt[0].expenses == pytest.approx(nxt[0].fixed + nxt[0].variable)
    rw = runway_months(demo_db, as_of)
    assert rw is not None and rw > 0
    proj = savings_projection(demo_db, as_of, months=6)
    assert len(proj) == 6 and proj[0][0] == "2026-11"


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
