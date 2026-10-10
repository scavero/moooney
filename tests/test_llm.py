"""Capa LLM: cliente con reintentos y registro, offline, anonymize y deciders."""

import json
from collections import deque

import httpx
import pytest

from moooney.db import repo
from moooney.llm.anonymize import AnonymizeError, assert_safe_state
from moooney.llm.client import CallRecorder, LLMClient, LLMError, LLMSettings, offline_enabled
from moooney.llm.decide import (
    ChatJsonDecider,
    ClefChatDecider,
    build_decider,
    parse_clef_response,
)

CLEF_CONTENT = json.dumps(
    {
        "answers": {
            "category": {
                "choice": "Alimentacion",
                "confidence": 0.47,
                "probabilities": {"Alimentacion": 0.47, "Otros": 0.3},
            },
            "flow_type": {"choice": "expense", "confidence": 0.93},
            "is_recurring": {"confidence": 0.4},
        }
    }
)

STATE = "Concepto: MERCADONA FALSA. Importe medio: -40.0 EUR (12 apariciones, 4 dias)."


def _settings() -> LLMSettings:
    return LLMSettings(
        base_url="https://test.local/v1",
        models={"clef": "clef-flash", "redaccion": "qwen-test", "embeddings": "bge-test"},
        backoff_s=0.001,
    )


def _client(responses, seen, recorder=None) -> LLMClient:
    queue = deque(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content.decode("utf-8")))
        item = queue.popleft()
        if isinstance(item, int):
            return httpx.Response(item)
        return httpx.Response(200, json=item)

    return LLMClient(
        _settings(), recorder=recorder, transport=httpx.MockTransport(handler), sleeper=lambda s: None
    )


def _ok(content: str = CLEF_CONTENT) -> dict:
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def test_chat_devuelve_content_y_registra_llamada(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    conn = repo.connect(tmp_path / "moo.db")
    recorder = CallRecorder(conn)
    seen: list = []
    client = _client([_ok()], seen, recorder)
    content = client.chat(model="clef-flash", messages=[{"role": "user", "content": "x"}],
                          purpose="categorize")
    assert content == CLEF_CONTENT
    recorder.flush()
    row = conn.execute("SELECT model, purpose, input_tokens, output_tokens, payload_hash"
                       " FROM llm_calls").fetchone()
    assert (row["model"], row["purpose"]) == ("clef-flash", "categorize")
    assert (row["input_tokens"], row["output_tokens"]) == (11, 7)
    assert len(row["payload_hash"]) == 64


def test_offline_bloquea_la_red(monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    assert offline_enabled()
    seen: list = []
    client = _client([_ok()], seen)
    with pytest.raises(LLMError, match="offline"):
        client.chat(model="m", messages=[], purpose="p")
    assert seen == []


def test_sin_clave_de_api_error_claro(monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    seen: list = []
    client = _client([_ok()], seen)
    with pytest.raises(LLMError, match="LITELLM_API_KEY"):
        client.chat(model="m", messages=[], purpose="p")


def test_reintenta_500_y_graba_una_sola_vez(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    conn = repo.connect(tmp_path / "moo.db")
    seen: list = []
    recorder = CallRecorder(conn)
    client = _client([500, 502, _ok()], seen, recorder)
    client.chat(model="m", messages=[{"role": "user", "content": "h"}], purpose="p")
    assert len(seen) == 3
    recorder.flush()
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


def test_401_no_reintenta_y_queda_registrado(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    conn = repo.connect(tmp_path / "moo.db")
    seen: list = []
    recorder = CallRecorder(conn)
    client = _client([401], seen, recorder)
    with pytest.raises(LLMError, match="HTTP 401"):
        client.chat(model="m", messages=[{"role": "user", "content": "h"}], purpose="p")
    assert len(seen) == 1
    recorder.flush()
    assert conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0] == 1


def test_parse_respuesta_clef_con_envoltorio():
    answers = parse_clef_response("```json\n" + CLEF_CONTENT + "\n```")
    assert answers["category"].choice == "Alimentacion"
    assert answers["category"].probabilities["Otros"] == 0.3
    assert answers["flow_type"].confidence == 0.93
    assert answers["is_recurring"].choice is None  # noul: solo confianza
    assert answers["is_recurring"].confidence == 0.4


def test_parse_respuesta_sin_envoltorio_y_abreviada():
    answers = parse_clef_response('{"category": "Otros", "flow_type": {"choice": "expense"}}')
    assert answers["category"].choice == "Otros"
    assert answers["flow_type"].confidence == 0.0


def test_parse_respuesta_no_json_error():
    with pytest.raises(LLMError, match="JSON"):
        parse_clef_response("esto no es json")


def test_clef_decider_un_solo_mensaje_con_json():
    seen: list = []
    client = _client([_ok()], seen)
    decider = ClefChatDecider(client, "clef-flash")
    answers = decider.decide(STATE, {"category": {"type": "choice", "criteria": {"Otros": "x"}}})
    assert answers["category"].choice == "Alimentacion"
    body = seen[0]
    assert len(body["messages"]) == 1
    assert body["messages"][0]["role"] == "user"
    inner = json.loads(body["messages"][0]["content"])
    assert inner["state"] == STATE
    assert "questions" in inner
    assert "response_format" not in body


def test_qwen_decider_envia_json_schema():
    seen: list = []
    client = _client([_ok()], seen)
    decider = ChatJsonDecider(client, "qwen-test")
    decider.decide(STATE, {"category": {"type": "choice", "criteria": {"Otros": "x"}}})
    rf = seen[0]["response_format"]
    assert rf["type"] == "json_schema"
    enum = rf["json_schema"]["schema"]["properties"]["answers"]["properties"]["category"][
        "properties"
    ]["choice"]["enum"]
    assert enum == ["Otros"]


def test_anonymize_rechaza_antes_de_tocar_red(monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-test")
    seen: list = []
    decider = ClefChatDecider(_client([_ok()], seen), "clef-flash")
    malo = "Concepto: TRANSFERENCIA. IBAN ES66 0081 5700 23 4567890123."
    with pytest.raises(AnonymizeError):
        decider.decide(malo, {})
    assert seen == []


def test_anonymize_checkeos_unitarios():
    assert_safe_state(STATE)
    for malo in (
        "referencia 12345678901234 en juego",
        "tarjeta 5163 8303 0390 0205 fin",
        "saldo real 1234.5678 euros",
        "cuenta ES6600815700234567890123",
    ):
        with pytest.raises(AnonymizeError):
            assert_safe_state(malo)


def test_build_decider_segun_config():
    client = _client([], [])
    assert isinstance(build_decider(_settings(), client), ClefChatDecider)
    s = _settings()
    s.decider = "qwen"
    assert isinstance(build_decider(s, client), ChatJsonDecider)
    s.decider = "otro"
    with pytest.raises(ValueError, match="decider desconocido"):
        build_decider(s, client)
