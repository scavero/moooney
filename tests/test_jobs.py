"""Trabajos en segundo plano: etapas, progreso y avisos a la página para que nadie tenga que recargar."""

import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from moooney.demo import build_demo
from moooney.web.app import create_app
from moooney.web.jobs import Job, JobRegistry, report, stage

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def _esperar(job: Job) -> None:
    for _ in range(200):
        if not job.running:
            return
        time.sleep(0.01)
    raise AssertionError("el trabajo no termina")


def test_etapas_y_progreso():
    seguir = threading.Event()
    visto = {}

    def fn(log):
        log("línea suelta")
        stage(log, "Categorizar")
        report(log, "Preguntando al modelo", 3, 12)
        visto["pct"], visto["stage"] = log.percent, log.stage_state(1)
        seguir.wait(2)

    reg = JobRegistry()
    job = reg.start("Prueba", fn, stages=["Importar", "Categorizar", "Alertas"])
    for _ in range(200):
        if "pct" in visto:
            break
        time.sleep(0.01)
    assert visto == {"pct": 25, "stage": "current"}
    assert job.stage_state(0) == "done" and job.stage_state(2) == "todo"
    assert reg.current() is job and reg.latest() is job
    seguir.set()
    _esperar(job)
    assert job.status == "done" and job.current == "" and job.percent is None
    assert [job.stage_state(i) for i in range(3)] == ["done"] * 3
    assert reg.current() is None and reg.latest() is job
    assert job.seconds_since_finish < 5


def test_error_marca_la_etapa():
    def fn(log):
        stage(log, "Importar")
        raise ValueError("Excel ilegible")

    job = JobRegistry().start("Prueba", fn, stages=["Subir", "Importar", "Categorizar"])
    _esperar(job)
    assert job.status == "error" and "Excel ilegible" in job.error
    assert [job.stage_state(i) for i in range(3)] == ["done", "error", "todo"]


def test_report_y_stage_aceptan_un_logger_simple():
    lineas = []
    report(lineas.append, "nada")  # un lambda o list.append no tiene detail ni stage: no falla
    stage(lineas.append, "nada")
    assert lineas == []


def test_la_pagina_se_entera_sin_recargar(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/pipeline")
        # recarga de cifras al terminar e indicador en la barra lateral
        assert 'hx-trigger="job-finished from:body"' in r.text and 'id="pipeline-live"' in r.text
        assert 'id="job-ind"' in r.text and "every 2s" not in r.text

        r = c.post("/pipeline/jobs/alerts")
        assert r.headers["HX-Trigger"] == "job-started"
        job_id = r.text.split('id="job-', 1)[1].split('"', 1)[0]
        for _ in range(100):
            r = c.get(f"/pipeline/jobs/{job_id}")
            if 'data-job-status="running"' not in r.text:
                break
            time.sleep(0.05)
        assert r.headers.get("HX-Trigger") == "job-finished"
        assert "Listo en" in r.text and "hx-trigger" not in r.text.split("</div>", 1)[0]  # ya no pregunta

        # el indicador: desde otra página avisa con un toast que lleva a Pipeline; en Pipeline no
        r = c.get("/pipeline/jobs/current?was=running", headers={"HX-Current-URL": "http://x/merchants"})
        assert "terminado" in r.headers["X-Toast"] and "&#8594;" in r.headers["X-Toast"] and 'href="/pipeline"' in r.headers["X-Toast"]
        assert "✓" in r.text and "every 2s" not in r.text
        r = c.get("/pipeline/jobs/current?was=running", headers={"HX-Current-URL": "http://x/pipeline"})
        assert "X-Toast" not in r.headers
