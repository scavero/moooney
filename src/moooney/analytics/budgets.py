"""Presupuestos mensuales por categoría.

- `propose_budgets`: mediana del gasto de los últimos meses completos por categoría,
  con un recorte objetivo en las categorías variables (las fijas, dominadas por
  recurrentes, no se recortan). Es una propuesta: el usuario la edita.
- Los límites viven en `config/budgets.yaml` (ignorado por git: son datos del usuario)
  y se materializan por mes en la tabla `budgets` para conservar el histórico.
- `budget_status`: gastado vs límite del mes, con proyección a fin de mes.
"""

import math
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from moooney.analytics import periods
from moooney.analytics.metrics import EXPENSE_FLOWS, _spend_by_category
from moooney.analytics.recurring import active_keys

DEFAULT_CUT = 0.10
FIXED_SHARE = 0.70  # si ≥70 % del gasto de la categoría es recurrente, no se recorta
EXCLUDED_CATEGORIES = {"Transferencias", "Ahorro e inversion", "Ingresos", "(sin categoría)"}
ROUND_TO = 5.0


@dataclass
class BudgetRow:
    category: str
    limit: float
    spent: float
    projected: float | None = None

    @property
    def pct(self) -> float:
        return (self.spent / self.limit * 100.0) if self.limit > 0 else 0.0

    @property
    def remaining(self) -> float:
        return self.limit - self.spent


def _round_up(value: float) -> float:
    return math.ceil(value / ROUND_TO) * ROUND_TO


def propose_budgets(conn: sqlite3.Connection, month: str, *, months: int = 6,
                    cut: float = DEFAULT_CUT, as_of: str | None = None,
                    first: str | None = None) -> dict[str, dict]:
    """Propuesta {categoria: {limit, median, fixed_share, cut_applied}} sobre meses completos."""
    as_of = as_of or periods.as_of_date(conn) or month + "-01"
    history = [
        m for m in periods.last_n_months(month, months) if periods.is_full_month(m, as_of, first)
    ]
    if not history:
        return {}
    spend: dict[str, list[float]] = {}
    fixed: dict[str, list[float]] = {}
    keys = active_keys(conn)
    for m in history:
        by_cat = _spend_by_category(conn, m)
        for cat, v in by_cat.items():
            spend.setdefault(cat, []).append(max(v, 0.0))
        if keys:
            f_day, l_day = periods.month_bounds(m)
            ph = ",".join("?" for _ in keys)
            fph = ",".join("?" for _ in EXPENSE_FLOWS)
            for r in conn.execute(
                f"SELECT COALESCE(category, '(sin categoría)') AS c, -SUM(amount) AS g"
                f" FROM transactions WHERE date BETWEEN ? AND ? AND flow_type IN ({fph})"
                f" AND concept_clean IN ({ph}) GROUP BY category",
                (f_day, l_day, *EXPENSE_FLOWS, *keys),
            ):
                fixed.setdefault(r["c"], []).append(max(float(r["g"]), 0.0))
    out: dict[str, dict] = {}
    for cat, values in spend.items():
        if cat in EXCLUDED_CATEGORIES:
            continue
        padded = values + [0.0] * (len(history) - len(values))
        median = statistics.median(padded)
        if median <= 0:
            continue
        fixed_total = sum(fixed.get(cat, []))
        fixed_share = fixed_total / sum(padded) if sum(padded) else 0.0
        cut_applied = cut if fixed_share < FIXED_SHARE else 0.0
        out[cat] = {
            "limit": _round_up(median * (1 - cut_applied)),
            "median": round(median, 2),
            "fixed_share": round(fixed_share, 2),
            "cut_applied": cut_applied,
        }
    return dict(sorted(out.items(), key=lambda kv: -kv[1]["limit"]))


# ---------- config/budgets.yaml ----------


def load_budgets_config(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    limits = data.get("limits") or {}
    return {str(k): float(v) for k, v in limits.items()}


def save_budgets_config(path: Path, limits: dict[str, float], *, meta: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "# nota": "Límites mensuales por categoría en euros. Edita a mano o con `moooney budget set`.",
        "updated_at": date.today().isoformat(),
        **(meta or {}),
        "limits": {k: float(v) for k, v in limits.items()},
    }
    path.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")


# ---------- tabla budgets ----------


def materialize_month(conn: sqlite3.Connection, month: str, limits: dict[str, float],
                      source: str) -> int:
    """Guarda (o actualiza) los límites del mes en la tabla budgets. Devuelve filas."""
    with conn:
        conn.executemany(
            """
            INSERT INTO budgets (category, month, limit_amount, source) VALUES (?, ?, ?, ?)
            ON CONFLICT(category, month) DO UPDATE SET
                limit_amount = excluded.limit_amount, source = excluded.source
            """,
            [(cat, month, lim, source) for cat, lim in limits.items()],
        )
    return len(limits)


def limits_for_month(conn: sqlite3.Connection, month: str, fallback: dict[str, float]) -> dict[str, float]:
    rows = conn.execute(
        "SELECT category, limit_amount FROM budgets WHERE month = ?", (month,)
    ).fetchall()
    if rows:
        return {r["category"]: float(r["limit_amount"]) for r in rows}
    return dict(fallback)


def _fixed_spend_by_category(conn: sqlite3.Connection, month: str) -> dict[str, float]:
    keys = active_keys(conn)
    if not keys:
        return {}
    f_day, l_day = periods.month_bounds(month)
    ph = ",".join("?" for _ in keys)
    fph = ",".join("?" for _ in EXPENSE_FLOWS)
    rows = conn.execute(
        f"SELECT COALESCE(category, '(sin categoría)') AS c, -SUM(amount) AS g FROM transactions"
        f" WHERE date BETWEEN ? AND ? AND flow_type IN ({fph}) AND concept_clean IN ({ph})"
        f" GROUP BY category",
        (f_day, l_day, *EXPENSE_FLOWS, *keys),
    ).fetchall()
    return {r["c"]: max(float(r["g"]), 0.0) for r in rows}


def budget_status(conn: sqlite3.Connection, month: str, limits: dict[str, float], *,
                  as_of: str | None = None) -> list[BudgetRow]:
    """Gastado vs límite. La proyección solo extrapola la parte variable: lo ya
    pagado en recurrentes (alquiler, cuotas) no se vuelve a pagar este mes."""
    spent = _spend_by_category(conn, month)
    as_of = as_of or periods.as_of_date(conn)
    projected_factor: float | None = None
    fixed: dict[str, float] = {}
    if as_of and periods.month_of(as_of) == month:
        day = int(as_of[8:10])
        projected_factor = periods.days_in_month(month) / day if day else None
        fixed = _fixed_spend_by_category(conn, month)
    out: list[BudgetRow] = []
    for cat, lim in limits.items():
        s = max(spent.get(cat, 0.0), 0.0)
        projected = None
        if projected_factor:
            f = min(fixed.get(cat, 0.0), s)
            projected = f + (s - f) * projected_factor
        out.append(BudgetRow(category=cat, limit=lim, spent=s, projected=projected))
    out.sort(key=lambda r: -r.pct)
    return out
