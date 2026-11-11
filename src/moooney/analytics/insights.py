"""Diagnóstico y recomendaciones deterministas a partir de métricas, recurrentes y previsión.

Cada hallazgo lleva un impacto estimado en €/mes para poder ordenarlos por lo
que de verdad importa. Nada aquí usa modelos: cada frase se puede reproducir
con una consulta. La redacción con modelo (fase 5) parte de esta misma lista.
"""

import sqlite3
from dataclasses import dataclass

from moooney.analytics import periods
from moooney.analytics.forecast import (
    forecast_month_end,
    full_months_before,
    runway_months,
)
from moooney.analytics.metrics import (
    EXPENSE_FLOWS,
    MetricsReport,
    _spend_by_category,
    build_report,
)

MIN_IMPACT_EUR = 15.0  # por debajo de esto una variación por categoría no merece una frase
MIN_PCT_CHANGE = 0.15
SMALL_RECURRING_EUR = 25.0


@dataclass
class Insight:
    kind: str
    severity: str  # good | info | warn | bad
    title: str
    detail: str
    impact: float = 0.0  # €/mes que están en juego (positivo = ahorro potencial o mejora)
    link: str | None = None

    @property
    def tone(self) -> str:
        return {"good": "ok", "info": "info", "warn": "warn", "bad": "crit"}[self.severity]


def _eur(v: float) -> str:
    s = f"{v:,.0f}".replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    return f"{s} €"


def category_insights(report: MetricsReport, month: str) -> list[Insight]:
    """Categorías muy por encima o por debajo de su media de meses completos.

    Si el mes está incompleto se compara a prorrata de los días transcurridos y
    se omiten las categorías dominadas por cargos fijos (un alquiler pagado el
    día 2 no "se dispara" el día 10)."""
    out: list[Insight] = []
    full = report.summary.full_month
    ratio = 1.0
    if not full:
        day = int(report.as_of[8:10])
        ratio = day / periods.days_in_month(month)
    for c in report.categories:
        if c.category in ("Transferencias", "(sin categoría)") or c.avg_prev is None or c.n_prev_months < 2:
            continue
        if not full and c.amount > 0 and c.fixed_amount / c.amount > 0.5:
            continue
        base = (c.avg_prev or 0.0) * ratio
        delta = c.amount - base
        pct = delta / base if base > 0 else (1.0 if delta > 0 else 0.0)
        cuando = "" if full else f" a día {report.as_of[8:10]}"
        # con el mes a medias la media también va a prorrata, y lo que «libera» es una extrapolación
        hab = "habituales" if full else "habituales hasta ese día"
        if delta > MIN_IMPACT_EUR and pct > MIN_PCT_CHANGE:
            out.append(Insight(
                kind="category_up", severity="warn",
                title=f"{c.category} se dispara{cuando}: {_eur(c.amount)} frente a {_eur(base)} {hab}",
                detail=f"+{pct:.0%} sobre los últimos {c.n_prev_months} meses completos"
                       f"{' (a prorrata de días)' if not full else ''}. "
                       + (f"Volver a la media libera {_eur(delta / ratio)} al mes." if full else
                        f"Si el resto del mes sigue igual, serían {_eur(delta / ratio)} más que un mes normal."),
                impact=delta / ratio,
                link=f"/transactions?month={month}&category={c.category}",
            ))
        elif delta < -MIN_IMPACT_EUR and pct < -MIN_PCT_CHANGE and base > 0:
            out.append(Insight(
                kind="category_down", severity="good",
                title=f"{c.category} baja{cuando}: {_eur(c.amount)} frente a {_eur(base)} {hab}",
                detail=f"{pct:.0%} respecto a los últimos {c.n_prev_months} meses"
                       f"{' (a prorrata de días)' if not full else ''}. Si se mantiene, son {_eur(-delta / ratio)} menos al mes.",
                impact=-delta / ratio,
                link=f"/transactions?month={month}&category={c.category}",
            ))
    return out


