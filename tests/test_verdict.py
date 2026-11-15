"""El veredicto del resumen (una frase determinista) y la lista «Para mirar»."""

from types import SimpleNamespace

from moooney.analytics.insights import Insight
from moooney.analytics.metrics import MonthSummary
from moooney.web.app import build_verdict, para_mirar


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


def test_veredicto_con_prevision_habla_de_como_acaba_el_mes():
    s = _s(full_month=False)
    assert build_verdict(s, [], day=10, forecast=SimpleNamespace(net=-60.0)) == "Vas camino de gastar 60 € más de lo que entra este mes."
    assert build_verdict(s, [], day=10, forecast=SimpleNamespace(net=396.0)) == "Vas camino de ahorrar 396 € este mes."


def test_para_mirar_funde_alertas_y_hallazgos_sin_repetir():
    pending = [{"severity": "info", "type": "bank_fee", "date": "2026-10-02", "message": "comisión de 8 €"},
               {"severity": "critical", "type": "budget_100", "date": "2026-10-01", "message": "Ocio: presupuesto superado (90 € de 60 €)"}]
    insights = [Insight(kind="month_end", severity="bad", title="Previsión a fin de mes", detail="x"),  # ya es la cifra de cabecera
                Insight(kind="category_up", severity="warn", title="Ocio ya supera un mes normal", detail="x", impact=30.0),  # ya hay alerta
                Insight(kind="fees", severity="warn", title="El banco te ha cobrado 96 €", detail="x", impact=8.0),  # ídem
                Insight(kind="runway", severity="bad", title="Tu saldo cubre 0,8 meses de gasto", detail="x"),
                Insight(kind="fixed_total", severity="info", title="Tus gastos fijos", detail="x")]  # dato, no pendiente
    items = para_mirar(pending, insights, dudosas=2)
    assert [x["title"] for x in items] == ["Ocio: presupuesto superado (90 € de 60 €)", "Tu saldo cubre 0,8 meses de gasto",
                                           "2 comercios por revisar", "comisión de 8 €"]
    assert items[0]["detail"] == "Presupuesto superado · 1 oct" and para_mirar([], [], 0) == []
