"""Bucle del asistente: el modelo de texto pide consultas, moooney las calcula y el modelo responde.

Lo que viaja al modelo: la pregunta del usuario, las respuestas anteriores de la conversación y
los resultados de las consultas (agregados). Todo pasa por la barrera de anonimización y cada
llamada queda en llm_calls con purpose="chat". El historial vive en la tabla chat_messages.
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

from moooney.chat import tools as tools_mod
from moooney.llm.anonymize import AnonymizeError, assert_safe_text
from moooney.llm.client import LLMClient, LLMError, LLMSettings

MAX_ROUNDS = 5  # consultas encadenadas como mucho por pregunta
MAX_QUESTION_CHARS = 600
MAX_TOOL_CHARS = 6000
HISTORY_TURNS = 6  # pares pregunta-respuesta que se reenvían como contexto

SYSTEM_PROMPT = """Eres el asistente de moooney, la aplicación de finanzas personales del usuario.
Respondes en español, claro y breve: de 80 a 200 palabras salvo que te pidan detalle, en Markdown
sencillo (párrafos cortos y viñetas; sin tablas). Tuteas. Empieza por la respuesta, sin título.

Cómo trabajas:
- Para cualquier cifra, llama a las herramientas. Nunca inventes ni estimes un número que no te
  haya devuelto una herramienta; si no lo tienes, dilo.
- No hagas proyecciones ni porcentajes por tu cuenta. Para saber cómo acabará el mes en curso,
  usa la herramienta prevision; no multipliques lo gastado por los días que faltan. Como mucho,
  suma o resta cifras que te hayan devuelto. Si una herramienta dice que no hay comparación, no la
  sustituyas con cálculos tuyos: dilo y ofrece lo que sí hay.
- Puedes llamar a varias herramientas antes de responder. Si una devuelve un error, corrige los
  argumentos (por ejemplo, el nombre exacto de la categoría) y vuelve a intentarlo.
- Los importes van en euros con coma decimal (1.234,56 €). Los meses, por su nombre (octubre de 2026).
- Si el mes en curso no está completo, dilo al comparar con meses enteros.
- Solo ves totales por mes, categoría y comercio: no ves movimientos sueltos, ni a quién se
  hizo un Bizum. Si te piden eso, explícalo y propone mirar la página Movimientos.

