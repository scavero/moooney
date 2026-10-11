"""Previsión sencilla y explicable: recurrentes conocidos + medias de lo variable.

- Fin del mes en curso: lo ya gastado + recurrentes que vencen en lo que queda +
  gasto variable medio por día de los últimos meses completos × días restantes.
- Próximos meses: fijo normalizado de los recurrentes activos + variable medio +
  ingresos medios. Sin modelos estadísticos opacos: cada cifra se puede reproducir.
"""

import sqlite3
from dataclasses import dataclass
from datetime import date

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


@dataclass
class MonthForecast:
    month: str
    fixed: float
    variable: float
    income: float

    @property
    def expenses(self) -> float:
        return self.fixed + self.variable

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


def forecast_next_months(conn: sqlite3.Connection, as_of: str, n: int = 3, *,
                         first: str | None = None) -> list[MonthForecast]:
    month = periods.month_of(as_of)
    history = full_months_before(conn, month, HISTORY_MONTHS, as_of=as_of, first=first)
    variable = _avg([h.variable_expenses for h in history])
    income = _avg([h.income for h in history])
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    fixed = float(conn.execute(
        f"SELECT COALESCE(SUM(ABS(amount_avg) * 30.0 / period_days), 0) AS s FROM recurring"
        f" WHERE active = 1 AND flow_type IN ({ph})",
        EXPENSE_FLOWS,
    ).fetchone()["s"])
    out: list[MonthForecast] = []
    cur = month
    for _ in range(n):
        cur = periods.next_month(cur)
        out.append(MonthForecast(month=cur, fixed=fixed, variable=variable, income=income))
    return out


def last_balance(conn: sqlite3.Connection) -> float | None:
    row = conn.execute(
        "SELECT balance FROM transactions WHERE balance IS NOT NULL ORDER BY date DESC, id DESC LIMIT 1"
    ).fetchone()
    return float(row["balance"]) if row else None


def runway_months(conn: sqlite3.Connection, as_of: str, *, first: str | None = None) -> float | None:
    """Meses que aguanta el saldo actual sin ingresos, al ritmo de gasto medio reciente."""
    balance = last_balance(conn)
    history = full_months_before(conn, periods.month_of(as_of), HISTORY_MONTHS, as_of=as_of, first=first)
    avg_exp = _avg([h.expenses for h in history])
    if balance is None or avg_exp <= 0:
        return None
    return max(balance, 0.0) / avg_exp


def savings_projection(conn: sqlite3.Connection, as_of: str, months: int = 12, *,
                       first: str | None = None) -> list[tuple[str, float]]:
    """Saldo proyectado mes a mes si se mantiene el ahorro neto medio reciente."""
    balance = last_balance(conn) or 0.0
    history = full_months_before(conn, periods.month_of(as_of), 6, as_of=as_of, first=first)
    net = _avg([h.net for h in history])
    out = []
    cur = periods.month_of(as_of)
    acc = balance
    for _ in range(months):
        cur = periods.next_month(cur)
        acc += net
        out.append((cur, acc))
    return out


def today_or_as_of(as_of: str) -> date:
    return date.fromisoformat(as_of)
