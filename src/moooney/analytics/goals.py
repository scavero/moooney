"""Metas de ahorro y «qué pasaría si» (decisión 2026-11-11). Determinista.

La base es el mes medio de los últimos seis meses completos: ingresos, gasto por categoría y
recurrentes activos. El gasto de cada categoría se parte en fijo (recurrentes activos de esa
categoría, llevados a 30 días) y variable (el resto): el simulador recorta un porcentaje de lo
variable o quita recurrentes enteros, así no se cuenta dos veces el mismo euro.

Una meta dice cuánto quieres juntar, cuánto llevas ya apartado y, si quieres, para cuándo.
Con el ahorro medio al mes se calcula cuándo llegarías y, si hay fecha, cuánto tendrías que
ahorrar al mes para llegar a tiempo.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime

from moooney.analytics import periods
from moooney.analytics.metrics import EXPENSE_FLOWS, _spend_by_category
from moooney.analytics.recurring import load_recurring

BASE_MONTHS = 6


@dataclass
class Goal:
    id: int
    name: str
    target: float
    saved: float
    deadline: str | None  # AAAA-MM

    @property
    def missing(self) -> float:
        return max(0.0, self.target - self.saved)

    @property
    def pct(self) -> float:
        return min(100.0, self.saved / self.target * 100) if self.target > 0 else 0.0


@dataclass
class CategoryBase:
    category: str
    total: float  # gasto medio al mes
    fixed: float  # parte de recurrentes activos

    @property
    def variable(self) -> float:
        return max(0.0, self.total - self.fixed)


@dataclass
class RecurringBase:
    key: str
    category: str | None
    monthly: float  # en positivo, llevado a 30 días


@dataclass
class Baseline:
    months: list[str]
    income: float
    expenses: float
    categories: list[CategoryBase] = field(default_factory=list)
    recurring: list[RecurringBase] = field(default_factory=list)

    @property
    def net(self) -> float:
        return self.income - self.expenses


@dataclass
class Plan:
    months_to_goal: int | None  # al ritmo actual; None si no ahorras
    eta: str | None  # AAAA-MM
    months_left: int | None  # hasta la fecha límite
    needed: float | None  # al mes para llegar a la fecha
    on_track: bool | None


def _months_between(a: str, b: str) -> int:
    """Meses de a (AAAA-MM) a b (AAAA-MM), contando el de b."""
    return (int(b[:4]) - int(a[:4])) * 12 + int(b[5:7]) - int(a[5:7])


def _add_months(month: str, n: int) -> str:
    y, m = int(month[:4]), int(month[5:7]) - 1 + n
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def baseline(conn: sqlite3.Connection) -> Baseline | None:
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return None
    first = periods.first_date(conn)
    current = periods.month_of(as_of)
    months = [m for m in periods.last_n_months(current, 24) if periods.is_full_month(m, as_of, first)][-BASE_MONTHS:]
    if not months:
        return None
    n = len(months)
    a, _ = periods.month_bounds(months[0])
    _, b = periods.month_bounds(months[-1])
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    row = conn.execute(
        f"""SELECT COALESCE(SUM(CASE WHEN flow_type = 'income' THEN amount END), 0) AS inc,
                   COALESCE(-SUM(CASE WHEN flow_type IN ({ph}) THEN amount END), 0) AS exp
            FROM transactions WHERE date BETWEEN ? AND ?""",
        (*EXPENSE_FLOWS, a, b),
    ).fetchone()
    totals: dict[str, float] = {}
    for m in months:
        for c, v in _spend_by_category(conn, m).items():
            totals[c] = totals.get(c, 0.0) + v / n
    rec = [
        RecurringBase(r["key"], r["category"], -float(r["amount_avg"]) * 30.0 / r["period_days"])
        for r in load_recurring(conn, active_only=True)
        if r["flow_type"] == "expense" and float(r["amount_avg"]) < 0
    ]
    rec.sort(key=lambda r: -r.monthly)
    fixed: dict[str, float] = {}
    for r in rec:
        fixed[r.category or ""] = fixed.get(r.category or "", 0.0) + r.monthly
    cats = [CategoryBase(c, v, min(v, fixed.get(c, 0.0))) for c, v in totals.items() if v > 0]
    cats.sort(key=lambda c: -c.variable)
    return Baseline(months, float(row["inc"]) / n, float(row["exp"]) / n, cats, rec)


def plan(goal: Goal, monthly_saving: float, today: str) -> Plan:
    """Cuándo llegas al ritmo `monthly_saving` y cuánto hace falta para llegar a la fecha."""
    now = today[:7]
    if goal.missing <= 0:
        return Plan(0, now, None, 0.0, True)
    months_to = math.ceil(goal.missing / monthly_saving) if monthly_saving > 0 else None
    eta = _add_months(now, months_to) if months_to is not None else None
    if not goal.deadline:
        return Plan(months_to, eta, None, None, None)
    left = max(0, _months_between(now, goal.deadline))
    needed = goal.missing / left if left > 0 else goal.missing
    return Plan(months_to, eta, left, needed, months_to is not None and months_to <= left)


# ---------- guardar ----------


def list_goals(conn: sqlite3.Connection) -> list[Goal]:
    rows = conn.execute("SELECT * FROM goals ORDER BY COALESCE(deadline, '9999'), id").fetchall()
    return [Goal(r["id"], r["name"], float(r["target"]), float(r["saved"]), r["deadline"]) for r in rows]


def add_goal(conn: sqlite3.Connection, name: str, target: float, saved: float = 0.0,
             deadline: str | None = None) -> int:
    name = " ".join(name.split())[:60]
    if not name:
        raise ValueError("ponle un nombre a la meta")
    if target <= 0 or saved < 0:
        raise ValueError("la meta tiene que ser mayor que cero")
    if deadline:
        try:
            datetime.strptime(deadline, "%Y-%m")
        except ValueError:
            raise ValueError("la fecha va como AAAA-MM") from None
        if deadline < date.today().isoformat()[:7]:
            raise ValueError("la fecha ya ha pasado")
    with conn:
        cur = conn.execute(
            "INSERT INTO goals (name, target, saved, deadline, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, target, saved, deadline or None, datetime.now().isoformat(timespec="seconds")),
        )
    return int(cur.lastrowid)


def set_saved(conn: sqlite3.Connection, goal_id: int, saved: float) -> None:
    if saved < 0:
        raise ValueError("no puede ser negativo")
    with conn:
        conn.execute("UPDATE goals SET saved = ? WHERE id = ?", (saved, goal_id))


def delete_goal(conn: sqlite3.Connection, goal_id: int) -> None:
    with conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
