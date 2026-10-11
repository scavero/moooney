"""Métricas mensuales en SQL puro: ingresos, gastos, ahorro, fijo/variable y categorías.

Convenciones:
- Gasto = flujos `expense` y `card_settlement`, en positivo (una devolución resta).
- Ingreso = flujo `income`. Inversión = flujo `investment` (aportación neta, positiva).
- Ahorro neto = ingresos - gastos. La inversión es ahorro, no gasto: se muestra aparte.
- Transferencias internas no cuentan en nada.
- Fijo = gasto en claves recurrentes activas (tabla recurring); variable = el resto.
"""

import sqlite3
from dataclasses import dataclass, field

from moooney.analytics import periods
from moooney.analytics.recurring import active_keys

EXPENSE_FLOWS = ("expense", "card_settlement")


@dataclass
class MonthSummary:
    month: str
    income: float = 0.0
    expenses: float = 0.0
    investment: float = 0.0
    fixed_expenses: float = 0.0
    n_tx: int = 0
    full_month: bool = True

    @property
    def net(self) -> float:
        return self.income - self.expenses

    @property
    def savings_rate(self) -> float | None:
        return self.net / self.income if self.income > 0 else None

    @property
    def variable_expenses(self) -> float:
        return self.expenses - self.fixed_expenses


@dataclass
class CategoryRow:
    category: str
    amount: float
    pct: float
    prev_amount: float | None = None
    avg_prev: float | None = None
    n_prev_months: int = 0
    fixed_amount: float = 0.0

    @property
    def delta_prev(self) -> float | None:
        return None if self.prev_amount is None else self.amount - self.prev_amount

    @property
    def delta_avg(self) -> float | None:
        return None if self.avg_prev is None else self.amount - self.avg_prev


@dataclass
class MetricsReport:
    as_of: str
    month: str
    summary: MonthSummary
    series: list[MonthSummary] = field(default_factory=list)
    categories: list[CategoryRow] = field(default_factory=list)


def _sum(conn: sqlite3.Connection, month: str, flows: tuple[str, ...], sign: int) -> float:
    first, last = periods.month_bounds(month)
    ph = ",".join("?" for _ in flows)
    row = conn.execute(
        f"SELECT COALESCE(SUM(amount), 0) AS s FROM transactions"
        f" WHERE date BETWEEN ? AND ? AND flow_type IN ({ph})",
        (first, last, *flows),
    ).fetchone()
    return sign * float(row["s"])


def monthly_summary(conn: sqlite3.Connection, month: str, *, as_of: str | None = None,
                    first: str | None = None, recurring: set[str] | None = None) -> MonthSummary:
    first_day, last_day = periods.month_bounds(month)
    s = MonthSummary(month=month)
    s.income = _sum(conn, month, ("income",), +1)
    s.expenses = _sum(conn, month, EXPENSE_FLOWS, -1)
    s.investment = _sum(conn, month, ("investment",), -1)
    s.n_tx = int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM transactions WHERE date BETWEEN ? AND ?",
            (first_day, last_day),
        ).fetchone()["n"]
    )
    keys = active_keys(conn) if recurring is None else recurring
    if keys:
        ph = ",".join("?" for _ in keys)
        fph = ",".join("?" for _ in EXPENSE_FLOWS)
        row = conn.execute(
            f"SELECT COALESCE(SUM(amount), 0) AS s FROM transactions WHERE date BETWEEN ? AND ?"
            f" AND flow_type IN ({fph}) AND concept_clean IN ({ph})",
            (first_day, last_day, *EXPENSE_FLOWS, *keys),
        ).fetchone()
        s.fixed_expenses = -float(row["s"])
    if as_of is not None:
        s.full_month = periods.is_full_month(month, as_of, first)
    return s


def monthly_series(conn: sqlite3.Connection, month: str, n_months: int, *,
                   as_of: str | None = None, first: str | None = None) -> list[MonthSummary]:
    keys = active_keys(conn)
    months = [*periods.last_n_months(month, n_months - 1), month] if n_months > 0 else [month]
    return [monthly_summary(conn, m, as_of=as_of, first=first, recurring=keys) for m in months]


def _spend_by_category(conn: sqlite3.Connection, month: str) -> dict[str, float]:
    first, last = periods.month_bounds(month)
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    rows = conn.execute(
        f"""
        SELECT COALESCE(category, '(sin categoría)') AS c, -SUM(amount) AS gasto
        FROM transactions
        WHERE date BETWEEN ? AND ? AND flow_type IN ({ph})
        GROUP BY category
        """,
        (first, last, *EXPENSE_FLOWS),
    ).fetchall()
    return {r["c"]: float(r["gasto"]) for r in rows}


def category_breakdown(conn: sqlite3.Connection, month: str, *, compare_months: int = 6,
                       as_of: str | None = None, first: str | None = None) -> list[CategoryRow]:
    """Gasto por categoría del mes, con variación vs mes anterior y vs media de meses completos."""
    current = _spend_by_category(conn, month)
    total = sum(v for v in current.values() if v > 0)
    prev = _spend_by_category(conn, periods.prev_month(month))
    history: list[dict[str, float]] = []
    for m in periods.last_n_months(month, compare_months):
        if as_of is not None and not periods.is_full_month(m, as_of, first):
            continue
        history.append(_spend_by_category(conn, m))
    keys = active_keys(conn)
    fixed_by_cat: dict[str, float] = {}
    if keys:
        first_day, last_day = periods.month_bounds(month)
        ph = ",".join("?" for _ in keys)
        fph = ",".join("?" for _ in EXPENSE_FLOWS)
        for r in conn.execute(
            f"SELECT COALESCE(category, '(sin categoría)') AS c, -SUM(amount) AS g FROM transactions"
            f" WHERE date BETWEEN ? AND ? AND flow_type IN ({fph}) AND concept_clean IN ({ph})"
            f" GROUP BY category",
            (first_day, last_day, *EXPENSE_FLOWS, *keys),
        ):
            fixed_by_cat[r["c"]] = float(r["g"])

    out: list[CategoryRow] = []
    for cat, amount in sorted(current.items(), key=lambda kv: -kv[1]):
        hist_vals = [h.get(cat, 0.0) for h in history]
        out.append(
            CategoryRow(
                category=cat,
                amount=amount,
                pct=(amount / total * 100.0) if total else 0.0,
                prev_amount=prev.get(cat, 0.0) if prev else None,
                avg_prev=(sum(hist_vals) / len(hist_vals)) if hist_vals else None,
                n_prev_months=len(hist_vals),
                fixed_amount=fixed_by_cat.get(cat, 0.0),
            )
        )
    return out


def build_report(conn: sqlite3.Connection, month: str | None = None, *, n_months: int = 6) -> MetricsReport:
    as_of = periods.as_of_date(conn)
    if as_of is None:
        raise ValueError("no hay transacciones cargadas")
    first = periods.first_date(conn)
    month = month or periods.month_of(as_of)
    summary = monthly_summary(conn, month, as_of=as_of, first=first)
    series = monthly_series(conn, month, n_months, as_of=as_of, first=first)
    categories = category_breakdown(conn, month, as_of=as_of, first=first)
    return MetricsReport(as_of=as_of, month=month, summary=summary, series=series, categories=categories)
