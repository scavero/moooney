"""Fase 3: métricas, recurrentes, presupuestos y alertas sobre una base sintética."""

import sqlite3
from datetime import date, timedelta

import pytest
from typer.testing import CliRunner

from moooney.analytics import periods
from moooney.analytics.alerts import (
    acknowledge,
    anomalous_amount_alerts,
    bank_fee_alerts,
    budget_alerts,
    duplicate_charge_alerts,
    negative_balance_alert,
    pending_alerts,
    recurring_alerts,
    run_alerts,
    store_alerts,
)
from moooney.analytics.budgets import (
    budget_status,
    limits_for_month,
    load_budgets_config,
    materialize_month,
    propose_budgets,
    save_budgets_config,
)
from moooney.analytics.metrics import build_report, category_breakdown, monthly_summary
from moooney.analytics.recurring import classify, detect_recurring, store_recurring
from moooney.db import repo

AS_OF = "2025-07-15"


def _tx(conn, rows):
    """rows: (date, key, amount, category, flow_type[, balance])."""
    with conn:
        for i, r in enumerate(rows):
            d, key, amount, category, flow = r[:5]
            balance = r[5] if len(r) > 5 else None
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount,"
                " balance, account, created_at, category, flow_type, confidence) VALUES"
                " (?, ?, 'X', ?, ?, ?, 'p', 't', ?, ?, 1.0)",
                (f"f{i}-{d}-{key}-{amount}", d, key, amount, balance, category, flow),
            )


def _monthly(key, amount, category, flow, day, months):
    """Un movimiento al mes durante `months` meses terminando en julio 2025."""
    out = []
    y, m = 2025, 7
    for _ in range(months):
        out.append((f"{y}-{m:02d}-{day:02d}", key, amount, category, flow))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


@pytest.fixture()
def db(tmp_path) -> sqlite3.Connection:
    conn = repo.connect(tmp_path / "t.db")
    rows = []
    rows += _monthly("NOMINA", 2000.0, "Ingresos", "income", 1, 8)
    rows += _monthly("ALQUILER FALSO", -700.0, "Vivienda", "expense", 2, 8)
    rows += _monthly("NETFLIX", -12.99, "Suscripciones", "expense", 5, 8)
    rows += _monthly("SUSCRIPCION FONDO", -200.0, "Ahorro e inversion", "investment", 3, 8)
    rows += _monthly("TRASPASO INTERNO", -300.0, "Transferencias", "transfer_internal", 4, 8)
    # compras variables de supermercado, varias por mes
    for base in _monthly("MERCADONA FALSA", -60.0, "Alimentacion", "expense", 10, 8):
        rows.append(base)
        rows.append((base[0][:8] + "20", "MERCADONA FALSA", -45.0, "Alimentacion", "expense"))
    # ocio solo algunos meses
    rows += [
        ("2025-05-18", "CINE FALSO", -18.0, "Ocio", "expense"),
        ("2025-06-21", "CINE FALSO", -18.0, "Ocio", "expense"),
        ("2025-07-12", "CINE FALSO", -18.0, "Ocio", "expense"),
    ]
    _tx(conn, rows)
    # saldo en el último movimiento
    with conn:
        conn.execute("UPDATE transactions SET balance = 900.0 WHERE date = '2025-07-12'")
    return conn


# ---------- periodos ----------


def test_periodos_basicos():
    assert periods.prev_month("2025-01") == "2024-12"
    assert periods.next_month("2025-12") == "2026-01"
    assert periods.last_n_months("2025-03", 3) == ["2024-12", "2025-01", "2025-02"]
    assert periods.month_bounds("2024-02") == ("2024-02-01", "2024-02-29")
    assert periods.is_full_month("2025-06", "2025-07-15")
    assert not periods.is_full_month("2025-07", "2025-07-15")
    assert not periods.is_full_month("2024-11", "2025-07-15", first="2024-11-10")


# ---------- recurrentes ----------


