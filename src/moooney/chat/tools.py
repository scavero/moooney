"""Consultas que el asistente puede pedir. Todas deterministas y todas devuelven agregados.

El modelo no hace cuentas: elige una consulta, este módulo la calcula sobre la base y le
devuelve el resultado en JSON. Ninguna consulta devuelve movimientos sueltos, IBAN, saldos de
cuenta ni nombres de personas: solo totales por mes, por categoría o por clave de comercio
normalizada (la misma que ya se usa para categorizar).
"""

import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from moooney.analytics import alerts as alerts_mod
from moooney.analytics import budgets as budgets_mod
from moooney.analytics import forecast as forecast_mod
from moooney.analytics import goals as goals_mod
from moooney.analytics import period_summary as period_mod
from moooney.analytics import periods, yearly
from moooney.analytics.insights import build_insights
from moooney.analytics.metrics import EXPENSE_FLOWS, build_report
from moooney.analytics.price_changes import detect_price_changes
from moooney.analytics.recurring import load_recurring

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


class ToolError(ValueError):
    """Argumentos que no valen. El mensaje vuelve al modelo para que corrija la consulta."""


@dataclass
class Context:
    conn: sqlite3.Connection
    budgets_path: Path | None = None


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    run: Callable[[Context, dict], dict]

    def schema(self) -> dict:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description, "parameters": self.parameters}}


def _r(v: float | None, nd: int = 0) -> float | int | None:
    if v is None:
        return None
    return round(v, nd) if nd else round(v)


def _as_of(ctx: Context) -> str:
    as_of = periods.as_of_date(ctx.conn)
    if as_of is None:
        raise ToolError("no hay movimientos cargados")
    return as_of


def _month(ctx: Context, args: dict) -> str:
    m = (args.get("mes") or "").strip()
    if not m:
        return periods.month_of(_as_of(ctx))
    if not _MONTH.match(m):
        raise ToolError(f"mes con formato AAAA-MM, no «{m}»")
    return m


_MES = {"type": "string", "description": "Mes AAAA-MM. Si se omite, el mes de los últimos datos."}


# ---------- consultas ----------

def resumen_mes(ctx: Context, args: dict) -> dict:
    m = _month(ctx, args)
    rep = build_report(ctx.conn, m)
    s = rep.summary
    return {
        "mes": m, "datos_hasta": rep.as_of, "mes_completo": s.full_month,
        "ingresos": _r(s.income), "gastos": _r(s.expenses), "gastos_fijos": _r(s.fixed_expenses),
        "gastos_variables": _r(s.variable_expenses), "ahorro": _r(s.net),
        "tasa_ahorro_pct": _r(s.savings_rate * 100) if s.savings_rate is not None else None,
        "inversion": _r(s.investment),
        "meses_anteriores": [
            {"mes": x.month, "ingresos": _r(x.income), "gastos": _r(x.expenses), "ahorro": _r(x.net),
             "completo": x.full_month}
            for x in rep.series[:-1]
        ],
    }


def gasto_por_categoria(ctx: Context, args: dict) -> dict:
    m = _month(ctx, args)
    cat = (args.get("categoria") or "").strip()
    rep = build_report(ctx.conn, m)
    rows = [
        {"categoria": c.category, "importe": _r(c.amount), "pct_del_gasto": _r(c.pct),
         "media_meses_anteriores": _r(c.avg_prev), "diferencia_con_media": _r(c.delta_avg),
         "parte_fija": _r(c.fixed_amount)}
        for c in rep.categories
    ]
    out: dict = {"mes": m, "mes_completo": rep.summary.full_month}
    if not cat:
        out["categorias"] = rows
        return out
    match = [r for r in rows if r["categoria"].lower() == cat.lower()]
    known = _categories(ctx.conn)
    name = next((k for k in known if k.lower() == cat.lower()), None)
    if name is None:
        raise ToolError(f"no hay ninguna categoría «{cat}». Existen: {', '.join(known)}")
    out["categoria"] = match[0] if match else {"categoria": name, "importe": 0}
    months = periods.last_n_months(m, 12)
    out["ultimos_12_meses"] = _category_by_month(ctx.conn, name, months)
    return out


