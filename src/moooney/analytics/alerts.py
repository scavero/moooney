"""Alertas deterministas. Cada detector es una regla explicable; el modelo no decide nada aquí.

Tipos:
- budget_80 / budget_100: categoría al 80 % / 100 % del presupuesto del mes.
- anomalous_amount: importe muy fuera del histórico de ese comercio.
- duplicate_charge: mismo comercio e importe en menos de 48 h.
- new_recurring: recurrente que empezó hace poco (nueva suscripción o cuota).
- price_increase: recurrente cuyo último importe supera en > 5 % su media.
- missing_income: ingreso recurrente que no ha llegado cuando tocaba.
- negative_balance: el saldo proyectado hasta el próximo ingreso recurrente queda en negativo.
- bank_fee: comisiones bancarias cobradas en la ventana.

Cada alerta lleva una `dedup_key`; volver a ejecutar no la duplica.
"""

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

from moooney.analytics import periods
from moooney.analytics.budgets import BudgetRow
from moooney.analytics.metrics import EXPENSE_FLOWS

WINDOW_DAYS = 30
ANOMALY_MIN_HISTORY = 5
ANOMALY_SIGMAS = 3.0
ANOMALY_MIN_EUROS = 20.0
DUPLICATE_HOURS = 48
NEW_RECURRING_DAYS = 90
PRICE_INCREASE_PCT = 0.05
MISSING_INCOME_GRACE_DAYS = 5
MISSING_INCOME_MAX_PERIODS = 3  # pasados 3 periodos sin ingreso se deja de avisar
SEVERITIES = ("info", "warning", "critical")


@dataclass
class Alert:
    type: str
    severity: str
    date: str
    message: str
    dedup_key: str
    transaction_id: int | None = None


def _window(as_of: str, days: int = WINDOW_DAYS) -> str:
    return (date.fromisoformat(as_of) - timedelta(days=days)).isoformat()


# ---------- detectores ----------


def budget_alerts(status: list[BudgetRow], month: str) -> list[Alert]:
    out: list[Alert] = []
    for row in status:
        if row.limit <= 0:
            continue
        if row.pct >= 100:
            out.append(Alert(
                type="budget_100", severity="critical", date=f"{month}-01",
                message=f"{row.category}: presupuesto superado ({row.spent:,.0f} € de {row.limit:,.0f} €)",
                dedup_key=f"budget_100|{row.category}|{month}",
            ))
        elif row.pct >= 80:
            out.append(Alert(
                type="budget_80", severity="warning", date=f"{month}-01",
                message=f"{row.category}: al {row.pct:.0f} % del presupuesto ({row.spent:,.0f} € de {row.limit:,.0f} €)",
                dedup_key=f"budget_80|{row.category}|{month}",
            ))
    return out


def anomalous_amount_alerts(conn: sqlite3.Connection, as_of: str) -> list[Alert]:
    since = _window(as_of)
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    recent = conn.execute(
        f"SELECT id, date, concept_clean AS k, amount FROM transactions"
        f" WHERE date > ? AND flow_type IN ({ph}) AND amount < 0 ORDER BY date",
        (since, *EXPENSE_FLOWS),
    ).fetchall()
    out: list[Alert] = []
    cache: dict[str, list[float]] = {}
    for t in recent:
        key = t["k"]
        if key not in cache:
            cache[key] = [
                abs(float(r["amount"]))
                for r in conn.execute(
                    "SELECT amount FROM transactions WHERE concept_clean = ? AND date <= ? AND amount < 0",
                    (key, since),
                )
            ]
        hist = cache[key]
        if len(hist) < ANOMALY_MIN_HISTORY:
            continue
        med = statistics.median(hist)
        mad = statistics.median(abs(a - med) for a in hist)
        sigma = 1.4826 * mad if mad > 0 else max(med * 0.1, 1.0)
        amount = abs(float(t["amount"]))
        if amount - med > ANOMALY_SIGMAS * sigma and amount - med > ANOMALY_MIN_EUROS:
            out.append(Alert(
                type="anomalous_amount", severity="warning", date=t["date"],
                message=f"{key}: {amount:,.2f} € frente a los {med:,.2f} € habituales",
                dedup_key=f"anomalous_amount|{t['id']}", transaction_id=int(t["id"]),
            ))
    return out


