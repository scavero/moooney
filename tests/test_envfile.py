"""Ficheros .env: parseo, precedencia y guardado desde la web. Todo sintético."""

import os

import httpx

from moooney import envfile
from moooney.llm.client import LLMClient, LLMError, LLMSettings


def _llm_settings() -> LLMSettings:
    return LLMSettings(base_url="https://proxy.invalid/v1", models={"clef": "clef-flash"}, max_retries=0)


def test_parse_env_ignora_comentarios_y_quita_comillas():
    texto = "# comentario\n\nexport A=1\nB = \"con comillas\"\nC='simple'\nsin_igual\nD=\n"
    assert envfile.parse_env(texto) == {"A": "1", "B": "con comillas", "C": "simple", "D": ""}


def test_precedencia_entorno_data_raiz(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / ".env").write_text("LITELLM_API_KEY=raiz\nGOOGLE_PLACES_API_KEY=raiz-g\nOTRA=no\n", encoding="utf-8")
    (data / ".env").write_text("LITELLM_API_KEY=datos\n", encoding="utf-8")
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    monkeypatch.delenv("OTRA", raising=False)
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")

    fuentes = envfile.load_env_files(data)

    assert os.environ["LITELLM_API_KEY"] == "datos"  # data/.env manda sobre ./.env
    assert os.environ["GOOGLE_PLACES_API_KEY"] == "raiz-g"
    assert "OTRA" not in os.environ  # solo la lista blanca
    assert fuentes["LITELLM_API_KEY"] == str(data / ".env")
    assert fuentes["GOOGLE_PLACES_API_KEY"] == str(tmp_path / ".env")
    assert fuentes["MOOONEY_OFFLINE"] == envfile.SOURCE_ENV


def test_entorno_manda_sobre_ficheros(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    (data / ".env").write_text("LITELLM_API_KEY=datos\n", encoding="utf-8")
    monkeypatch.setenv("LITELLM_API_KEY", "entorno")
    fuentes = envfile.load_env_files(data)
    assert os.environ["LITELLM_API_KEY"] == "entorno"
    assert fuentes["LITELLM_API_KEY"] == envfile.SOURCE_ENV


def test_guardar_sustituir_y_quitar(tmp_path):
    p = tmp_path / "data" / ".env"
    envfile.save_env_value(p, "LITELLM_API_KEY", "sk-uno")
    envfile.save_env_value(p, "GOOGLE_PLACES_API_KEY", "g-uno")
    envfile.save_env_value(p, "LITELLM_API_KEY", "sk-dos")
    texto = p.read_text(encoding="utf-8")
    assert texto.startswith("#") and texto.count("LITELLM_API_KEY=") == 1
    assert envfile.parse_env(texto) == {"LITELLM_API_KEY": "sk-dos", "GOOGLE_PLACES_API_KEY": "g-uno"}
    assert envfile.remove_env_value(p, "LITELLM_API_KEY") is True
    assert envfile.remove_env_value(p, "LITELLM_API_KEY") is False
    assert envfile.parse_env(p.read_text(encoding="utf-8")) == {"GOOGLE_PLACES_API_KEY": "g-uno"}


def test_pista_no_revela_la_clave():
    assert envfile.hint("sk-1234567890abcd") == "…abcd"
    assert envfile.hint("corta") == "configurada"
    assert envfile.hint(None) == ""


def test_list_models_con_transporte_simulado(monkeypatch):
    monkeypatch.delenv("MOOONEY_OFFLINE", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-prueba")
    vistos: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request.headers.get("authorization", ""))
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "clef-flash"}, {"id": "bge-m3"}]})
        return httpx.Response(404)

    client = LLMClient(_llm_settings(), transport=httpx.MockTransport(handler))
    assert client.list_models() == ["clef-flash", "bge-m3"]
    assert vistos == ["Bearer sk-prueba"]

    rechaza = LLMClient(_llm_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    try:
        rechaza.list_models()
    except LLMError as e:
        assert "401" in str(e)
    else:
        raise AssertionError("una clave rechazada debe fallar")
