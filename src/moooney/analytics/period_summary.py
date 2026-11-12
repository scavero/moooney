"""Resumen de un periodo que no es un mes: trimestre, año o rango libre (decisión 2026-11-12).

Mismos criterios que las métricas mensuales y que el resumen anual (gasto = `expense` +
`card_settlement`, ingreso = `income`). El periodo se compara con el anterior equivalente:
el trimestre con el trimestre anterior, el año con el año anterior y un rango con el tramo
de la misma longitud que lo precede. Si el periodo está en curso, el anterior se corta a la
misma altura («hasta el día 40 del trimestre» frente a lo mismo del anterior) para que la
comparación sea justa. Si el anterior no tiene datos desde su primer mes, no se compara.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from moooney.analytics import periods
from moooney.analytics.metrics import EXPENSE_FLOWS
from moooney.analytics.periods import Period
from moooney.analytics.yearly import (
    MESES,
    CategoryYear,
    MonthYear,
    YearTotals,
    _by_category,
    _has_data,
    _shift_year,
    _totals,
)


@dataclass
class PeriodSummary:
    period: Period
    start: str  # primer día que cuenta (el periodo, recortado al primer dato)
    cutoff: str  # último día que cuenta (el periodo, recortado al último dato)
    partial: bool  # el periodo no ha terminado en los datos
    n_tx: int
    totals: YearTotals
    prev: Period | None
    prev_cutoff: str | None
    prev_totals: YearTotals | None
    categories: list[CategoryYear] = field(default_factory=list)
    months: list[MonthYear] = field(default_factory=list)
    merchants: list[sqlite3.Row] = field(default_factory=list)

    @property
    def costliest(self) -> MonthYear | None:
        con = [m for m in self.months if m.has_data and m.full]
        return max(con, key=lambda m: m.expenses) if len(con) > 1 else None

    @property
    def cheapest(self) -> MonthYear | None:
        con = [m for m in self.months if m.has_data and m.full and m.expenses > 0]
        return min(con, key=lambda m: m.expenses) if len(con) > 1 else None


def top_merchants_between(conn: sqlite3.Connection, first: str, last: str, *, limit: int = 12) -> list[sqlite3.Row]:
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    return conn.execute(
        f"""
        SELECT concept_clean AS key, category, COUNT(*) AS n, -SUM(amount) AS total
        FROM transactions
        WHERE date BETWEEN ? AND ? AND flow_type IN ({ph}) AND amount < 0
        GROUP BY concept_clean
        ORDER BY total DESC
        LIMIT ?
        """,
        (first, last, *EXPENSE_FLOWS, limit),
    ).fetchall()


def _prev_cutoff(p: Period, prev: Period, cutoff: str) -> str:
    """Hasta dónde cuenta el periodo anterior: a la misma altura que el actual."""
    if cutoff >= p.end:
        return prev.end
    if p.kind == "year":
        return _shift_year(cutoff, -1)
    elapsed = periods.Period("range", "", p.start, cutoff).days
    return min(prev.end, periods.add_days(prev.start, elapsed - 1))


def period_summary(conn: sqlite3.Connection, p: Period, *, merchants: int = 8) -> PeriodSummary | None:
    """Resumen del periodo, o None si no hay ningún dato dentro."""
    as_of = periods.as_of_date(conn)
    first_date = periods.first_date(conn)
    if as_of is None or first_date is None or p.start > as_of or p.end < first_date:
        return None
    start, cutoff = max(p.start, first_date), min(p.end, as_of)
    partial = cutoff < p.end

    prev = periods.shift_period(p, -1)
    prev_cut = _prev_cutoff(p, prev, cutoff)
    # el anterior solo se compara si hay datos desde su primer mes: medio periodo contra uno entero engaña
    comparable = periods.month_of(first_date) <= periods.month_of(prev.start) and _has_data(conn, prev.start, prev_cut)

    current = _by_category(conn, start, cutoff)
    before = _by_category(conn, prev.start, prev_cut) if comparable else {}
    cats = [CategoryYear(c, v, before.get(c) if comparable else None) for c, v in current.items() if v > 0]
    cats += [CategoryYear(c, 0.0, v) for c, v in before.items() if c not in current and v > 0]
    cats.sort(key=lambda c: -max(c.amount, c.prev or 0))

    months: list[MonthYear] = []
    for month in p.months:
        a, b = periods.month_bounds(month)
        if b < start or a > cutoff:
            continue
        whole = a >= p.start and b <= p.end  # un rango puede coger el mes a trozos
        a, b = max(a, start), min(b, cutoff)
        t = _totals(conn, a, b)
        prev_exp = None
        if p.kind == "year":  # solo el año compara cada mes con el mismo mes del año anterior
            pa, pb = _shift_year(a, -1), _shift_year(b, -1)
            prev_exp = _totals(conn, pa, pb).expenses if _has_data(conn, pa, pb) else None
        by_cat = _by_category(conn, a, b)
        top = max(by_cat.items(), key=lambda kv: kv[1], default=(None, 0.0))
        full = whole and periods.is_full_month(month, as_of, first_date)
        months.append(MonthYear(month, MESES[int(month[5:7]) - 1], t.expenses, t.income, prev_exp,
                                _has_data(conn, a, b), full, top[0], top[1]))

    n_tx = int(conn.execute("SELECT COUNT(*) AS n FROM transactions WHERE date BETWEEN ? AND ?",
                            (start, cutoff)).fetchone()["n"])
    return PeriodSummary(
        period=p, start=start, cutoff=cutoff, partial=partial, n_tx=n_tx,
        totals=_totals(conn, start, cutoff),
        prev=prev if comparable else None, prev_cutoff=prev_cut if comparable else None,
        prev_totals=_totals(conn, prev.start, prev_cut) if comparable else None,
        categories=cats, months=months,
        merchants=top_merchants_between(conn, start, cutoff, limit=merchants),
    )
