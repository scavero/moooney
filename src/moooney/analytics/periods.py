"""Utilidades de meses y fechas para la analítica. Todo en ISO (YYYY-MM-DD, YYYY-MM)."""

import calendar
import sqlite3
from datetime import date


def month_of(iso_date: str) -> str:
    return iso_date[:7]


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def next_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y + 1}-01" if m == 12 else f"{y}-{m + 1:02d}"


def last_n_months(month: str, n: int) -> list[str]:
    """Los n meses anteriores a `month` (sin incluirlo), del más antiguo al más reciente."""
    out: list[str] = []
    cur = month
    for _ in range(n):
        cur = prev_month(cur)
        out.append(cur)
    return list(reversed(out))


def month_bounds(month: str) -> tuple[str, str]:
    y, m = (int(x) for x in month.split("-"))
    last = calendar.monthrange(y, m)[1]
    return f"{month}-01", f"{month}-{last:02d}"


def days_in_month(month: str) -> int:
    y, m = (int(x) for x in month.split("-"))
    return calendar.monthrange(y, m)[1]


def as_of_date(conn: sqlite3.Connection) -> str | None:
    """Fecha de referencia de la analítica: el último movimiento cargado."""
    row = conn.execute("SELECT MAX(date) AS d FROM transactions").fetchone()
    return row["d"] if row and row["d"] else None


def first_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MIN(date) AS d FROM transactions").fetchone()
    return row["d"] if row and row["d"] else None


def is_full_month(month: str, as_of: str, first: str | None = None) -> bool:
    """El mes está completo en los datos: termina antes de as_of y empieza después del primer dato."""
    _, last = month_bounds(month)
    first_day, _ = month_bounds(month)
    if last > as_of:
        return False
    if first is not None and first_day < first:
        return False
    return True


def iso(d: date) -> str:
    return d.isoformat()