def test_classify_detecta_mensual_y_rechaza_irregular():
    mensual = ["2025-01-05", "2025-02-05", "2025-03-06", "2025-04-04", "2025-05-05"]
    label, period, cv = classify(mensual, [-12.99] * 5)
    assert label == "mensual" and 28 <= period <= 31 and cv == 0
    assert classify(["2025-01-05", "2025-01-09", "2025-03-01", "2025-06-20"], [-5] * 4) is None
    assert classify(["2025-01-05", "2025-02-05"], [-5, -5]) is None  # solo dos, no anual
    assert classify(["2024-03-01", "2025-03-02"], [-80, -82])[0] == "anual"


def test_detect_recurring_marca_activos_y_fijos(db):
    items = detect_recurring(db, AS_OF)
    by_key = {i.key: i for i in items}
    assert {"NOMINA", "ALQUILER FALSO", "NETFLIX", "SUSCRIPCION FONDO"} <= set(by_key)
    assert "CINE FALSO" in by_key  # tres meses seguidos ya es recurrente
    assert by_key["NETFLIX"].period_label == "mensual" and by_key["NETFLIX"].fixed
    assert by_key["NETFLIX"].next_expected in ("2025-08-04", "2025-08-05")
    assert by_key["NOMINA"].flow_type == "income" and by_key["NOMINA"].active
    assert "TRASPASO INTERNO" not in by_key  # los traspasos no se consideran
    # supermercado: dos compras al mes con intervalos 10/20 días: no es periódico
    assert "MERCADONA FALSA" not in by_key
    n = store_recurring(db, items, "t")
    assert n == len(items)
    assert db.execute("SELECT COUNT(*) AS n FROM recurring WHERE active = 1").fetchone()["n"] >= 4


# ---------- metricas ----------


def test_monthly_summary_y_fijo_variable(db):
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    s = monthly_summary(db, "2025-06", as_of=AS_OF, first="2024-12-01")
    assert s.income == pytest.approx(2000.0)
    assert s.expenses == pytest.approx(700 + 12.99 + 60 + 45 + 18)
    assert s.investment == pytest.approx(200.0)
    assert s.fixed_expenses == pytest.approx(700 + 12.99 + 18)  # recurrentes activos
    assert s.variable_expenses == pytest.approx(105.0)
    assert s.net == pytest.approx(2000 - s.expenses)
    assert s.savings_rate == pytest.approx(s.net / 2000)
    assert s.full_month
    assert not monthly_summary(db, "2025-07", as_of=AS_OF).full_month


def test_category_breakdown_compara_con_historico(db):
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    rows = category_breakdown(db, "2025-06", as_of=AS_OF, first="2024-12-01")
    by_cat = {r.category: r for r in rows}
    assert by_cat["Vivienda"].amount == pytest.approx(700.0)
    assert by_cat["Vivienda"].delta_prev == pytest.approx(0.0)
    assert by_cat["Ocio"].amount == pytest.approx(18.0)
    # media de 6 meses completos anteriores (dic-may): ocio solo en mayo → 18/6
    assert by_cat["Ocio"].n_prev_months == 6
    assert by_cat["Ocio"].avg_prev == pytest.approx(18.0 / 6)
    assert sum(r.pct for r in rows) == pytest.approx(100.0)
    assert "Transferencias" not in by_cat and "Ahorro e inversion" not in by_cat


def test_build_report_mes_por_defecto(db):
    report = build_report(db, n_months=3)
    assert report.month == "2025-07" and report.as_of == "2025-07-20"  # último movimiento del fixture
    assert [m.month for m in report.series] == ["2025-05", "2025-06", "2025-07"]


# ---------- presupuestos ----------


def test_propose_budgets_recorta_variables_y_respeta_fijas(db, tmp_path):
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    proposal = propose_budgets(db, "2025-07", months=6, cut=0.10, as_of=AS_OF, first="2024-12-01")
    assert proposal["Vivienda"]["cut_applied"] == 0.0  # 100 % recurrente
    assert proposal["Vivienda"]["limit"] == 700.0
    assert proposal["Alimentacion"]["cut_applied"] == 0.10
    assert proposal["Alimentacion"]["limit"] == 95.0  # 105 * 0.9 = 94.5 → 95
    assert "Transferencias" not in proposal and "Ahorro e inversion" not in proposal

    path = tmp_path / "budgets.yaml"
    limits = {c: v["limit"] for c, v in proposal.items()}
    save_budgets_config(path, limits, meta={"source": "proposal"})
    assert load_budgets_config(path) == limits
    assert load_budgets_config(tmp_path / "no_existe.yaml") == {}


