"""Detección determinista de cargos e ingresos recurrentes.

Una clave de comercio es recurrente si sus apariciones siguen un periodo
reconocible (semanal, quincenal, mensual, bimestral, trimestral, anual) con
regularidad suficiente. El importe puede variar (una factura de luz es
recurrente aunque cambie): `amount_cv` mide esa variabilidad y `fixed` marca
las de importe estable. Nada de esto usa modelos.
"""

import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, timedelta

# (etiqueta, mínimo, máximo) en días para el intervalo mediano entre apariciones.
PERIODS: list[tuple[str, int, int]] = [
    ("semanal", 5, 9),
    ("quincenal", 12, 17),
    ("mensual", 26, 35),
    ("bimestral", 55, 66),
    ("trimestral", 83, 97),
    ("anual", 345, 385),
]
MIN_OCCURRENCES = 3
MIN_OCCURRENCES_YEARLY = 2
REGULARITY_TOLERANCE = 0.30  # fracción del periodo que puede desviarse cada intervalo
MIN_REGULAR_SHARE = 0.70  # proporción de intervalos que deben caer dentro de la tolerancia
STABLE_AMOUNT_CV = 0.15  # coeficiente de variación (MAD/mediana) para considerar importe fijo
ACTIVE_FACTOR = 1.6  # activa si no han pasado más de 1,6 periodos desde la última aparición
FLOWS = ("expense", "income", "investment", "card_settlement")


@dataclass
class RecurringItem:
    key: str
    category: str | None
    flow_type: str
    period_days: int
    period_label: str
    n: int
    amount_avg: float
    amount_last: float
    amount_cv: float
    first_seen: str
    last_seen: str
    next_expected: str
    active: bool

    @property
    def fixed(self) -> bool:
        return self.amount_cv <= STABLE_AMOUNT_CV


def _label_for(median_gap: float) -> tuple[str, int] | None:
    for label, lo, hi in PERIODS:
        if lo <= median_gap <= hi:
            return label, round(median_gap)
    return None


def classify(dates: list[str], amounts: list[float]) -> tuple[str, int, float] | None:
    """(etiqueta, periodo_dias, amount_cv) si la serie es recurrente; None si no."""
    if len(dates) < MIN_OCCURRENCES_YEARLY:
        return None
    ds = sorted(date.fromisoformat(d) for d in dates)
    gaps = [(b - a).days for a, b in zip(ds, ds[1:], strict=False)]
    gaps = [g for g in gaps if g > 0]  # mismo día = misma factura partida, no cuenta
    if not gaps:
        return None
    median_gap = statistics.median(gaps)
    found = _label_for(median_gap)
    if found is None:
        return None
    label, period = found
    if label != "anual" and len(dates) < MIN_OCCURRENCES:
        return None
    tol = max(period * REGULARITY_TOLERANCE, 3)
    regular = sum(1 for g in gaps if abs(g - period) <= tol)
    if regular / len(gaps) < MIN_REGULAR_SHARE:
        return None
    abs_amounts = [abs(a) for a in amounts]
    med = statistics.median(abs_amounts)
    if med == 0:
        return None
    mad = statistics.median(abs(a - med) for a in abs_amounts)
    return label, period, mad / med


def detect_recurring(conn: sqlite3.Connection, as_of: str) -> list[RecurringItem]:
    """Recorre todas las claves y devuelve las recurrentes (activas o no)."""
    placeholders = ",".join("?" for _ in FLOWS)
    rows = conn.execute(
        f"""
        SELECT concept_clean AS k, date, amount, category, flow_type
        FROM transactions
        WHERE concept_clean IS NOT NULL AND flow_type IN ({placeholders})
        ORDER BY concept_clean, date
        """,
        FLOWS,
    ).fetchall()
    series: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        series.setdefault(r["k"], []).append(r)

    ref = date.fromisoformat(as_of)
    items: list[RecurringItem] = []
    for key, txs in series.items():
        dates = [t["date"] for t in txs]
        amounts = [float(t["amount"]) for t in txs]
        result = classify(dates, amounts)
        if result is None:
            continue
        label, period, cv = result
        last = date.fromisoformat(max(dates))
        next_expected = last + timedelta(days=period)
        active = (ref - last).days <= period * ACTIVE_FACTOR
        items.append(
            RecurringItem(
                key=key,
                category=txs[-1]["category"],
                flow_type=txs[-1]["flow_type"],
                period_days=period,
                period_label=label,
                n=len(txs),
                amount_avg=statistics.median(amounts),
                amount_last=amounts[-1],
                amount_cv=round(cv, 4),
                first_seen=min(dates),
                last_seen=max(dates),
                next_expected=next_expected.isoformat(),
                active=active,
            )
        )
    items.sort(key=lambda i: (not i.active, i.amount_avg))
    return items


def store_recurring(conn: sqlite3.Connection, items: list[RecurringItem], updated_at: str) -> int:
    """Sustituye la tabla recurring por la detección actual. Devuelve cuántas filas."""
    with conn:
        conn.execute("DELETE FROM recurring")
        conn.executemany(
            """
            INSERT INTO recurring (key, category, flow_type, period_days, period_label, n,
                amount_avg, amount_last, amount_cv, first_seen, last_seen, next_expected,
                active, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    i.key, i.category, i.flow_type, i.period_days, i.period_label, i.n,
                    i.amount_avg, i.amount_last, i.amount_cv, i.first_seen, i.last_seen,
                    i.next_expected, int(i.active), updated_at,
                )
                for i in items
            ],
        )
    return len(items)


def load_recurring(conn: sqlite3.Connection, *, active_only: bool = True) -> list[sqlite3.Row]:
    where = "WHERE active = 1" if active_only else ""
    return conn.execute(
        f"SELECT * FROM recurring {where} ORDER BY active DESC, amount_avg ASC"
    ).fetchall()


def active_keys(conn: sqlite3.Connection) -> set[str]:
    return {r["key"] for r in conn.execute("SELECT key FROM recurring WHERE active = 1")}