def duplicate_charge_alerts(conn: sqlite3.Connection, as_of: str) -> list[Alert]:
    since = _window(as_of)
    rows = conn.execute(
        "SELECT a.id AS id_a, b.id AS id_b, a.concept_clean AS k, a.amount, a.date AS d_a, b.date AS d_b"
        " FROM transactions a JOIN transactions b"
        "   ON a.concept_clean = b.concept_clean AND a.amount = b.amount AND a.id < b.id"
        " WHERE a.amount < 0 AND b.date > ? AND a.flow_type IN ('expense', 'card_settlement')"
        "   AND julianday(b.date) - julianday(a.date) BETWEEN 0 AND ?",
        (since, DUPLICATE_HOURS / 24.0),
    ).fetchall()
    out: list[Alert] = []
    for r in rows:
        out.append(Alert(
            type="duplicate_charge", severity="warning", date=r["d_b"],
            message=f"{r['k']}: {abs(float(r['amount'])):,.2f} € cobrado dos veces ({r['d_a']} y {r['d_b']})",
            dedup_key=f"duplicate_charge|{r['id_a']}|{r['id_b']}", transaction_id=int(r["id_b"]),
        ))
    return out


def recurring_alerts(conn: sqlite3.Connection, as_of: str) -> list[Alert]:
    out: list[Alert] = []
    ref = date.fromisoformat(as_of)
    # Un ingreso que falta deja de ser "activo" justo cuando falta: por eso aquí se
    # miran todos los recurrentes y se filtra por tipo de alerta.
    for r in conn.execute("SELECT * FROM recurring"):
        first_seen = date.fromisoformat(r["first_seen"])
        last_seen = date.fromisoformat(r["last_seen"])
        avg, last = float(r["amount_avg"]), float(r["amount_last"])
        active = bool(r["active"])
        if r["flow_type"] == "income":
            expected = date.fromisoformat(r["next_expected"])
            recent = (ref - last_seen).days <= r["period_days"] * MISSING_INCOME_MAX_PERIODS
            if recent and (ref - expected).days > MISSING_INCOME_GRACE_DAYS:
                out.append(Alert(
                    type="missing_income", severity="critical", date=r["next_expected"],
                    message=f"{r['key']}: ingreso {r['period_label']} esperado el {r['next_expected']} y no ha llegado",
                    dedup_key=f"missing_income|{r['key']}|{r['next_expected']}",
                ))
            continue
        if not active:
            continue
        if r["flow_type"] in EXPENSE_FLOWS and (ref - first_seen).days <= NEW_RECURRING_DAYS:
            out.append(Alert(
                type="new_recurring", severity="info", date=r["last_seen"],
                message=f"{r['key']}: nuevo cargo {r['period_label']} de {abs(avg):,.2f} € desde {r['first_seen']}",
                dedup_key=f"new_recurring|{r['key']}",
            ))
        if r["flow_type"] in EXPENSE_FLOWS and avg < 0 and abs(last) > abs(avg) * (1 + PRICE_INCREASE_PCT):
            pct = (abs(last) / abs(avg) - 1) * 100
            out.append(Alert(
                type="price_increase", severity="warning", date=r["last_seen"],
                message=f"{r['key']}: último cargo {abs(last):,.2f} €, un {pct:.0f} % sobre su media ({abs(avg):,.2f} €)",
                dedup_key=f"price_increase|{r['key']}|{r['last_seen']}",
            ))
    return out