def test_budget_status_y_materializacion(db):
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    limits = {"Alimentacion": 100.0, "Ocio": 20.0, "Vivienda": 700.0}
    rows = budget_status(db, "2025-07", limits, as_of=AS_OF)
    by_cat = {r.category: r for r in rows}
    assert by_cat["Alimentacion"].spent == pytest.approx(105.0)
    assert by_cat["Alimentacion"].pct == pytest.approx(105.0)
    assert by_cat["Alimentacion"].projected == pytest.approx(105.0 * 31 / 15)  # todo variable
    # Vivienda es 100 % recurrente ya pagado: la proyección no lo multiplica
    assert by_cat["Vivienda"].projected == pytest.approx(700.0)
    assert by_cat["Ocio"].pct == pytest.approx(90.0)
    assert rows[0].category == "Alimentacion"  # ordenado por % descendente

    assert materialize_month(db, "2025-07", limits, source="user") == 3
    assert materialize_month(db, "2025-07", {"Ocio": 25.0}, source="user") == 1  # upsert
    assert limits_for_month(db, "2025-07", {})["Ocio"] == 25.0
    assert limits_for_month(db, "2025-08", {"Ocio": 30.0}) == {"Ocio": 30.0}  # fallback


# ---------- alertas ----------


def test_budget_alerts_80_y_100(db):
    limits = {"Alimentacion": 100.0, "Ocio": 20.0, "Vivienda": 700.0}
    found = budget_alerts(budget_status(db, "2025-07", limits, as_of=AS_OF), "2025-07")
    tipos = {a.dedup_key: a for a in found}
    assert "budget_100|Alimentacion|2025-07" in tipos
    assert "budget_80|Ocio|2025-07" in tipos
    assert "budget_100|Vivienda|2025-07" in tipos  # 700 de 700 = 100 %


def test_anomalia_y_duplicado(db):
    _tx(db, [
        ("2025-07-13", "MERCADONA FALSA", -300.0, "Alimentacion", "expense"),  # 5x lo habitual
        ("2025-07-14", "NETFLIX", -12.99, "Suscripciones", "expense"),  # ya cobrado el 05 → no (>48h)
        ("2025-07-14", "CINE FALSO", -18.0, "Ocio", "expense"),  # 12 → 14: 48h justas → sí
    ])
    anom = anomalous_amount_alerts(db, AS_OF)
    assert [a.message.split(":")[0] for a in anom] == ["MERCADONA FALSA"]
    dup = duplicate_charge_alerts(db, AS_OF)
    assert {a.message.split(":")[0] for a in dup} == {"CINE FALSO"}


def test_alertas_de_recurrentes_nuevo_subida_e_ingreso_ausente(db):
    # suscripción nueva (3 cargos recientes) y subida de precio en la última cuota
    _tx(db, [
        ("2025-05-09", "GIMNASIO FALSO", -30.0, "Ocio", "expense"),
        ("2025-06-09", "GIMNASIO FALSO", -30.0, "Ocio", "expense"),
        ("2025-07-09", "GIMNASIO FALSO", -36.0, "Ocio", "expense"),
    ])
    store_recurring(db, detect_recurring(db, "2025-07-15"), "t")
    found = recurring_alerts(db, "2025-07-15")
    tipos = {(a.type, a.message.split(":")[0]) for a in found}
    assert ("new_recurring", "GIMNASIO FALSO") in tipos
    assert ("price_increase", "GIMNASIO FALSO") in tipos
    assert not any(a.type == "missing_income" for a in found)
    # un mes y medio después sin nómina: ingreso ausente
    store_recurring(db, detect_recurring(db, "2025-08-20"), "t")
    found = recurring_alerts(db, "2025-08-20")
    assert any(a.type == "missing_income" and "NOMINA" in a.message for a in found)


