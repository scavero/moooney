"""Qué preguntar a continuación: sugerencias para seguir la conversación con el asistente.

Deterministas, sin llamar al modelo: salen de las consultas que acaba de usar la respuesta
(si miró recurrentes, se proponen preguntas sobre recurrentes) y de la categoría que más se
sale de su media este mes. Así no cuestan tiempo ni envían nada fuera.
"""

import sqlite3

from moooney.analytics import periods
from moooney.analytics.metrics import build_report
from moooney.chat.agent import SUGGESTIONS

# `{cat}` se sustituye por la categoría que más se sale de su media (o la mayor del mes).
BY_TOOL: dict[str, list[str]] = {
    "resumen_mes": ["¿Y frente al mes pasado?", "¿Cómo acabaré el mes a este ritmo?",
                    "¿En qué se me ha ido más este mes?"],
    "gasto_por_categoria": ["¿En qué comercios se va lo de {cat}?", "¿Cómo ha ido {cat} en los últimos 12 meses?",
                            "¿Cuánto podría recortar en {cat}?"],
    "comercios_top": ["¿Cuáles de esos son recibos o suscripciones?", "¿En cuál gasto más que hace unos meses?"],
    "gasto_en_comercio": ["¿Cuánto supone eso al año?", "¿Qué más gasto en esa categoría?"],
    "recurrentes": ["¿Cuáles me han subido de precio?", "¿Cuánto suman al año mis recibos fijos?",
                    "¿Cuál podría dar de baja?"],
    "presupuestos": ["¿En qué categorías me voy a pasar a este ritmo?", "¿Qué límite debería ajustar?"],
    "alertas": ["¿Qué debería revisar primero?", "¿Qué alertas se repiten cada mes?"],
    "hallazgos": ["¿Por dónde empiezo a recortar?", "¿Cuánto ahorraría al año si lo hago?"],
    "prevision": ["¿Qué cargos quedan por llegar este mes?", "¿Qué puedo hacer para acabar mejor el mes?"],
    "resumen_anio": ["¿Qué categoría ha subido más este año?", "¿Cuál fue el mes más caro y por qué?"],
    "resumen_periodo": ["¿Qué categoría ha subido más en ese periodo?", "¿Y el periodo anterior?"],
    "metas": ["¿Qué tendría que recortar para llegar antes?", "¿Cuánto debería apartar al mes?"],
}


def _norm(q: str) -> str:
    return " ".join(q.lower().split())


def focus_category(conn: sqlite3.Connection) -> str | None:
    """La categoría que más se sale de su media este mes; si ninguna se sale, la de más gasto."""
    as_of = periods.as_of_date(conn)
    if as_of is None:
        return None
    cats = [c for c in build_report(conn, periods.month_of(as_of)).categories if c.amount > 0]
    if not cats:
        return None
    over = [c for c in cats if (c.delta_avg or 0) > 0]
    best = max(over, key=lambda c: c.delta_avg) if over else max(cats, key=lambda c: c.amount)
    return best.category


def followups(conn: sqlite3.Connection, tools_used: list[str], asked: list[str], *, limit: int = 3) -> list[str]:
    """Hasta `limit` preguntas para seguir, sin repetir lo que ya se ha preguntado."""
    cat = focus_category(conn)
    seen = {_norm(q) for q in asked}
    out: list[str] = []

    def add(template: str) -> None:
        if "{cat}" in template and not cat:
            return
        q = template.format(cat=cat)
        if _norm(q) not in seen and len(out) < limit:
            seen.add(_norm(q))
            out.append(q)

    # la consulta más reciente manda; se va cogiendo una de cada para que haya variedad
    pools = [BY_TOOL[t] for t in dict.fromkeys(reversed(tools_used)) if t in BY_TOOL]
    for i in range(max((len(p) for p in pools), default=0)):
        for pool in pools:
            if i < len(pool):
                add(pool[i])
    for q in SUGGESTIONS:
        add(q)
    return out


def ideas(conn: sqlite3.Connection, categories: list[str]) -> list[str]:
    """Todas las preguntas que el campo de texto ofrece al escribir (autocompletado)."""
    out = list(SUGGESTIONS)
    for pool in BY_TOOL.values():
        out += [q for q in pool if "{cat}" not in q]
    for c in categories:
        out += [f"¿Cuánto gasto al mes en {c}?", f"¿En qué comercios se va lo de {c}?",
                f"¿Cómo ha ido {c} en los últimos 12 meses?"]
    return list(dict.fromkeys(out))