def comercios_top(ctx: Context, args: dict) -> dict:
    m = _month(ctx, args)
    cat = (args.get("categoria") or "").strip()
    limit = max(1, min(int(args.get("limite") or 10), 25))
    first, last = periods.month_bounds(m)
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    sql = (f"SELECT concept_clean AS k, category AS c, COUNT(*) AS n, -SUM(amount) AS t FROM transactions"
           f" WHERE date BETWEEN ? AND ? AND flow_type IN ({ph}) AND amount < 0")
    params: list = [first, last, *EXPENSE_FLOWS]
    if cat:
        sql += " AND lower(category) = lower(?)"
        params.append(cat)
    sql += " GROUP BY concept_clean ORDER BY t DESC LIMIT ?"
    params.append(limit)
    rows = ctx.conn.execute(sql, params).fetchall()
    return {"mes": m, "categoria": cat or None, "comercios": [
        {"comercio": r["k"], "categoria": r["c"], "cargos": r["n"], "total": _r(float(r["t"]), 2)} for r in rows]}


def gasto_en_comercio(ctx: Context, args: dict) -> dict:
    text = (args.get("comercio") or "").strip().upper()
    if len(text) < 3:
        raise ToolError("indica al menos tres letras del comercio")
    as_of = _as_of(ctx)
    months = periods.last_n_months(periods.month_of(as_of), int(args.get("meses") or 12))
    first, _ = periods.month_bounds(months[0])
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    rows = ctx.conn.execute(
        f"SELECT concept_clean AS k, substr(date, 1, 7) AS m, COUNT(*) AS n, -SUM(amount) AS t FROM transactions"
        f" WHERE date >= ? AND flow_type IN ({ph}) AND upper(concept_clean) LIKE ? GROUP BY k, m",
        (first, *EXPENSE_FLOWS, f"%{text}%"),
    ).fetchall()
    if not rows:
        return {"comercio": text, "encontrado": False,
                "nota": "ningún gasto con ese texto en la clave del comercio en el periodo"}
    keys = sorted({r["k"] for r in rows})
    by_month = {mm: [0.0, 0] for mm in months}
    for r in rows:
        if r["m"] in by_month:
            by_month[r["m"]][0] += float(r["t"])
            by_month[r["m"]][1] += r["n"]
    total = sum(v[0] for v in by_month.values())
    with_spend = [v[0] for v in by_month.values() if v[1]]
    return {
        "comercio": text, "encontrado": True, "claves": keys[:10],
        "total_periodo": _r(total, 2), "meses_del_periodo": len(months), "meses_con_gasto": len(with_spend),
        "media_por_mes_del_periodo": _r(total / len(months), 2),
        "media_en_meses_con_gasto": _r(total / len(with_spend), 2) if with_spend else 0,
        "nota": "el mes en curso puede estar incompleto",
        "por_mes": [{"mes": mm, "total": _r(v[0], 2), "cargos": v[1]} for mm, v in by_month.items()],
    }


def recurrentes(ctx: Context, args: dict) -> dict:
    changes = {c.key: c for c in detect_price_changes(ctx.conn)}
    out = []
    for r in load_recurring(ctx.conn, active_only=True):
        item = {"comercio": r["key"], "categoria": r["category"], "tipo": r["flow_type"],
                "periodicidad": r["period_label"], "importe": _r(abs(float(r["amount_avg"])), 2),
                "al_mes": _r(abs(float(r["amount_avg"])) * 30 / r["period_days"], 2),
                "proximo_cargo": r["next_expected"]}
        if (c := changes.get(r["key"])) is not None:
            item["cambio_precio"] = {"antes": _r(c.old, 2), "ahora": _r(c.new, 2), "pct": _r(c.pct),
                                     "desde": c.since, "euros_mas_al_anio": _r(c.delta_year)}
        out.append(item)
    gastos = [x for x in out if x["tipo"] != "income"]
    return {"recurrentes": out[:40], "total_gastos_al_mes": _r(sum(x["al_mes"] for x in gastos)),
            "subidas_de_precio": sum(1 for x in out if "cambio_precio" in x)}


