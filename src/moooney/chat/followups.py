"""Qué preguntar a continuación: sugerencias para seguir la conversación con el asistente.

Deterministas, sin llamar al modelo: salen de las consultas que acaba de usar la respuesta
(si miró recurrentes, se proponen preguntas sobre recurrentes) y de la categoría que más se
sale de su media este mes. Así no cuestan tiempo ni envían nada fuera.
"""

import json
import re
import sqlite3

from moooney.analytics import periods
from moooney.analytics.metrics import build_report
from moooney.chat.agent import SUGGESTIONS, check_question
from moooney.llm.anonymize import AnonymizeError, assert_safe_text
from moooney.llm.client import LLMClient, LLMError, LLMSettings

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


# ---------- sugerencias inferidas por el modelo ligero ----------

MODEL_SYSTEM = (
    "Lees una conversación entre un usuario y su asistente de finanzas personales. Propón {n} mensajes breves "
    "(menos de 70 caracteres cada uno) que el usuario podría enviar ahora para continuarla: responder a lo que el "
    "asistente acaba de preguntar u ofrecer, o profundizar en lo último que se ha hablado. Escríbelos en español, "
    "con la voz del usuario, distintos entre sí y sin repetir lo que ya ha preguntado. "
    'Devuelve solo JSON: {{"sugerencias": ["...", "..."]}}'
)


def suggest_model(settings: LLMSettings) -> str:
    return settings.models.get("sugerencias") or "gemma-4-e2b"


def _transcript(turns: list[dict]) -> str:
    return "\n".join(("Usuario: " if m["role"] == "user" else "Asistente: ") + " ".join(m["content"].split())[:700]
                     for m in turns[-6:])


def _ask_list(client: LLMClient, settings: LLMSettings, system: str, user: str, purpose: str) -> list:
    """Pide al modelo ligero una lista JSON de sugerencias. [] si falla o no devuelve una lista."""
    try:
        raw = client.chat(
            model=suggest_model(settings),
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": assert_safe_text(user, max_chars=6000)}],
            purpose=purpose, max_tokens=200,
        )
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip()))
        items = data.get("sugerencias") if isinstance(data, dict) else data
    except (LLMError, AnonymizeError, ValueError):
        return []
    return items if isinstance(items, list) else []


COMPLETE_SYSTEM = (
    "El usuario está escribiendo un mensaje para su asistente de finanzas personales. Complétalo: propón {n} "
    "mensajes completos y breves (menos de 80 caracteres) que empiecen exactamente por lo que ya ha escrito y "
    "encajen con la conversación. En español, con la voz del usuario y distintos entre sí. "
    'Devuelve solo JSON: {{"sugerencias": ["...", "..."]}}'
)
MIN_TYPED = 4


def model_completions(client: LLMClient, settings: LLMSettings, turns: list[dict], typed: str, *,
                      limit: int = 4) -> list[str]:
    """Formas de terminar lo que el usuario está escribiendo. Solo las que empiezan por lo tecleado,
    que son las únicas que el desplegable del navegador enseña."""
    typed = " ".join(typed.split())
    if len(typed) < MIN_TYPED:
        return []
    try:
        check_question(typed)
    except ValueError:
        return []
    user = (_transcript(turns) + "\n\n" if turns else "") + "El usuario ha escrito hasta ahora: " + typed
    out: list[str] = []
    for s in _ask_list(client, settings, COMPLETE_SYSTEM.format(n=limit), user, "chat_complete"):
        try:
            q = check_question(s) if isinstance(s, str) else ""
        except ValueError:
            continue
        if _norm(q).startswith(_norm(typed)) and _norm(q) != _norm(typed) and len(q) <= 120 and q not in out:
            out.append(q)
    return out[:limit]


def model_followups(client: LLMClient, settings: LLMSettings, turns: list[dict], *, limit: int = 3) -> list[str]:
    """Siguientes mensajes posibles, inferidos por el modelo ligero a partir de la conversación.

    `turns` son los mensajes ya guardados ({"role", "content"}). Devuelve [] si el modelo falla o no
    responde algo útil: quien llama se queda entonces con las sugerencias deterministas.
    """
    if not turns or turns[-1]["role"] != "assistant":
        return []
    items = _ask_list(client, settings, MODEL_SYSTEM.format(n=limit), _transcript(turns), "chat_followups")
    seen = {_norm(m["content"]) for m in turns if m["role"] == "user"}
    out: list[str] = []
    for s in items:
        try:  # lo que se propone se podrá enviar tal cual: pasa la misma barrera que una pregunta
            q = check_question(s) if isinstance(s, str) else ""
        except ValueError:
            continue
        if q and len(q) <= 120 and _norm(q) not in seen and len(out) < limit:
            seen.add(_norm(q))
            out.append(q)
    return out