def recurring_insights(conn: sqlite3.Connection) -> list[Insight]:
    out: list[Insight] = []
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    rows = conn.execute(
        f"SELECT key, category, period_days, amount_avg FROM recurring WHERE active = 1"
        f" AND flow_type IN ({ph}) ORDER BY ABS(amount_avg) ASC",
        EXPENSE_FLOWS,
    ).fetchall()
    monthly = [(r["key"], r["category"], abs(float(r["amount_avg"])) * 30.0 / r["period_days"]) for r in rows]
    total_fixed = sum(m for _, _, m in monthly)
    small = [(k, c, m) for k, c, m in monthly if m <= SMALL_RECURRING_EUR]
    if len(small) >= 3:
        s = sum(m for _, _, m in small)
        out.append(Insight(
            kind="small_recurring", severity="info",
            title=f"{len(small)} cargos pequeños recurrentes suman {_eur(s)} al mes",
            detail=f"Son {_eur(s * 12)} al año: " + ", ".join(k for k, _, _ in small[:6]) + ("…" if len(small) > 6 else "")
                   + ". Revisa cuáles usas de verdad.",
            impact=s, link="/recurring",
        ))
    subs = [(k, m) for k, c, m in monthly if c == "Suscripciones"]
    if subs:
        s = sum(m for _, m in subs)
        out.append(Insight(
            kind="subscriptions", severity="info",
            title=f"Suscripciones: {_eur(s)} al mes, {_eur(s * 12)} al año",
            detail=", ".join(k for k, _ in subs[:6]) + ("…" if len(subs) > 6 else ""),
            impact=s, link="/transactions?category=Suscripciones",
        ))
    if total_fixed > 0:
        out.append(Insight(
            kind="fixed_total", severity="info",
            title=f"Tus gastos fijos rondan {_eur(total_fixed)} al mes",
            detail=f"{len(monthly)} cargos recurrentes activos. Es el suelo de gasto antes de decidir nada.",
            impact=0.0, link="/recurring",
        ))
    return out


def fees_insight(conn: sqlite3.Connection, as_of: str) -> Insight | None:
    since = periods.month_of(as_of)
    y, m = (int(x) for x in since.split("-"))
    start = f"{y - 1}-{m:02d}-01"
    row = conn.execute(
        "SELECT COALESCE(-SUM(amount), 0) AS s, COUNT(*) AS n FROM transactions"
        " WHERE category = 'Comisiones bancarias' AND amount < 0 AND date >= ?",
        (start,),
    ).fetchone()
    if float(row["s"]) <= 0:
        return None
    return Insight(
        kind="fees", severity="warn",
        title=f"El banco te ha cobrado {_eur(float(row['s']))} en comisiones en 12 meses",
        detail=f"{row['n']} cargos. Casi siempre se pueden evitar cambiando de cuenta o de condiciones.",
        impact=float(row["s"]) / 12.0, link="/transactions?category=Comisiones%20bancarias",
    )


def otros_insight(conn: sqlite3.Connection, report: MetricsReport, month: str) -> Insight | None:
    otros = next((c for c in report.categories if c.category == "Otros"), None)
    if otros is None or otros.pct < 8:
        return None
    return Insight(
        kind="otros", severity="info",
        title=f"{otros.pct:.0f} % del gasto del mes está en Otros ({_eur(otros.amount)})",
        detail="No se puede decidir sobre lo que no se identifica. Revisa esos comercios y corrígelos: "
               "tus correcciones se recuerdan para siempre.",
        impact=0.0, link="/merchants?mode=otros",
    )


def restaurants_insight(conn: sqlite3.Connection, month: str) -> Insight | None:
    first, last = periods.month_bounds(month)
    row = conn.execute(
        "SELECT COALESCE(-SUM(CASE WHEN subcategory = 'restaurantes' THEN amount END), 0) AS fuera,"
        " COALESCE(-SUM(amount), 0) AS total FROM transactions"
        " WHERE category = 'Alimentacion' AND amount < 0 AND date BETWEEN ? AND ?",
        (first, last),
    ).fetchone()
    fuera, total = float(row["fuera"]), float(row["total"])
    if total <= 0 or fuera / total < 0.35 or fuera < 60:
        return None
    return Insight(
        kind="eating_out", severity="info",
        title=f"Comer fuera es el {fuera / total:.0%} de tu gasto en alimentación ({_eur(fuera)})",
        detail="Reducirlo a la mitad serían " + _eur(fuera / 2) + " al mes.",
        impact=fuera / 2, link=f"/transactions?month={month}&category=Alimentacion",
    )