def presupuestos(ctx: Context, args: dict) -> dict:
    m = _month(ctx, args)
    limits = budgets_mod.load_budgets_config(ctx.budgets_path) if ctx.budgets_path else {}
    limits = budgets_mod.limits_for_month(ctx.conn, m, limits)
    if not limits:
        return {"mes": m, "hay_presupuesto": False}
    rows = budgets_mod.budget_status(ctx.conn, m, limits, as_of=_as_of(ctx))
    return {"mes": m, "hay_presupuesto": True, "categorias": [
        {"categoria": r.category, "limite": _r(r.limit), "gastado": _r(r.spent), "pct": _r(r.pct),
         "queda": _r(r.remaining), "proyeccion_fin_de_mes": _r(r.projected)} for r in rows]}


def alertas(ctx: Context, args: dict) -> dict:
    rows = alerts_mod.pending_alerts(ctx.conn)
    return {"pendientes": len(rows), "alertas": [
        {"fecha": a["date"], "nivel": a["severity"], "tipo": a["type"], "mensaje": a["message"]} for a in rows[:15]]}


def hallazgos(ctx: Context, args: dict) -> dict:
    m = _month(ctx, args)
    items, _ = build_insights(ctx.conn, m)
    return {"mes": m, "hallazgos": [
        {"nivel": i.severity, "titulo": i.title, "detalle": i.detail, "euros_al_mes": _r(i.impact)} for i in items[:12]]}


def prevision(ctx: Context, args: dict) -> dict:
    as_of = _as_of(ctx)
    first = periods.first_date(ctx.conn)
    f = forecast_mod.forecast_month_end(ctx.conn, as_of, first=first)
    nxt = forecast_mod.forecast_next_months(ctx.conn, as_of, n=3, first=first)
    runway = forecast_mod.runway_months(ctx.conn, as_of, first=first)
    return {
        "datos_hasta": as_of,
        "fin_de_este_mes": {"mes": f.month, "dia": f.day, "dias_del_mes": f.days_in_month,
                            "gastado_ya": _r(f.spent), "fijos_pendientes": _r(f.fixed_pending),
                            "variable_esperado_resto": _r(f.variable_expected_rest),
                            "gastos_previstos": _r(f.expenses), "ingresos_previstos": _r(f.income),
                            "ahorro_previsto": _r(f.net)},
        "proximos_meses": [{"mes": x.month, "gastos": _r(x.expenses), "fijos": _r(x.fixed),
                            "ingresos": _r(x.income), "ahorro": _r(x.net)} for x in nxt],
        "meses_que_aguanta_sin_ingresos": _r(runway, 1),
    }


def resumen_anio(ctx: Context, args: dict) -> dict:
    year = int(args.get("anio") or _as_of(ctx)[:4])
    ys = yearly.year_summary(ctx.conn, year)
    if ys is None:
        raise ToolError(f"no hay datos de {year}. Hay: {', '.join(map(str, yearly.years_available(ctx.conn)))}")

    def tot(t):
        return None if t is None else {"ingresos": _r(t.income), "gastos": _r(t.expenses), "ahorro": _r(t.net)}

    first = periods.first_date(ctx.conn) or ""
    out: dict = {
        "anio": year, "datos_desde": first, "hasta": ys.cutoff, "en_curso": ys.partial, "este_anio": tot(ys.totals),
        "anio_anterior_mismo_periodo": tot(ys.prev_totals),
    }
    if ys.prev_totals is None:
        out["nota"] = (f"sin comparación con {year - 1}: los datos empiezan el {first} y ese año no está "
                       "completo desde enero")
    return out | {
        "categorias": [{"categoria": c.category, "importe": _r(c.amount), "anio_anterior": _r(c.prev),
                        "diferencia": _r(c.delta)} for c in ys.categories[:15]],
        "meses": [{"mes": x.month, "gastos": _r(x.expenses), "ingresos": _r(x.income), "completo": x.full}
                  for x in ys.months if x.has_data],
        "mes_mas_caro": ys.costliest.month if ys.costliest else None,
        "mes_mas_barato": ys.cheapest.month if ys.cheapest else None,
    }


