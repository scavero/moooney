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
        # la respuesta trae, fuera de banda, con qué seguir: sale de la consulta usada y no repite lo preguntado
        assert 'id="chat-next"' in r.text and 'hx-swap-oob="true"' in r.text and "¿Y frente al mes pasado?" in r.text
        r = c.get("/chat")
        assert "Nueva conversación" in r.text and "<strong>bien</strong>" in r.text
        assert "Puedes seguir con:" in r.text and 'list="chat-ideas"' in r.text and "¿Cuánto gasto al mes en " in r.text
        # una respuesta sin consultas no pierde el hilo, y una lista pegada al párrafo sale como lista
        monkeypatch.setattr(service, "chat_ask", lambda conn, paths, q: (
            agent.save(conn, "user", q), agent.save(conn, "assistant", "x"),
            agent.Answer(content="Tus recibos:\n- Luz\n- Agua", tools_used=[]))[-1])
        r = c.post("/chat", data={"question": "¿y la luz?"})
        assert "<li>Luz</li>" in r.text and "¿Cómo acabaré el mes a este ritmo?" in r.text
        r = c.post("/chat/clear", follow_redirects=True)
        assert "Nueva conversación" not in r.text


def test_sugerencias_para_seguir(demo):
    from moooney.chat import followups

    _, conn = demo
    cat = followups.focus_category(conn)
    assert cat
    tras = followups.followups(conn, ["resumen_mes", "gasto_por_categoria"], ["¿cómo voy?"])
    assert len(tras) == 3 and any(cat in q for q in tras)  # manda la última consulta, con la categoría que se sale
    assert tras[0] == followups.BY_TOOL["gasto_por_categoria"][0].format(cat=cat)
    # no repite lo ya preguntado, aunque cambien mayúsculas o espacios
    again = followups.followups(conn, ["metas"], ["¿qué tendría que recortar  para llegar antes?"])
    assert "¿Qué tendría que recortar para llegar antes?" not in again and len(again) == 3
    # sin consultas reconocibles, las ideas generales
    assert followups.followups(conn, ["inventada"], []) == agent.SUGGESTIONS[:3]
    pool = followups.ideas(conn, ["Ocio"])
    assert "¿Cuánto gasto al mes en Ocio?" in pool and len(pool) == len(set(pool)) and not any("{cat}" in q for q in pool)


def test_sugerencias_del_modelo_ligero(demo, monkeypatch):
    from moooney.chat import followups

    settings_path, conn = demo
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        content = '```json\n{"sugerencias": ["Sí, compáralo", "¿cómo voy?", "Mi IBAN es ES9121000418450200051332", 7, "¿Y el gym?"]}\n```'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    client = LLMClient(SETTINGS, transport=httpx.MockTransport(handler))
    monkeypatch.setenv("LITELLM_API_KEY", "clave-falsa")
    monkeypatch.delenv("MOOONEY_OFFLINE")
    turns = [{"role": "user", "content": "¿cómo voy?"}, {"role": "assistant", "content": "Bien. ¿Lo comparo con septiembre?"}]
    # fuera lo ya preguntado, lo que no pasaría la barrera y lo que no es texto
    assert followups.model_followups(client, SETTINGS, turns) == ["Sí, compáralo", "¿Y el gym?"]
    assert seen[0]["model"] == followups.suggest_model(SETTINGS) and "Asistente: Bien." in seen[0]["messages"][1]["content"]
    assert followups.model_followups(client, SETTINGS, turns[:1]) == [] and len(seen) == 1  # sin respuesta, no se llama
    roto = LLMClient(SETTINGS, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "no sé"}}]})))
    assert followups.model_followups(roto, SETTINGS, turns) == []
    conn.close()
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    with TestClient(create_app(settings_path)) as c:  # offline: /chat/next cae en las deterministas
        r = c.get("/chat/next")
        assert r.status_code == 200 and "<html" not in r.text
