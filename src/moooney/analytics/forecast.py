"""Previsión sencilla y explicable: recurrentes conocidos + medias de lo variable.

- Fin del mes en curso: lo ya gastado + recurrentes que vencen en lo que queda +
  gasto variable medio por día de los últimos meses completos × días restantes.
- Próximos meses: fijo normalizado de los recurrentes activos + variable medio +
  ingresos medios. Sin modelos estadísticos opacos: cada cifra se puede reproducir.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date
from itertools import accumulate

from moooney.analytics import periods
from moooney.analytics.metrics import EXPENSE_FLOWS, MonthSummary, monthly_summary
from moooney.analytics.recurring import active_keys

HISTORY_MONTHS = 3


@dataclass
class MonthEndForecast:
    month: str
    day: int
    days_in_month: int
    spent: float
    fixed_spent: float
    variable_spent: float
    fixed_pending: float
    variable_expected_rest: float
    income_so_far: float
    income_pending: float
    history_months: int

    @property
    def expenses(self) -> float:
        return self.spent + self.fixed_pending + self.variable_expected_rest

    @property
    def income(self) -> float:
        return self.income_so_far + self.income_pending

    @property
    def net(self) -> float:
        return self.income - self.expenses


def full_months_before(conn: sqlite3.Connection, month: str, n: int, *, as_of: str,
                       first: str | None) -> list[MonthSummary]:
    """Los últimos n meses completos anteriores a `month` (puede devolver menos)."""
    keys = active_keys(conn)
    out: list[MonthSummary] = []
    cur = month
    while len(out) < n:
        cur = periods.prev_month(cur)
        if first is not None and cur < periods.month_of(first):
            break
        if periods.is_full_month(cur, as_of, first):
            out.append(monthly_summary(conn, cur, as_of=as_of, first=first, recurring=keys))
    return list(reversed(out))


def _avg(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _pending_recurring(conn: sqlite3.Connection, start_excl: str, end_incl: str,
                       flows: tuple[str, ...]) -> float:
    """Suma (en positivo) de recurrentes activos con vencimiento en (start, end]."""
    ph = ",".join("?" for _ in flows)
    row = conn.execute(
        f"SELECT COALESCE(SUM(ABS(amount_avg)), 0) AS s FROM recurring WHERE active = 1"
        f" AND flow_type IN ({ph}) AND next_expected > ? AND next_expected <= ?",
        (*flows, start_excl, end_incl),
    ).fetchone()
    return float(row["s"])


def forecast_month_end(conn: sqlite3.Connection, as_of: str, *, first: str | None = None) -> MonthEndForecast:
    month = periods.month_of(as_of)
    _, last_day = periods.month_bounds(month)
    dim = periods.days_in_month(month)
    day = int(as_of[8:10])
    keys = active_keys(conn)
    cur = monthly_summary(conn, month, as_of=as_of, first=first, recurring=keys)
    history = full_months_before(conn, month, HISTORY_MONTHS, as_of=as_of, first=first)
    variable_per_day = _avg([h.variable_expenses / periods.days_in_month(h.month) for h in history])
    rest_days = max(dim - day, 0)
    return MonthEndForecast(
        month=month, day=day, days_in_month=dim,
        spent=cur.expenses, fixed_spent=cur.fixed_expenses, variable_spent=cur.variable_expenses,
        fixed_pending=_pending_recurring(conn, as_of, last_day, EXPENSE_FLOWS),
        variable_expected_rest=variable_per_day * rest_days,
        income_so_far=cur.income,
        income_pending=_pending_recurring(conn, as_of, last_day, ("income",)),
        history_months=len(history),
    )


def usual_income(conn: sqlite3.Connection, as_of: str, *, first: str | None = None) -> float:
    """Ingresos medios de los últimos meses completos: con qué cuenta «si cobras como siempre»."""
    history = full_months_before(conn, periods.month_of(as_of), HISTORY_MONTHS, as_of=as_of, first=first)
    return _avg([h.income for h in history])


def last_balance(conn: sqlite3.Connection) -> float | None:
    """Saldo actual: suma del último saldo conocido de cada cuenta (varios bancos)."""
    rows = conn.execute(
        """
        SELECT (SELECT balance FROM transactions b WHERE b.account = t.account AND b.balance IS NOT NULL
                ORDER BY b.date DESC, b.id DESC LIMIT 1) AS balance
        FROM transactions t GROUP BY t.account
        """
    ).fetchall()
    saldos = [float(r["balance"]) for r in rows if r["balance"] is not None]
    return sum(saldos) if saldos else None


def runway_months(conn: sqlite3.Connection, as_of: str, *, first: str | None = None) -> float | None:
    """Meses que aguanta el saldo actual sin ingresos, al ritmo de gasto medio reciente."""
    balance = last_balance(conn)
    history = full_months_before(conn, periods.month_of(as_of), HISTORY_MONTHS, as_of=as_of, first=first)
    avg_exp = _avg([h.expenses for h in history])
    if balance is None or avg_exp <= 0:
        return None
    return max(balance, 0.0) / avg_exp


def savings_track(conn: sqlite3.Connection, as_of: str, *, first: str | None = None) -> dict:
    """Lo ahorrado mes a mes desde el primer dato, acumulado y separado en lo que se queda en la cuenta
    (ahorro neto menos lo que va a inversión) y lo aportado a inversión, más la media mensual de ambos en
    los 12 últimos meses completos. Solo flujos de la cuenta: lo que valen las inversiones no se ve."""
    cur, m = periods.month_of(as_of), periods.month_of(first or as_of)
    months = []
    while m <= cur:
        months.append(m)
        m = periods.next_month(m)
    keys = active_keys(conn)
    rows = [monthly_summary(conn, m, as_of=as_of, first=first, recurring=keys) for m in months]
    full = [r for r in rows if r.full_month][-12:]
    return {
        "months": months,
        "cuenta": [round(x, 2) for x in accumulate(r.net - r.investment for r in rows)],
        "invertido": [round(x, 2) for x in accumulate(r.investment for r in rows)],
        "media_cuenta": round(_avg([r.net - r.investment for r in full]), 2),
        "media_inversion": round(_avg([r.investment for r in full]), 2),
        "meses_media": len(full),
    }

def today_or_as_of(as_of: str) -> date:
    return date.fromisoformat(as_of)
