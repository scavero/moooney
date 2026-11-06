"""El veredicto del resumen: una frase determinista a partir del resumen del mes."""

from moooney.analytics.insights import Insight
from moooney.analytics.metrics import MonthSummary
from moooney.web.app import build_verdict


def _s(**kw):
    base = dict(month="2026-10", income=2600.0, expenses=1750.0, full_month=True)
    base.update(kw)
    return MonthSummary(**base)


def test_veredicto_ahorro_y_aviso():
    aviso = Insight(kind="category_up", severity="warn", title="Alimentación se dispara", detail="x", impact=120.0)
    assert build_verdict(_s(), [aviso]) == "Has ahorrado el 33 % de lo que entró; alimentación se dispara."


def test_veredicto_mes_en_rojo_y_sin_ingresos():
    assert build_verdict(_s(expenses=3000.0), []) == "Has gastado 400 € más de lo que entró."
    assert build_verdict(_s(income=0.0, expenses=300.0), []).startswith("Sin ingresos, has gastado 300 €")


def test_veredicto_mes_incompleto_lleva_el_dia():
    assert build_verdict(_s(full_month=False), [], day=10).startswith("A día 10, has ahorrado")