def resumen_periodo(ctx: Context, args: dict) -> dict:
    text = (args.get("periodo") or "").strip()
    desde, hasta = (args.get("desde") or "").strip(), (args.get("hasta") or "").strip()
    if not (text or desde or hasta):  # sin argumentos: el trimestre de los últimos datos
        text = periods.period_containing("quarter", _as_of(ctx)).key
    p = periods.range_period(desde, hasta) if desde and hasta else periods.parse_period(text)
    if p is None:
        raise ToolError("indica «periodo» (AAAA-Q1 a AAAA-Q4, AAAA o AAAA-MM) o «desde» y «hasta» (AAAA-MM-DD)")
    ps = period_mod.period_summary(ctx.conn, p, merchants=10)
    if ps is None:
        raise ToolError(f"no hay datos en ese periodo; los hay del {periods.first_date(ctx.conn)} al {_as_of(ctx)}")

    def tot(t):
        return None if t is None else {"ingresos": _r(t.income), "gastos": _r(t.expenses), "ahorro": _r(t.net),
                                       "inversion": _r(t.investment)}

    out: dict = {"periodo": p.key, "desde": ps.start, "hasta": ps.cutoff, "en_curso": ps.partial,
                 "totales": tot(ps.totals), "periodo_anterior": None}
    if ps.prev is not None:
        out["periodo_anterior"] = {"desde": ps.prev.start, "hasta": ps.prev_cutoff} | tot(ps.prev_totals)
    else:
        out["nota"] = "sin comparación: el periodo anterior no tiene datos desde su primer mes"
    return out | {
        "categorias": [{"categoria": c.category, "importe": _r(c.amount), "periodo_anterior": _r(c.prev),
                        "diferencia": _r(c.delta)} for c in ps.categories[:15]],
        "meses": [{"mes": x.month, "gastos": _r(x.expenses), "ingresos": _r(x.income), "completo": x.full}
                  for x in ps.months if x.has_data],
        "comercios": [{"comercio": r["key"], "categoria": r["category"], "cargos": r["n"],
                       "total": _r(float(r["total"]), 2)} for r in ps.merchants],
    }


def metas(ctx: Context, args: dict) -> dict:
    base = goals_mod.baseline(ctx.conn)
    net = base.net if base else 0.0
    today = date.today().isoformat()
    out = []
    for g in goals_mod.list_goals(ctx.conn):
        p = goals_mod.plan(g, net, today)
        out.append({"meta": g.name, "objetivo": _r(g.target), "apartado": _r(g.saved), "falta": _r(g.missing),
                    "fecha_limite": g.deadline, "llega_en": p.eta, "meses_a_su_ritmo": p.months_to_goal,
                    "necesita_al_mes": _r(p.needed), "a_tiempo": p.on_track})
    res: dict = {"metas": out}
    if base:
        res["mes_medio"] = {
            "desde": base.months[0], "hasta": base.months[-1], "meses_completos": len(base.months), "ingresos": _r(base.income),
            "gastos": _r(base.expenses), "ahorro": _r(net),
            "categorias": [{"categoria": c.category, "total": _r(c.total), "fijo": _r(c.fixed),
                            "variable": _r(c.variable)} for c in base.categories[:12]],
        }
    return res


# ---------- auxiliares ----------

def _categories(conn: sqlite3.Connection) -> list[str]:
    return [r["c"] for r in conn.execute(
        "SELECT DISTINCT category AS c FROM transactions WHERE category IS NOT NULL ORDER BY c")]


def _category_by_month(conn: sqlite3.Connection, category: str, months: list[str]) -> list[dict]:
    first, _ = periods.month_bounds(months[0])
    _, last = periods.month_bounds(months[-1])
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    got = {r["m"]: float(r["g"]) for r in conn.execute(
        f"SELECT substr(date, 1, 7) AS m, -SUM(amount) AS g FROM transactions"
        f" WHERE date BETWEEN ? AND ? AND flow_type IN ({ph}) AND category = ? GROUP BY m",
        (first, last, *EXPENSE_FLOWS, category))}
    return [{"mes": m, "gasto": _r(max(got.get(m, 0.0), 0.0))} for m in months]


