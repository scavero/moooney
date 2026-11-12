"""Utilidades de meses y fechas para la analítica. Todo en ISO (YYYY-MM-DD, YYYY-MM)."""

import calendar
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta


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


# ---------- periodos: mes, trimestre, año o rango libre ----------

_KEY_MONTH = re.compile(r"^(\d{4})-(0[1-9]|1[0-2])$")
_KEY_QUARTER = re.compile(r"^(\d{4})-[Qq]([1-4])$")
_KEY_YEAR = re.compile(r"^(\d{4})$")
_KEY_RANGE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.\.(\d{4}-\d{2}-\d{2})$")
KINDS = ("month", "quarter", "year", "range")


@dataclass(frozen=True)
class Period:
    """Un tramo de fechas con nombre. `key` es lo que viaja en la URL:
    `2026-10` (mes), `2026-Q4` (trimestre), `2026` (año) o `2026-01-15..2026-03-10` (rango)."""

    kind: str
    key: str
    start: str
    end: str

    @property
    def months(self) -> list[str]:
        out, cur = [], month_of(self.start)
        while cur <= month_of(self.end):
            out.append(cur)
            cur = next_month(cur)
        return out

    @property
    def days(self) -> int:
        return (date.fromisoformat(self.end) - date.fromisoformat(self.start)).days + 1


def month_period(month: str) -> Period:
    first, last = month_bounds(month)
    return Period("month", month, first, last)


def quarter_period(year: int, quarter: int) -> Period:
    m0 = 3 * (quarter - 1) + 1
    first, _ = month_bounds(f"{year}-{m0:02d}")
    _, last = month_bounds(f"{year}-{m0 + 2:02d}")
    return Period("quarter", f"{year}-Q{quarter}", first, last)


def year_period(year: int) -> Period:
    return Period("year", str(year), f"{year}-01-01", f"{year}-12-31")


def range_period(start: str, end: str) -> Period | None:
    """Rango libre entre dos fechas ISO (en cualquier orden). None si alguna no es una fecha."""
    try:
        a, b = sorted((date.fromisoformat(start), date.fromisoformat(end)))
    except (TypeError, ValueError):
        return None
    return Period("range", f"{a.isoformat()}..{b.isoformat()}", a.isoformat(), b.isoformat())


def parse_period(key: str | None) -> Period | None:
    """Periodo a partir de su clave de URL; None si no se reconoce."""
    key = (key or "").strip()
    if _KEY_MONTH.match(key):
        return month_period(key)
    if m := _KEY_QUARTER.match(key):
        return quarter_period(int(m.group(1)), int(m.group(2)))
    if m := _KEY_YEAR.match(key):
        return year_period(int(m.group(1)))
    if m := _KEY_RANGE.match(key):
        return range_period(m.group(1), m.group(2))
    return None


def period_containing(kind: str, iso_date: str) -> Period:
    """El mes, trimestre o año que contiene esa fecha."""
    y, m = int(iso_date[:4]), int(iso_date[5:7])
    if kind == "quarter":
        return quarter_period(y, (m - 1) // 3 + 1)
    if kind == "year":
        return year_period(y)
    return month_period(iso_date[:7])


def shift_period(p: Period, n: int) -> Period:
    """El periodo n pasos después (o antes, con n negativo). Un rango se desplaza su propia longitud."""
    if p.kind == "month":
        cur = p.key
        for _ in range(abs(n)):
            cur = next_month(cur) if n > 0 else prev_month(cur)
        return month_period(cur)
    if p.kind == "quarter":
        idx = int(p.key[:4]) * 4 + int(p.key[-1]) - 1 + n
        return quarter_period(idx // 4, idx % 4 + 1)
    if p.kind == "year":
        return year_period(int(p.key) + n)
    delta = timedelta(days=p.days * n)
    a, b = date.fromisoformat(p.start) + delta, date.fromisoformat(p.end) + delta
    return Period("range", f"{a.isoformat()}..{b.isoformat()}", a.isoformat(), b.isoformat())


def add_days(iso_date: str, days: int) -> str:
    return (date.fromisoformat(iso_date) + timedelta(days=days)).isoformat()
