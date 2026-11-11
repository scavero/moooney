"""Fase 5: informe con consejos (modelo falso), vigilancia de la carpeta y web del informe."""

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.config import load_settings
from moooney.db import repo
from moooney.demo import build_demo
from moooney.ingest.watch import pending_files, watch
from moooney.llm.anonymize import AnonymizeError, assert_safe_text
from moooney.llm.client import LLMClient, LLMSettings
from moooney.report import advice
from moooney.web.app import create_app
from tests.conftest import make_santander_xlsx

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture()
def demo(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=8)
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    store_recurring(conn, detect_recurring(conn, "2026-10-10"), "t")
    yield settings_path, conn
    conn.close()


def test_payload_solo_agregados_y_pasa_anonymize(demo):
    _, conn = demo
    payload = advice.build_payload(conn, budgets_limits={"Alimentacion": 400.0})
    assert payload["mes"] == "2026-10" and payload["resumen"]["ingresos"] > 0
    assert "presupuesto" in payload and "prevision_fin_de_mes" in payload
    assert len(payload["proximos_meses"]) == 3
    text = advice.payload_text(payload)
    assert "fingerprint" not in text and "balance" not in text  # nada de filas crudas
    assert json.loads(text)["mes"] == "2026-10"
    with pytest.raises(AnonymizeError):
        assert_safe_text("ES66 0081 5700 23 4567890123", max_chars=100)
    with pytest.raises(AnonymizeError):
        assert_safe_text("x" * 101, max_chars=100)


def test_informe_con_modelo_falso_y_sin_modelo(demo, monkeypatch):
    _, conn = demo
    monkeypatch.delenv("MOOONEY_OFFLINE")
    monkeypatch.setenv("LITELLM_API_KEY", "clave-falsa")
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen["model"] = body["model"]
        seen["user"] = body["messages"][1]["content"]
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "## En una frase\nMes inventado.\n\n## Qué haría yo\n- Nada"}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 40},
        })

    settings = LLMSettings(base_url="https://test.local/v1", models={"redaccion": "qwen-test", "clef": "c", "embeddings": "e"})
    client = LLMClient(settings, transport=httpx.MockTransport(handler))
    payload = advice.build_payload(conn)
    md = advice.generate_report(client, settings, payload)
    assert md.startswith("## En una frase") and seen["model"] == "qwen-test"
    assert "Datos del mes en JSON" in seen["user"] and '"mes":"2026-10"' in seen["user"]
    fallback = advice.fallback_report(payload)
    assert "## Qué haría yo" in fallback and "## Vigilar" in fallback
    rid = advice.store_report(conn, month="2026-10", kind="modelo", model="qwen-test", content=md)
    assert rid > 0 and advice.latest_report(conn, "2026-10")["content"] == md


def test_web_informe_sin_modelo_en_offline(demo):
    settings_path, conn = demo
    conn.close()
    app = create_app(settings_path)
    with TestClient(app) as c:
        r = c.get("/report")
        assert r.status_code == 200 and "Todavía no hay informe" in r.text
        r = c.post("/report/generate-offline?month=2026-10", follow_redirects=True)
        assert r.status_code == 200
        assert "En una frase" in r.text and "reglas" in r.text
        r = c.get("/report?month=2026-09")
        assert r.status_code == 200 and "el JSON de agregados" in r.text


def test_watch_detecta_excel_nuevo_y_corre_la_pipeline(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    settings = load_settings(settings_path)
    assert pending_files(settings) == []
    lines: list[str] = []
    assert watch(settings, settings_path.parent, lines.append, once=True) == 0
    # la primera vuelta guarda la copia del día (en la carpeta de la demo) y no hay nada nuevo
    assert lines[0].startswith("copia de seguridad: moooney-") and lines[1:] == ["nada nuevo"]
    assert len(list((tmp_path / "demo" / "backups").glob("moooney-*.db"))) == 1
    lines.clear()
    watch(settings, settings_path.parent, lines.append, once=True)
    assert lines == ["nada nuevo"]  # la segunda, el mismo día, ya no copia

    make_santander_xlsx(settings.paths.excel_dir / "nuevo.xlsx")
    assert [p.name for p in pending_files(settings)] == ["nuevo.xlsx"]
    lines.clear()
    runs = watch(settings, settings_path.parent, lines.append, once=True)
    assert runs == 1
    texto = "\n".join(lines)
    assert "nuevo.xlsx: 3 filas, 3 nuevas" in texto and "pipeline completada" in texto
    assert pending_files(settings) == []  # ya importado
    # bucle con sleeper falso: dos vueltas sin novedades y sale
    esperas: list[float] = []
    assert watch(settings, settings_path.parent, lines.append, interval=5, sleeper=esperas.append,
                 max_iterations=2) == 0
    assert esperas == [5]  # dos comprobaciones, una espera entre ellas


def _excel_ilegible(path: Path) -> Path:
    """Excel sintético con una cabecera que no casa con ningún perfil de banco."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Columna A", "Columna B", "Columna C"])
    ws.append(["x", "y", "z"])
    wb.save(path)
    return path


def test_watch_aparta_un_excel_ilegible_sin_caerse_ni_repetir(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    settings = load_settings(settings_path)
    _excel_ilegible(settings.paths.excel_dir / "raro.xlsx")
    make_santander_xlsx(settings.paths.excel_dir / "bueno.xlsx")
    lines: list[str] = []
    esperas: list[float] = []
    runs = watch(settings, settings_path.parent, lines.append, interval=5, sleeper=esperas.append,
                 max_iterations=3)
    texto = "\n".join(lines)
    assert runs == 1  # el ilegible no relanza la pipeline en cada vuelta
    assert "bueno.xlsx: 3 filas, 3 nuevas" in texto
    assert "raro.xlsx: no se ha podido leer" in texto
    assert texto.count("raro.xlsx: no se ha podido importar; se ignora") == 1
    assert [p.name for p in pending_files(settings)] == ["raro.xlsx"]
    assert esperas == [5, 5]


def test_watch_no_se_cae_si_la_pipeline_falla(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    settings = load_settings(settings_path)
    make_santander_xlsx(settings.paths.excel_dir / "nuevo.xlsx")

    def revienta(*args, **kwargs):
        raise RuntimeError("base de datos bloqueada")

    monkeypatch.setattr("moooney.ingest.watch.run_pipeline_once", revienta)
    lines: list[str] = []
    assert watch(settings, settings_path.parent, lines.append, interval=5, sleeper=lambda s: None,
                 max_iterations=2) == 0
    assert sum("error en la vigilancia (RuntimeError: base de datos bloqueada)" in x for x in lines) == 2