Consejos: concretos, basados en los datos, con el ahorro estimado en euros al mes cuando se pueda.
No recomiendes productos financieros, inversiones, bancos ni empresas concretas, ni opines sobre
cuánto aporta a inversión: no eres un asesor financiero autorizado. Si te preguntan algo ajeno a
las finanzas del usuario, dilo en una frase."""

NUDGE = "Llama ahora a la herramienta que necesitas, sin escribir texto."

SUGGESTIONS = [
    "¿Cómo voy este mes?",
    "¿En qué gasto más que de costumbre?",
    "¿Qué recibos o suscripciones me han subido?",
    "¿Dónde podría recortar 100 € al mes?",
    "¿Cuándo llego a mis metas?",
    "¿Cómo va este año frente al anterior?",
]


@dataclass
class Answer:
    content: str
    tools_used: list[str] = field(default_factory=list)
    error: str | None = None


def _model(settings: LLMSettings) -> str:
    return settings.models.get("redaccion") or settings.models.get("qwen") or next(iter(settings.models.values()))


def check_question(question: str) -> str:
    q = " ".join(question.split())
    if not q:
        raise ValueError("escribe una pregunta")
    if len(q) > MAX_QUESTION_CHARS:
        raise ValueError(f"la pregunta es demasiado larga (máximo {MAX_QUESTION_CHARS} caracteres)")
    try:
        assert_safe_text(q, max_chars=MAX_QUESTION_CHARS)
    except AnonymizeError as e:
        raise ValueError(f"no envío eso al modelo: {e}. Quita números de cuenta o tarjeta") from e
    return q


def announces_tool(content: str) -> bool:
    """Respuesta corta que solo anuncia una consulta en vez de hacerla."""
    return len(content) < 200 and any(t.name in content for t in tools_mod.TOOLS)


def run_tool(ctx: tools_mod.Context, name: str, raw_args: str | dict | None) -> str:
    """Ejecuta una consulta y devuelve el JSON para el modelo (o un error que pueda corregir)."""
    tool = tools_mod.BY_NAME.get(name)
    if tool is None:
        return json.dumps({"error": f"no existe la herramienta {name}"}, ensure_ascii=False)
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else (raw_args or {})
        if not isinstance(args, dict):
            raise tools_mod.ToolError("los argumentos deben ser un objeto JSON")
        result = tool.run(ctx, args)
    except (tools_mod.ToolError, ValueError, TypeError) as e:
        result = {"error": str(e)}
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_TOOL_CHARS:
        text = json.dumps({"error": "resultado demasiado largo; acota la consulta (un mes, una categoría)"},
                          ensure_ascii=False)
    try:
        return assert_safe_text(text, max_chars=MAX_TOOL_CHARS)
    except AnonymizeError:
        # el error no repite el contenido, para no filtrarlo
        return json.dumps({"error": "el resultado contenía algo que no puede salir de la máquina"})


def ask(client: LLMClient, settings: LLMSettings, ctx: tools_mod.Context, question: str,
        history: list[dict] | None = None) -> Answer:
    q = check_question(question)
    system = SYSTEM_PROMPT + "\n\nDatos disponibles (JSON):\n" + json.dumps(
        tools_mod.overview(ctx), ensure_ascii=False)
    messages: list[dict] = [{"role": "system", "content": system}]
    for h in (history or [])[-HISTORY_TURNS * 2:]:
        messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": q})
    schemas = [t.schema() for t in tools_mod.TOOLS]
    used: list[str] = []
    model = _model(settings)
    nudged = False
    for round_ in range(MAX_ROUNDS + 1):
        last = round_ == MAX_ROUNDS  # en la última vuelta ya no se ofrecen herramientas: toca responder
        msg = client.complete(model=model, messages=messages, purpose="chat",
                              tools=None if last else schemas, max_tokens=1200,
                              extra=settings.chat_extra or None)
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        if not calls and not last and not nudged and announces_tool(content):
            # a veces el modelo escribe «tengo que llamar a X» en vez de llamarla: se le pide que lo haga
            nudged = True
            messages += [{"role": "assistant", "content": content}, {"role": "user", "content": NUDGE}]
            continue
        if not calls or last:
            if not content:
                raise LLMError("el modelo no ha devuelto respuesta")
            return Answer(content=content, tools_used=used)
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            fn = call.get("function") or {}
            name = fn.get("name", "")
            used.append(name)
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": run_tool(ctx, name, fn.get("arguments"))})
    raise LLMError("el modelo no ha terminado de responder")  # pragma: no cover


# ---------- historial ----------

def history(conn: sqlite3.Connection, limit: int = 40) -> list[sqlite3.Row]:
    rows = conn.execute(
        "SELECT * FROM chat_messages ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return list(reversed(rows))


def history_for_model(conn: sqlite3.Connection) -> list[dict]:
    return [{"role": r["role"], "content": r["content"]} for r in history(conn, HISTORY_TURNS * 2)
            if r["role"] in ("user", "assistant") and not r["error"]]


def save(conn: sqlite3.Connection, role: str, content: str, *, tools_used: list[str] | None = None,
         error: bool = False) -> int:
    with conn:
        cur = conn.execute(
            "INSERT INTO chat_messages (role, content, tools, error, created_at) VALUES (?, ?, ?, ?, ?)",
            (role, content, ",".join(dict.fromkeys(tools_used or [])) or None, int(error),
             datetime.now().isoformat(timespec="seconds")),
        )
    return int(cur.lastrowid)


def clear(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM chat_messages")