def savings_insights(conn: sqlite3.Connection, report: MetricsReport, as_of: str, first: str | None) -> list[Insight]:
    out: list[Insight] = []
    history = full_months_before(conn, report.month, 6, as_of=as_of, first=first)
    if len(history) >= 3:
        rates = [h.savings_rate for h in history if h.savings_rate is not None]
        negative = [h.month for h in history if h.net < 0]
        if rates:
            avg_rate = sum(rates) / len(rates)
            recent = rates[-1]
            if avg_rate >= 0.2:
                sev, txt = "good", "Buen ritmo: por encima del 20 % recomendado."
            elif avg_rate >= 0.1:
                sev, txt = "info", "Aceptable; el objetivo habitual es el 20 %."
            else:
                sev, txt = "warn", "Por debajo del 10 %: poco margen ante un imprevisto."
            out.append(Insight(
                kind="savings_rate", severity=sev,
                title=f"Tasa de ahorro media de {avg_rate:.0%} en los últimos {len(rates)} meses completos",
                detail=f"{txt} Último mes completo: {recent:.0%}.",
                impact=0.0,
            ))
        if negative:
            out.append(Insight(
                kind="negative_months", severity="bad",
                title=f"{len(negative)} de los últimos {len(history)} meses gastaste más de lo que ingresaste",
                detail="Meses en rojo: " + ", ".join(negative) + ".",
                impact=0.0,
            ))
        variable = [h.variable_expenses for h in history]
        if len(variable) >= 4:
            older, newer = variable[: len(variable) // 2], variable[len(variable) // 2:]
            a, b = sum(older) / len(older), sum(newer) / len(newer)
            if a > 0 and (b - a) / a > MIN_PCT_CHANGE and b - a > MIN_IMPACT_EUR:
                out.append(Insight(
                    kind="variable_trend", severity="warn",
                    title=f"El gasto variable va a más: {_eur(b)} al mes frente a {_eur(a)} hace unos meses",
                    detail="Tendencia de los últimos meses completos, sin contar recurrentes.",
                    impact=b - a, link="/trends",
                ))
    runway = runway_months(conn, as_of, first=first)
    if runway is not None:
        if runway < 1:
            sev, txt = "bad", "Menos de un mes de colchón: cualquier imprevisto te deja en negativo."
        elif runway < 3:
            sev, txt = "warn", "Lo habitual es tener de 3 a 6 meses de gastos ahorrados."
        else:
            sev, txt = "good", "Colchón razonable."
        out.append(Insight(
            kind="runway", severity=sev,
            title=f"Tu saldo cubre {runway:.1f} meses de gasto",
            detail=txt, impact=0.0,
        ))
    return out


def top_merchant_insight(conn: sqlite3.Connection, month: str, report: MetricsReport) -> Insight | None:
    first, last = periods.month_bounds(month)
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    row = conn.execute(
        f"SELECT concept_clean AS k, COUNT(*) AS n, -SUM(amount) AS t FROM transactions"
        f" WHERE date BETWEEN ? AND ? AND flow_type IN ({ph}) AND amount < 0"
        f" AND category NOT IN ('Vivienda', 'Tarjeta de credito', 'Transferencias')"
        f" GROUP BY concept_clean ORDER BY t DESC LIMIT 1",
        (first, last, *EXPENSE_FLOWS),
    ).fetchone()
    if row is None or report.summary.expenses <= 0:
        return None
    share = float(row["t"]) / report.summary.expenses
    if share < 0.08:
        return None
    return Insight(
        kind="top_merchant", severity="info",
        title=f"{row['k']} se lleva el {share:.0%} de tu gasto del mes ({_eur(float(row['t']))}, {row['n']} veces)",
        detail="Fuera de vivienda y tarjeta, es donde más se te va.",
        impact=0.0, link=f"/transactions?month={month}&key={row['k']}",
    )


def forecast_insight(conn: sqlite3.Connection, as_of: str, first: str | None, month: str) -> Insight | None:
    if periods.month_of(as_of) != month:
        return None
    f = forecast_month_end(conn, as_of, first=first)
    if f.history_months == 0:
        return None
    sev = "good" if f.net >= 0 else "bad"
    return Insight(
        kind="month_end", severity=sev,
        title=f"Previsión a fin de mes: {_eur(f.expenses)} de gasto y {_eur(f.net)} de ahorro",
        detail=f"Llevas {_eur(f.spent)} el día {f.day} de {f.days_in_month}; quedan {_eur(f.fixed_pending)} en recurrentes "
               f"y unos {_eur(f.variable_expected_rest)} de gasto variable al ritmo de los últimos {f.history_months} meses.",
        impact=0.0, link="/trends",
    )


def build_insights(conn: sqlite3.Connection, month: str | None = None) -> tuple[list[Insight], MetricsReport]:
    report = build_report(conn, month, n_months=12)
    as_of, m, first = report.as_of, report.month, periods.first_date(conn)
    items: list[Insight] = []
    fi = forecast_insight(conn, as_of, first, m)
    if fi:
        items.append(fi)
    items += category_insights(report, m)
    for maybe in (top_merchant_insight(conn, m, report), restaurants_insight(conn, m),
                  otros_insight(conn, report, m), fees_insight(conn, as_of)):
        if maybe:
            items.append(maybe)
    items += recurring_insights(conn)
    items += savings_insights(conn, report, as_of, first)
    order = {"bad": 0, "warn": 1, "good": 2, "info": 3}
    items.sort(key=lambda i: (order[i.severity], -i.impact))
    return items, report


def spend_share_12m(conn: sqlite3.Connection, as_of: str) -> list[tuple[str, float, float]]:
    """(categoría, total 12 meses, %) ordenado: dónde se ha ido el dinero en un año."""
    month = periods.month_of(as_of)
    months = [*periods.last_n_months(month, 11), month]
    totals: dict[str, float] = {}
    for mth in months:
        for c, v in _spend_by_category(conn, mth).items():
            totals[c] = totals.get(c, 0.0) + max(v, 0.0)
    grand = sum(totals.values()) or 1.0
    return sorted(((c, v, v / grand * 100) for c, v in totals.items()), key=lambda t: -t[1])
