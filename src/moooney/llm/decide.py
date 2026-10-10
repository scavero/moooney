"""Deciders: cómo se pregunta a los modelos y cómo se lee la respuesta.

Clef-Flash no conversa: se le manda un único mensaje de usuario cuyo content
es un JSON {"state": ..., "questions": ...} y responde en message.content con
JSON {"answers": {...}} (ver docs/clef_test.json). ChatJsonDecider usa Qwen
con response_format JSON Schema y es la reserva si Clef falla.
"""

import json
import re
from typing import Protocol

from pydantic import BaseModel

from moooney.llm.anonymize import assert_safe_state
from moooney.llm.client import LLMClient, LLMError, LLMSettings


class Answer(BaseModel):
    choice: str | None = None
    confidence: float = 0.0
    probabilities: dict[str, float] = {}


class Decider(Protocol):
    def decide(self, state: str, questions: dict) -> dict[str, Answer]: ...


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def parse_clef_response(text: str) -> dict[str, Answer]:
    """Convierte el content de Clef en respuestas. Acepta el envoltorio
    {"answers": {...}] o las respuestas sueltas, y formas abreviadas."""
    cleaned = _FENCE.sub("", text.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError("la respuesta del modelo no es JSON valido") from exc
    if not isinstance(data, dict):
        raise LLMError("la respuesta del modelo no es un objeto JSON")
    answers = data.get("answers") if isinstance(data.get("answers"), dict) else data
    out: dict[str, Answer] = {}
    for qid, value in answers.items():
        if isinstance(value, dict):
            probs = value.get("probabilities")
            out[qid] = Answer(
                choice=value.get("choice"),
                confidence=float(value.get("confidence") or 0.0),
                probabilities=(
                    {str(k): float(v) for k, v in probs.items()} if isinstance(probs, dict) else {}
                ),
            )
        elif isinstance(value, (str, int, float, bool)):
            out[qid] = Answer(choice=str(value), confidence=0.0)
    return out


def _ask_json(client: LLMClient, *, model: str, state: str, questions: dict, purpose: str,
              response_format: dict | None = None) -> dict[str, Answer]:
    assert_safe_state(state)
    content = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
    messages = [{"role": "user", "content": content}]
    raw = client.chat(model=model, messages=messages, purpose=purpose,
                      response_format=response_format)
    return parse_clef_response(raw)


class ClefChatDecider:
    """clef-flash: un solo mensaje de usuario con el estado y las preguntas."""

    def __init__(self, client: LLMClient, model: str) -> None:
        self.client = client
        self.model = model

    def decide(self, state: str, questions: dict) -> dict[str, Answer]:
        return _ask_json(
            self.client, model=self.model, state=state, questions=questions, purpose="categorize"
        )


def _answers_schema(questions: dict) -> dict:
    props: dict = {}
    for qid, spec in questions.items():
        qtype = spec.get("type")
        if qtype == "choice":
            props[qid] = {
                "type": "object",
                "properties": {
                    "choice": {"type": "string", "enum": list(spec.get("criteria", {}))},
                    "confidence": {"type": "number"},
                },
                "required": ["choice", "confidence"],
            }
        else:  # noul (sí/no)
            props[qid] = {
                "type": "object",
                "properties": {
                    "choice": {"type": "boolean"},
                    "confidence": {"type": "number"},
                },
                "required": ["choice", "confidence"],
            }
    return {
        "type": "object",
        "properties": {
            "answers": {
                "type": "object",
                "properties": props,
                "required": list(props),
            }
        },
        "required": ["answers"],
    }


class ChatJsonDecider:
    """Reserva: Qwen con response_format JSON Schema y confianza autoinformada."""

    def __init__(self, client: LLMClient, model: str) -> None:
        self.client = client
        self.model = model

    def decide(self, state: str, questions: dict) -> dict[str, Answer]:
        schema = _answers_schema(questions)
        response_format = {
            "type": "json_schema",
            "json_schema": {"name": "answers", "strict": False, "schema": schema},
        }
        return _ask_json(
            self.client,
            model=self.model,
            state=state,
            questions=questions,
            purpose="categorize",
            response_format=response_format,
        )


def build_decider(settings: LLMSettings, client: LLMClient) -> Decider:
    if settings.decider == "clef":
        return ClefChatDecider(client, settings.models["clef"])
    if settings.decider == "qwen":
        return ChatJsonDecider(client, settings.models["redaccion"])
    raise ValueError(f"decider desconocido en llm.yaml: {settings.decider!r}")
