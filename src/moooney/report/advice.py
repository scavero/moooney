"""Informe mensual con consejos redactados por el modelo de texto (Qwen en ClusterTIC).

El modelo NO ve movimientos: recibe únicamente agregados (resumen del mes,
categorías con variación, hallazgos deterministas, presupuesto, recurrentes,
alertas y previsión) y devuelve un informe en Markdown con acciones concretas
priorizadas por euros al mes. El texto enviado pasa por la misma barrera de
anonimización que todo lo demás y queda registrado en llm_calls.
"""

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime

from moooney.analytics import alerts as alerts_mod
from moooney.analytics import budgets as budgets_mod
from moooney.analytics import forecast as forecast_mod
from moooney.analytics import periods
from moooney.analytics.insights import Insight, build_insights
from moooney.analytics.recurring import load_recurring
from moooney.llm.anonymize import assert_safe_text
from moooney.llm.client import LLMClient, LLMSettings

MAX_PAYLOAD_CHARS = 12000

SYSTEM_PROMPT = """Eres un asesor de finanzas personales prudente y directo. Recibes agregados mensuales
de una sola persona (nunca movimientos individuales). Escribe en español, en Markdown, sin saludos
ni relleno. Estructura obligatoria:

## En una frase
Una frase con el estado del mes.

## Dónde se va el dinero
3-5 viñetas con las cifras más relevantes del mes y cómo comparan con la media.

## Qué haría yo
3-5 acciones concretas, ordenadas por impacto en euros al mes, cada una con el ahorro estimado
y en qué dato se basa. Nada genérico: usa los nombres de categoría y comercio que recibes.

## Vigilar
2-3 riesgos o cosas a revisar (alertas, recurrentes nuevos, previsión).

Reglas: no inventes datos que no estén en el JSON; si una cifra no aparece, no la cites.
No des consejos de inversión ni de productos financieros. Máximo 350 palabras."""


def build_payload(conn: sqlite3.Connection, month: str | None = None, *,
                  budgets_limits: dict[str, float] | None = None) -> dict:
    insights, report = build_insights(conn, month)
    m, as_of = report.month, report.as_of
    first = periods.first_date(conn)
    s = report.summary
    payload: dict = {
        "mes": m,
        "datos_hasta": as_of,
        "mes_completo": s.full_month,
        "resumen": {
            "ingresos": round(s.income), "gastos": round(s.expenses),
            "gastos_fijos": round(s.fixed_expenses), "gastos_variables": round(s.variable_expenses),
            "ahorro_neto": round(s.net),
            "tasa_ahorro": round(s.savings_rate, 2) if s.savings_rate is not None else None,
            "inversion": round(s.investment),
        },
        "serie_meses": [
            {"mes": x.month, "ingresos": round(x.income), "gastos": round(x.expenses), "ahorro": round(x.net),
             "completo": x.full_month}
            for x in report.series[-6:]
        ],
        "categorias": [
            {"categoria": c.category, "importe": round(c.amount), "pct": round(c.pct),
             "media_meses_anteriores": round(c.avg_prev) if c.avg_prev is not None else None,
             "vs_media": round(c.delta_avg) if c.delta_avg is not None else None,
             "parte_fija": round(c.fixed_amount)}
            for c in report.categories[:12]
        ],
        "hallazgos": [
            {"tipo": i.kind, "nivel": i.severity, "titulo": i.title, "detalle": i.detail,
             "impacto_eur_mes": round(i.impact)}
            for i in insights[:10]
        ],
        "recurrentes_activos": [
            {"clave": r["key"], "categoria": r["category"], "periodo": r["period_label"],
             "importe": round(float(r["amount_avg"]), 2)}
            for r in load_recurring(conn, active_only=True)[:15]
        ],
        "alertas_pendientes": [
            {"nivel": a["severity"], "tipo": a["type"], "mensaje": a["message"]}
            for a in alerts_mod.pending_alerts(conn)[:10]
        ],
    }
    if budgets_limits:
        limits = budgets_mod.limits_for_month(conn, m, budgets_limits)
        rows = budgets_mod.budget_status(conn, m, limits, as_of=as_of)
        payload["presupuesto"] = [
            {"categoria": r.category, "limite": round(r.limit), "gastado": round(r.spent), "pct": round(r.pct)}
            for r in rows[:12]
        ]
    if periods.month_of(as_of) == m:
        f = forecast_mod.forecast_month_end(conn, as_of, first=first)
        payload["prevision_fin_de_mes"] = {
            "dia": f.day, "dias_mes": f.days_in_month, "gastos": round(f.expenses),
            "ingresos": round(f.income), "ahorro": round(f.net),
        }
    nxt = forecast_mod.forecast_next_months(conn, as_of, n=3, first=first)
    payload["proximos_meses"] = [
        {"mes": x.month, "gastos": round(x.expenses), "ingresos": round(x.income), "ahorro": round(x.net)} for x in nxt
    ]
    runway = forecast_mod.runway_months(conn, as_of, first=first)
    payload["colchon_meses"] = round(runway, 1) if runway is not None else None
    return payload


def payload_text(payload: dict) -> str:
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_PAYLOAD_CHARS:
        text = text[:MAX_PAYLOAD_CHARS]
    return assert_safe_text(text, max_chars=MAX_PAYLOAD_CHARS)


def generate_report(client: LLMClient, settings: LLMSettings, payload: dict) -> str:
    """Llama al modelo de redacción y devuelve el Markdown del informe."""
    model = settings.models.get("redaccion") or settings.models.get("qwen") or next(iter(settings.models.values()))
    content = client.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "Datos del mes en JSON:\n" + payload_text(payload)},
        ],
        purpose="report",
        max_tokens=2500,
        extra=settings.chat_extra or None,
    )
    return content.strip()


def fallback_report(payload: dict, insights: list[Insight] | None = None) -> str:
    """Informe sin modelo: los hallazgos deterministas en Markdown. Sirve en modo offline."""
    r = payload["resumen"]
    lines = [
        "## En una frase",
        f"En {payload['mes']} ingresaste {r['ingresos']} € y gastaste {r['gastos']} €: ahorro neto de {r['ahorro_neto']} €.",
        "",
        "## Dónde se va el dinero",
    ]
    for c in payload["categorias"][:5]:
        vs = f" ({c['vs_media']:+} € frente a la media)" if c.get("vs_media") is not None else ""
        lines.append(f"- {c['categoria']}: {c['importe']} € ({c['pct']} %){vs}")
    lines += ["", "## Qué haría yo"]
    acciones = [h for h in payload["hallazgos"] if h["impacto_eur_mes"] > 0][:5]
    for h in acciones or payload["hallazgos"][:3]:
        lines.append(f"- {h['titulo']}. {h['detalle']}")
    lines += ["", "## Vigilar"]
    for a in payload["alertas_pendientes"][:3]:
        lines.append(f"- [{a['nivel']}] {a['mensaje']}")
    if not payload["alertas_pendientes"]:
        lines.append("- Sin alertas pendientes.")
    return "\n".join(lines)


def store_report(conn: sqlite3.Connection, *, month: str, kind: str, model: str, content: str) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO reports (month, kind, model, content, created_at) VALUES (?, ?, ?, ?, ?)",
            (month, kind, model, content, datetime.now().isoformat(timespec="seconds")),
        )
    return int(cur.lastrowid)


def latest_report(conn: sqlite3.Connection, month: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM reports WHERE month = ? ORDER BY created_at DESC, id DESC LIMIT 1", (month,)
    ).fetchone()


def insight_dicts(items: list[Insight]) -> list[dict]:
    return [asdict(i) for i in items]
