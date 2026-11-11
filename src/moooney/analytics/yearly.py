"""Resumen anual (decisión 2026-11-11): un año frente al anterior, en SQL sobre los mismos
criterios que las métricas mensuales (gasto = `expense` + `card_settlement`, ingreso = `income`).

Si el año está en curso, la comparación es justa: el año anterior se corta en el mismo día
(«hasta el 10 de octubre» frente a «hasta el 10 de octubre del año pasado»). Los meses que
no tienen datos en un año no cuentan como cero: se marcan y se dejan fuera de la comparación.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from moooney.analytics import periods
from moooney.analytics.metrics import EXPENSE_FLOWS

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


@dataclass
class YearTotals:
    income: float = 0.0
    expenses: float = 0.0
    investment: float = 0.0

    @property
    def net(self) -> float:
        return self.income - self.expenses

    @property
    def savings_rate(self) -> float | None:
        return self.net / self.income if self.income > 0 else None


@dataclass
class CategoryYear:
    category: str
    amount: float
    prev: float | None

    @property
    def delta(self) -> float | None:
        return None if self.prev is None else self.amount - self.prev

    @property
    def delta_pct(self) -> float | None:
        return None if not self.prev else (self.amount / self.prev - 1) * 100


@dataclass
class MonthYear:
    month: str  # AAAA-MM
    label: str
    expenses: float
    income: float
    prev_expenses: float | None
    has_data: bool
    full: bool = True  # mes completo: solo estos cuentan para el más caro y el más barato
    top_category: str | None = None
    top_amount: float = 0.0


@dataclass
class YearSummary:
    year: int
    cutoff: str  # último día que cuenta (AAAA-MM-DD)
    partial: bool
    totals: YearTotals
    prev_totals: YearTotals | None
    categories: list[CategoryYear] = field(default_factory=list)
    months: list[MonthYear] = field(default_factory=list)

    @property
    def costliest(self) -> MonthYear | None:
        con = [m for m in self.months if m.has_data and m.full]
        return max(con, key=lambda m: m.expenses) if con else None

    @property
    def cheapest(self) -> MonthYear | None:
        con = [m for m in self.months if m.has_data and m.full and m.expenses > 0]
        return min(con, key=lambda m: m.expenses) if con else None


def years_available(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute("SELECT DISTINCT substr(date, 1, 4) AS y FROM transactions ORDER BY y DESC").fetchall()
    return [int(r["y"]) for r in rows]


def _totals(conn: sqlite3.Connection, first: str, last: str) -> YearTotals:
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    row = conn.execute(
        f"""SELECT
              COALESCE(SUM(CASE WHEN flow_type = 'income' THEN amount END), 0) AS inc,
              COALESCE(-SUM(CASE WHEN flow_type IN ({ph}) THEN amount END), 0) AS exp,
              COALESCE(-SUM(CASE WHEN flow_type = 'investment' THEN amount END), 0) AS inv
            FROM transactions WHERE date BETWEEN ? AND ?""",
        (*EXPENSE_FLOWS, first, last),
    ).fetchone()
    return YearTotals(float(row["inc"]), float(row["exp"]), float(row["inv"]))


def _by_category(conn: sqlite3.Connection, first: str, last: str) -> dict[str, float]:
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    rows = conn.execute(
        f"""SELECT COALESCE(category, '(sin categoría)') AS c, -SUM(amount) AS s FROM transactions
            WHERE date BETWEEN ? AND ? AND flow_type IN ({ph}) GROUP BY c""",
        (first, last, *EXPENSE_FLOWS),
    ).fetchall()
    return {r["c"]: float(r["s"]) for r in rows}


def _has_data(conn: sqlite3.Connection, first: str, last: str) -> bool:
    return conn.execute("SELECT 1 FROM transactions WHERE date BETWEEN ? AND ? LIMIT 1", (first, last)).fetchone() is not None


def _shift_year(iso: str, years: int) -> str:
    y, rest = int(iso[:4]) + years, iso[4:]
    if rest == "-02-29":
        rest = "-02-28"
    return f"{y}{rest}"


def year_summary(conn: sqlite3.Connection, year: int) -> YearSummary | None:
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return None
    first = f"{year}-01-01"
    cutoff = min(f"{year}-12-31", as_of)
    if cutoff < first:
        return None
    partial = cutoff < f"{year}-12-31"
    prev_first, prev_cut = f"{year - 1}-01-01", _shift_year(cutoff, -1)
    has_prev = _has_data(conn, prev_first, prev_cut)
    # el año anterior solo se compara si tiene datos desde su primer mes; si empieza a mitad,
    # comparar totales engaña (medio año contra uno entero)
    first_date = periods.first_date(conn) or first
    prev_complete = has_prev and first_date <= f"{year - 1}-01-31"

    current = _by_category(conn, first, cutoff)
    prev = _by_category(conn, prev_first, prev_cut) if prev_complete else {}
    cats = [CategoryYear(c, v, prev.get(c) if prev_complete else None) for c, v in current.items() if v > 0]
    cats += [CategoryYear(c, 0.0, v) for c, v in prev.items() if c not in current and v > 0]
    cats.sort(key=lambda c: -max(c.amount, c.prev or 0))

    months: list[MonthYear] = []
    last_month = int(cutoff[5:7])
    for m in range(1, last_month + 1):
        month = f"{year}-{m:02d}"
        a, b = periods.month_bounds(month)
        b = min(b, cutoff)
        t = _totals(conn, a, b)
        has = _has_data(conn, a, b)
        pa, pb = periods.month_bounds(f"{year - 1}-{m:02d}")
        pb = min(pb, _shift_year(b, -1))
        prev_exp = _totals(conn, pa, pb).expenses if _has_data(conn, pa, pb) else None
        by_cat = _by_category(conn, a, b)
        top = max(by_cat.items(), key=lambda kv: kv[1], default=(None, 0.0))
        full = periods.is_full_month(month, as_of, first_date)
        months.append(MonthYear(month, MESES[m - 1], t.expenses, t.income, prev_exp, has, full, top[0], top[1]))

    return YearSummary(
        year=year, cutoff=cutoff, partial=partial, totals=_totals(conn, first, cutoff),
        prev_totals=_totals(conn, prev_first, prev_cut) if prev_complete else None,
        categories=cats, months=months,
    )