def negative_balance_alert(conn: sqlite3.Connection, as_of: str) -> list[Alert]:
    last = conn.execute(
        "SELECT balance FROM transactions WHERE balance IS NOT NULL ORDER BY date DESC, id DESC LIMIT 1"
    ).fetchone()
    if last is None:
        return []
    balance = float(last["balance"])
    incomes = conn.execute(
        "SELECT MIN(next_expected) AS d FROM recurring WHERE active = 1 AND flow_type = 'income'"
    ).fetchone()
    horizon = incomes["d"] if incomes and incomes["d"] and incomes["d"] > as_of else (
        date.fromisoformat(as_of) + timedelta(days=30)
    ).isoformat()
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    due = conn.execute(
        f"SELECT COALESCE(SUM(amount_avg), 0) AS s FROM recurring WHERE active = 1"
        f" AND flow_type IN ({ph}) AND next_expected > ? AND next_expected <= ?",
        (*EXPENSE_FLOWS, as_of, horizon),
    ).fetchone()
    projected = balance + float(due["s"])  # amount_avg de gastos es negativo
    if projected >= 0:
        return []
    return [Alert(
        type="negative_balance", severity="critical", date=as_of,
        message=f"saldo proyectado {projected:,.0f} € antes del próximo ingreso ({horizon}) "
                f"con los cargos recurrentes previstos ({-float(due['s']):,.0f} €)",
        dedup_key=f"negative_balance|{horizon}",
    )]


def bank_fee_alerts(conn: sqlite3.Connection, as_of: str) -> list[Alert]:
    since = _window(as_of)
    rows = conn.execute(
        "SELECT id, date, concept_clean AS k, amount FROM transactions"
        " WHERE date > ? AND category = 'Comisiones bancarias' AND amount < 0",
        (since,),
    ).fetchall()
    return [
        Alert(
            type="bank_fee", severity="info", date=r["date"],
            message=f"comisión bancaria de {abs(float(r['amount'])):,.2f} € ({r['k']})",
            dedup_key=f"bank_fee|{r['id']}", transaction_id=int(r["id"]),
        )
        for r in rows
    ]


# ---------- orquestación y persistencia ----------


def run_alerts(conn: sqlite3.Connection, as_of: str, budget_rows: list[BudgetRow] | None,
               month: str | None = None) -> list[Alert]:
    month = month or periods.month_of(as_of)
    alerts: list[Alert] = []
    if budget_rows:
        alerts += budget_alerts(budget_rows, month)
    alerts += anomalous_amount_alerts(conn, as_of)
    alerts += duplicate_charge_alerts(conn, as_of)
    alerts += recurring_alerts(conn, as_of)
    alerts += negative_balance_alert(conn, as_of)
    alerts += bank_fee_alerts(conn, as_of)
    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    alerts.sort(key=lambda a: (order[a.severity], a.date))
    return alerts


def store_alerts(conn: sqlite3.Connection, alerts: list[Alert]) -> int:
    """Inserta las alertas nuevas (por dedup_key). Devuelve cuántas entraron."""
    inserted = 0
    with conn:
        for a in alerts:
            cur = conn.execute(
                "INSERT OR IGNORE INTO alerts (type, severity, date, transaction_id, message,"
                " acknowledged, dedup_key) VALUES (?, ?, ?, ?, ?, 0, ?)",
                (a.type, a.severity, a.date, a.transaction_id, a.message, a.dedup_key),
            )
            inserted += cur.rowcount
    return inserted


def pending_alerts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM alerts WHERE acknowledged = 0 ORDER BY"
        " CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, date DESC"
    ).fetchall()


def acknowledge(conn: sqlite3.Connection, alert_id: int | None = None) -> int:
    with conn:
        if alert_id is None:
            cur = conn.execute("UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0")
        else:
            cur = conn.execute("UPDATE alerts SET acknowledged = 1 WHERE id = ?", (alert_id,))
    return cur.rowcount