def overview(ctx: Context) -> dict:
    """Lo mínimo para orientarse, que va en el mensaje de sistema: fechas y categorías."""
    as_of = periods.as_of_date(ctx.conn)
    return {
        "hoy": date.today().isoformat(),
        "datos_desde": periods.first_date(ctx.conn), "datos_hasta": as_of,
        "categorias": _categories(ctx.conn),
    }


_CAT = {"type": "string", "description": "Nombre exacto de la categoría (ver la lista del sistema)."}

TOOLS: list[Tool] = [
    Tool("resumen_mes", "Ingresos, gastos (fijos y variables), ahorro y tasa de ahorro de un mes, con los "
         "meses anteriores para comparar.", {"type": "object", "properties": {"mes": _MES}}, resumen_mes),
    Tool("gasto_por_categoria", "Gasto de un mes por categoría con su media de meses anteriores. Con "
         "«categoria», además su gasto en los 12 últimos meses.",
         {"type": "object", "properties": {"mes": _MES, "categoria": _CAT}}, gasto_por_categoria),
    Tool("comercios_top", "Comercios donde más se gastó en un mes (opcionalmente dentro de una categoría).",
         {"type": "object", "properties": {"mes": _MES, "categoria": _CAT,
                                           "limite": {"type": "integer", "description": "Cuántos (máx. 25)."}}},
         comercios_top),
    Tool("gasto_en_comercio", "Cuánto se gasta en un comercio mes a mes. Busca el texto dentro de la clave "
         "del comercio (p. ej. MERCADONA, NETFLIX).",
         {"type": "object", "properties": {"comercio": {"type": "string"},
                                           "meses": {"type": "integer", "description": "Meses hacia atrás (12)."}},
          "required": ["comercio"]}, gasto_en_comercio),
    Tool("recurrentes", "Cargos e ingresos periódicos activos (suscripciones, recibos, nómina), con su coste "
         "al mes, el próximo cargo y si han subido de precio.", {"type": "object", "properties": {}}, recurrentes),
    Tool("presupuestos", "Presupuesto de cada categoría en un mes: límite, gastado, lo que queda y la "
         "proyección a fin de mes.", {"type": "object", "properties": {"mes": _MES}}, presupuestos),
    Tool("alertas", "Alertas pendientes: presupuesto superado, cargos duplicados, importes raros, subidas.",
         {"type": "object", "properties": {}}, alertas),
    Tool("hallazgos", "Diagnóstico automático de un mes: qué se ha disparado, qué se puede recortar y "
         "cuánto supone al mes.", {"type": "object", "properties": {"mes": _MES}}, hallazgos),
    Tool("prevision", "Previsión de cómo acaba este mes, los tres siguientes y cuántos meses se aguanta "
         "sin ingresos.", {"type": "object", "properties": {}}, prevision),
    Tool("resumen_anio", "Un año frente al anterior en el mismo periodo, por categoría y mes a mes.",
         {"type": "object", "properties": {"anio": {"type": "integer"}}}, resumen_anio),
    Tool("resumen_periodo", "Totales de un trimestre, un año o un rango de fechas frente al periodo anterior "
         "equivalente (cortado a la misma altura si está en curso), por categoría, mes a mes y por comercio.",
         {"type": "object", "properties": {
             "periodo": {"type": "string", "description": "AAAA-Q1…AAAA-Q4 (trimestre), AAAA (año) o AAAA-MM. Si se omite todo, el trimestre en curso."},
             "desde": {"type": "string", "description": "Inicio de un rango libre, AAAA-MM-DD (con «hasta»)."},
             "hasta": {"type": "string", "description": "Fin del rango, AAAA-MM-DD."}}}, resumen_periodo),
    Tool("metas", "Metas de ahorro del usuario, cuándo llega a cada una a su ritmo y su mes medio "
         "(gasto fijo y variable por categoría) para simular recortes.",
         {"type": "object", "properties": {}}, metas),
]

BY_NAME = {t.name: t for t in TOOLS}