def test_saldo_proyectado_negativo(db):
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    # saldo 900 el 12/07; antes de la nómina del 01/08 toca alquiler (700) + netflix + fondo…
    # alquiler 02/08 cae DESPUÉS de la nómina del 01/08: no entra. Solo lo que venza antes.
    assert negative_balance_alert(db, AS_OF) == []
    with db:
        db.execute("UPDATE transactions SET balance = 5.0 WHERE date = '2025-07-12'")
        # alquiler el día 20 y el de julio aún no cobrado: vence el 21/07, antes de la nómina
        db.execute("DELETE FROM transactions WHERE concept_clean = 'ALQUILER FALSO' AND date >= '2025-07-01'")
        db.execute("UPDATE transactions SET date = substr(date, 1, 8) || '20' WHERE concept_clean = 'ALQUILER FALSO'")
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    found = negative_balance_alert(db, AS_OF)
    assert len(found) == 1 and found[0].severity == "critical"


def test_comisiones_y_persistencia_sin_duplicados(db):
    _tx(db, [("2025-07-10", "COMISION MANTENIMIENTO", -8.0, "Comisiones bancarias", "expense")])
    store_recurring(db, detect_recurring(db, AS_OF), "t")
    assert len(bank_fee_alerts(db, AS_OF)) == 1
    found = run_alerts(db, AS_OF, None)
    n1 = store_alerts(db, found)
    n2 = store_alerts(db, run_alerts(db, AS_OF, None))
    assert n1 == len(found) and n2 == 0
    pend = pending_alerts(db)
    assert len(pend) == n1
    assert pend[0]["severity"] in ("critical", "warning", "info")
    assert acknowledge(db, pend[0]["id"]) == 1
    assert len(pending_alerts(db)) == n1 - 1
    assert acknowledge(db) == n1 - 1


def test_migracion_dedup_key_sobre_tabla_antigua(tmp_path):
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.execute(
        "CREATE TABLE alerts (id INTEGER PRIMARY KEY, type TEXT NOT NULL, severity TEXT,"
        " date TEXT, transaction_id INTEGER, message TEXT, acknowledged INTEGER NOT NULL DEFAULT 0)"
    )
    raw.commit()
    raw.close()
    conn = repo.connect(db_path)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(alerts)")}
    assert "dedup_key" in cols
    assert store_alerts(conn, [
        __import__("moooney.analytics.alerts", fromlist=["Alert"]).Alert(
            "bank_fee", "info", "2025-01-01", "m", "k1"
        )
    ]) == 1


# ---------- CLI ----------


def test_cli_metrics_budget_y_alerts(tmp_path, monkeypatch, db):
    import yaml

    from moooney.cli import app

    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        yaml.safe_dump({
            "paths": {"db_path": "t.db"},
            "parser": {"columns": {c: [c] for c in ["date", "value_date", "concept", "amount", "balance"]}},
        }),
        encoding="utf-8",
    )
    (tmp_path / "config" / "categories.yaml").write_text(
        yaml.safe_dump({
            "categories": {"Ocio": {"flow_type": "expense", "description": "x"}},
            "flow_types": {"expense": "g"},
        }),
        encoding="utf-8",
    )
    db.close()
    runner = CliRunner()
    r = runner.invoke(app, ["metrics", "--months", "3"])
    assert r.exit_code == 0, r.output
    assert "Resumen del mes" in r.output and "2025-07" in r.output
    r = runner.invoke(app, ["recurring"])
    assert r.exit_code == 0 and "NETFLIX" in r.output
    r = runner.invoke(app, ["budget", "init"])
    assert r.exit_code == 0, r.output
    assert (tmp_path / "config" / "budgets.yaml").exists()
    r = runner.invoke(app, ["budget", "init"])
    assert r.exit_code == 1  # ya existe, sin --force
    r = runner.invoke(app, ["budget", "set", "Ocio", "10"])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["budget", "show"])
    assert r.exit_code == 0 and "Ocio" in r.output
    r = runner.invoke(app, ["alerts"])
    assert r.exit_code == 0, r.output
    assert "budget_100" in r.output  # Ocio: 18 gastados de 10
    r = runner.invoke(app, ["alerts", "--ack-all"])
    assert r.exit_code == 0


def test_days_helpers():
    assert periods.days_in_month("2025-02") == 28
    assert (date.fromisoformat("2025-07-15") - timedelta(days=30)).isoformat() == "2025-06-15"
