"""Recibos que suben de precio (decisión 2026-11-11). Determinista, sin modelos.

Dos casos, porque un recibo fijo y uno variable suben de forma distinta:

- **Escalón** (`step`): cuota fija que cambia de precio y se queda ahí, como una suscripción
  que pasa de 12,99 € a 15,99 €. Se busca el último cambio: los cargos desde el final que
  repiten el mismo importe (±2 %) son el precio nuevo, y antes debe haber al menos dos cargos
  iguales entre sí con el precio viejo. Así un recibo variable no da escalones falsos y la
  subida se sigue viendo aunque la media ya se haya puesto al día.
- **Tendencia** (`trend`): recibo variable, como la luz, cuyos tres últimos cargos promedian
  bastante más que los anteriores (hasta doce).

El coste se da al año (`delta_year`), que es como se decide si merece la pena hacer algo.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import dataclass

SAME_PRICE = 0.02  # dos cargos tienen el mismo precio si difieren menos de un 2 %
STEP_MIN_PCT = 0.03  # subida mínima de un escalón
STEP_MIN_EUROS = 0.50
STEP_MIN_BEFORE = 2  # cargos iguales con el precio viejo
TREND_RECENT = 3
TREND_BASE_MIN = 4
TREND_BASE_MAX = 12
TREND_MIN_PCT = 0.15
TREND_MIN_EUROS = 5.0
TREND_MAX_PERIOD = 66  # mensual o bimestral: con periodos más largos no hay serie suficiente


@dataclass
class PriceChange:
    key: str
    kind: str  # "step" | "trend"
    category: str | None
    period_label: str
    period_days: int
    old: float  # euros por cargo, en positivo
    new: float
    since: str  # fecha del primer cargo con el precio nuevo (o del primero de los recientes)
    n_since: int

    @property
    def pct(self) -> float:
        return (self.new / self.old - 1) * 100

    @property
    def delta_year(self) -> float:
        """Euros más al año al nuevo precio."""
        return (self.new - self.old) * 365 / self.period_days


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= SAME_PRICE * max(a, b)


def find_step(amounts: list[float]) -> tuple[float, float, int] | None:
    """(precio viejo, precio nuevo, índice del primer cargo nuevo) o None. Importes en positivo."""
    if len(amounts) < STEP_MIN_BEFORE + 1:
        return None
    new = amounts[-1]
    j = len(amounts) - 1
    while j > 0 and _same(amounts[j - 1], new):
        j -= 1
    if j == 0:
        return None  # siempre el mismo precio
    old = amounts[j - 1]
    i = j - 1
    while i > 0 and _same(amounts[i - 1], old):
        i -= 1
    if j - i < STEP_MIN_BEFORE:
        return None  # el precio viejo no era estable: es un recibo variable
    if new < old * (1 + STEP_MIN_PCT) or new - old < STEP_MIN_EUROS:
        return None
    return old, new, j


def find_trend(amounts: list[float]) -> tuple[float, float] | None:
    """(media anterior, media reciente) si los últimos cargos suben claramente."""
    recent = amounts[-TREND_RECENT:]
    base = amounts[:-TREND_RECENT][-TREND_BASE_MAX:]
    if len(recent) < TREND_RECENT or len(base) < TREND_BASE_MIN:
        return None
    old, new = statistics.mean(base), statistics.mean(recent)
    if old <= 0 or new < old * (1 + TREND_MIN_PCT) or new - old < TREND_MIN_EUROS:
        return None
    return old, new


def detect_price_changes(conn: sqlite3.Connection) -> list[PriceChange]:
    """Subidas de los recurrentes de gasto activos, de la que más cuesta al año a la que menos."""
    out: list[PriceChange] = []
    # solo gastos: la liquidación de la tarjeta sube o baja con lo que compras, no es un precio
    for r in conn.execute("SELECT * FROM recurring WHERE active = 1 AND flow_type = 'expense'"):
        # cargos del mismo día se suman: es una factura partida, no dos cobros
        rows = conn.execute(
            "SELECT date, -SUM(amount) AS a FROM transactions WHERE concept_clean = ? AND amount < 0 "
            "GROUP BY date ORDER BY date",
            (r["key"],),
        ).fetchall()
        dates = [x["date"] for x in rows]
        amounts = [float(x["a"]) for x in rows]
        base = dict(key=r["key"], category=r["category"], period_label=r["period_label"],
                    period_days=int(r["period_days"]))
        step = find_step(amounts)
        if step is not None:
            old, new, j = step
            out.append(PriceChange(kind="step", old=old, new=new, since=dates[j], n_since=len(amounts) - j, **base))
            continue
        # sin escalón claro, la tendencia: también sirve para un recibo casi fijo que va subiendo poco a poco
        if int(r["period_days"]) <= TREND_MAX_PERIOD:
            trend = find_trend(amounts)
            if trend is not None:
                old, new = trend
                out.append(PriceChange(kind="trend", old=old, new=new, since=dates[-TREND_RECENT],
                                       n_since=TREND_RECENT, **base))
    out.sort(key=lambda p: p.delta_year, reverse=True)
    return out
