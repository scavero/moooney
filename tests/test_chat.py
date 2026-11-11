"""Asistente de chat: consultas deterministas, bucle con un modelo falso y página web. Datos de la demo."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.chat import agent, tools
from moooney.db import repo
from moooney.demo import build_demo
from moooney.llm.client import CallRecorder, LLMClient, LLMSettings
from moooney.web.app import create_app

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"
SETTINGS = LLMSettings(base_url="https://test.local/v1", models={"redaccion": "qwen-test", "clef": "c"})


@pytest.fixture()
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=8)
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    store_recurring(conn, detect_recurring(conn, "2026-10-10"), "t")
    yield settings_path, conn
    conn.close()


def test_todas_las_consultas_responden_y_pasan_la_barrera(demo):
    _, conn = demo
    ctx = tools.Context(conn)
    for t in tools.TOOLS:
        out = agent.run_tool(ctx, t.name, "{}" if t.name != "gasto_en_comercio" else '{"comercio": "MERCADONA"}')
        data = json.loads(out)
        assert "error" not in data, (t.name, data)
        assert "balance" not in out and "fingerprint" not in out and "iban" not in out.lower()
    assert set(tools.overview(ctx)) == {"hoy", "datos_desde", "datos_hasta", "categorias"}


def test_errores_que_el_modelo_puede_corregir(demo):
    _, conn = demo
    ctx = tools.Context(conn)
    assert "Existen" in json.loads(agent.run_tool(ctx, "gasto_por_categoria", '{"categoria": "Inventada"}'))["error"]
    assert "AAAA-MM" in json.loads(agent.run_tool(ctx, "resumen_mes", '{"mes": "octubre"}'))["error"]
    assert "no existe" in json.loads(agent.run_tool(ctx, "borrar_todo", "{}"))["error"]
    assert "error" in json.loads(agent.run_tool(ctx, "resumen_mes", "no es json"))
    cat = tools.overview(ctx)["categorias"][0]
    out = json.loads(agent.run_tool(ctx, "gasto_por_categoria", json.dumps({"categoria": cat.upper()})))
    assert len(out["ultimos_12_meses"]) == 12


def test_pregunta_no_admite_datos_de_cuenta():
    with pytest.raises(ValueError):
        agent.check_question("   ")
    with pytest.raises(ValueError, match="no envío eso"):
        agent.check_question("¿qué pasa con ES66 0081 5700 23 4567890123?")
    assert agent.check_question("  ¿cómo   voy? ") == "¿cómo voy?"


def _fake_model(seen: list):
    """Primera vuelta: pide una consulta. Segunda: responde con la cifra que le devolvieron."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        tool_msgs = [m for m in body["messages"] if m["role"] == "tool"]
        if not tool_msgs:
            msg = {"content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "resumen_mes", "arguments": '{"mes": "2026-09"}'}}]}
        else:
            gastos = json.loads(tool_msgs[-1]["content"])["gastos"]
            msg = {"content": f"En septiembre gastaste **{gastos} €**."}
        return httpx.Response(200, json={"choices": [{"message": msg}], "usage": {"prompt_tokens": 9}})

    return handler


def test_bucle_con_modelo_falso(demo, monkeypatch):
    _, conn = demo
    seen: list = []
    rec = CallRecorder(conn)
    client = LLMClient(SETTINGS, recorder=rec, transport=httpx.MockTransport(_fake_model(seen)))
    monkeypatch.delenv("MOOONEY_OFFLINE")
    monkeypatch.setenv("LITELLM_API_KEY", "clave-falsa")
    ans = agent.ask(client, SETTINGS, tools.Context(conn), "¿Cuánto gasté en septiembre?",
                    [{"role": "user", "content": "hola"}, {"role": "assistant", "content": "hola"}])
    rec.flush()
    assert ans.tools_used == ["resumen_mes"] and "En septiembre gastaste" in ans.content
    assert len(seen) == 2 and seen[0]["tools"] and seen[0]["model"] == "qwen-test"
    assert seen[0]["messages"][0]["role"] == "system" and "categorias" in seen[0]["messages"][0]["content"]
    assert [m["role"] for m in seen[0]["messages"][1:]] == ["user", "assistant", "user"]
    n = conn.execute("SELECT COUNT(*) FROM llm_calls WHERE purpose = 'chat'").fetchone()[0]
    assert n == 2


def test_si_anuncia_la_consulta_se_le_pide_que_la_haga(demo, monkeypatch):
    _, conn = demo
    monkeypatch.delenv("MOOONEY_OFFLINE")
    monkeypatch.setenv("LITELLM_API_KEY", "clave-falsa")
    replies = iter([
        {"content": "I need to call the recurrentes tool."},
        {"content": None, "tool_calls": [{"id": "c1", "type": "function",
                                          "function": {"name": "recurrentes", "arguments": "{}"}}]},
        {"content": "Te ha subido el gimnasio."},
    ])
    seen: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": next(replies)}]})

    client = LLMClient(SETTINGS, transport=httpx.MockTransport(handler))
    ans = agent.ask(client, SETTINGS, tools.Context(conn), "¿Qué me ha subido?")
    assert ans.content == "Te ha subido el gimnasio." and ans.tools_used == ["recurrentes"]
    assert seen[1]["messages"][-1] == {"role": "user", "content": agent.NUDGE}


def test_historial_y_borrado(demo):
    _, conn = demo
    agent.save(conn, "user", "¿cómo voy?")
    agent.save(conn, "assistant", "Bien.", tools_used=["resumen_mes", "resumen_mes"])
    agent.save(conn, "assistant", "fallo", error=True)
    rows = agent.history(conn)
    assert [r["role"] for r in rows] == ["user", "assistant", "assistant"] and rows[1]["tools"] == "resumen_mes"
    assert agent.history_for_model(conn) == [{"role": "user", "content": "¿cómo voy?"},
                                             {"role": "assistant", "content": "Bien."}]
    agent.clear(conn)
    assert agent.history(conn) == []


def test_pagina(demo, monkeypatch):
    settings_path, conn = demo
    conn.close()
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/chat")
        assert r.status_code == 200 and "modo offline" in r.text and "¿Cómo voy este mes?" in r.text
        r = c.post("/chat", data={"question": "¿cómo voy?"})
        assert r.status_code == 200 and "modo offline" in r.text and 'role="alert"' in r.text

    from moooney.web import service

    def fake_ask(conn, paths, question):
        agent.save(conn, "user", question)
        agent.save(conn, "assistant", "Vas **bien** <script>x</script>", tools_used=["resumen_mes"])
        return agent.Answer(content="Vas **bien** <script>x</script>", tools_used=["resumen_mes"])

    monkeypatch.setattr(service, "chat_ask", fake_ask)
    with TestClient(create_app(settings_path)) as c:
        r = c.post("/chat", data={"question": "¿cómo voy?"})
        assert "<strong>bien</strong>" in r.text and "<script>" not in r.text and "resumen del mes" in r.text
        r = c.get("/chat")
        assert "Nueva conversación" in r.text and "<strong>bien</strong>" in r.text
        r = c.post("/chat/clear", follow_redirects=True)
        assert "Nueva conversación" not in r.text
