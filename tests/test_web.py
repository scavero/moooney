"""Fase 4: la web sobre la base de demostración (sintética). Sin red: offline forzado."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moooney.demo import build_demo, generate_transactions
from moooney.web.app import create_app, eur

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=8)
    app = create_app(settings_path)
    with TestClient(app) as c:
        yield c


def test_demo_es_determinista_y_coherente():
    a = generate_transactions(months=3)
    b = generate_transactions(months=3)
    assert a == b
    assert all(r["balance"] is not None for r in a)
    assert any(r["flow_type"] == "income" for r in a)


def test_formato_euro():
    assert eur(1234.6) == "1.235 €"
    assert eur(-42.1, 2) == "-42,10 €"
    assert eur(None) == "—"


def test_paginas_principales_responden(client):
    for path in ("/", "/transactions", "/merchants", "/merchants?mode=all", "/recurring",
                 "/recurring?all=1", "/budgets", "/alerts", "/alerts?all=1", "/pipeline"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "moooney" in r.text
    r = client.get("/?month=2026-09")
    assert r.status_code == 200 and "2026-09" in r.text
    r = client.get("/transactions?month=2026-09&category=Alimentacion&q=MERCADONA")
    assert r.status_code == 200 and "MERCADONA" in r.text


def test_correccion_de_categoria_aplica_a_movimientos(client):
    r = client.post("/merchants/TIENDA RARA XYZ/category", data={"category": "Compras", "flow_type": ""})
    assert r.status_code == 200
    assert "src-user" in r.text and "guardado" in r.text
    r = client.get("/transactions?key=TIENDA%20RARA%20XYZ")
    assert "Compras" in r.text and "Otros" not in r.text.split("<table>")[1]
    r = client.post("/merchants/TIENDA RARA XYZ/category", data={"category": "NoExiste"})
    assert r.status_code == 400


def test_presupuestos_proponer_y_fijar(client):
    r = client.get("/budgets")
    assert "Proponer presupuesto" in r.text
    r = client.post("/budgets/propose", data={"months": 6, "cut": 0.1}, follow_redirects=True)
    assert r.status_code == 200 and "Seguimiento del mes" in r.text
    assert "Alimentacion" in r.text
    r = client.post("/budgets/Ocio", data={"amount": 50, "month": "2026-10"})
    assert r.status_code == 200 and "Ocio" in r.text and "50 €" in r.text
    r = client.post("/budgets/Ocio", data={"amount": 0, "month": "2026-10"})
    assert r.status_code == 200 and r.text.strip() == ""


def test_alertas_recalcular_y_marcar_vistas(client):
    r = client.post("/alerts/refresh", follow_redirects=True)
    assert r.status_code == 200
    assert "anomalous_amount" in r.text or "duplicate_charge" in r.text
    first_id = r.text.split('hx-post="/alerts/')[1].split("/")[0]
    r = client.post(f"/alerts/{first_id}/ack")
    assert r.status_code == 200
    r = client.post("/alerts/ack-all", follow_redirects=True)
    assert "Nada pendiente" in r.text


def test_pipeline_trabajos_en_segundo_plano(client):
    r = client.post("/pipeline/jobs/ingest")
    assert r.status_code == 200 and "Importar Excel nuevos" in r.text
    job_id = r.text.split('id="job-')[1].split('"')[0]
    for _ in range(50):
        r = client.get(f"/pipeline/jobs/{job_id}")
        if "terminado" in r.text or "error" in r.text:
            break
        time.sleep(0.05)
    assert "terminado" in r.text and "no hay ficheros" in r.text
    r = client.post("/pipeline/jobs/alerts")
    job_id = r.text.split('id="job-')[1].split('"')[0]
    for _ in range(50):
        r = client.get(f"/pipeline/jobs/{job_id}")
        if "terminado" in r.text:
            break
        time.sleep(0.05)
    assert "recurrentes detectados" in r.text
    assert client.post("/pipeline/jobs/no-existe").status_code == 404


def test_pipeline_enriquecimiento_preview_y_toggle(client):
    r = client.get("/pipeline/enrich/preview")
    assert r.status_code == 200 and ("consultas saldrían" in r.text or "No hay nada" in r.text)
    r = client.get("/pipeline")
    assert "desactivado" in r.text
    r = client.post("/pipeline/enrichment/toggle", data={"enabled": 1}, follow_redirects=True)
    assert "activado" in r.text
    r = client.post("/pipeline/jobs/enrich")  # offline: el trabajo termina sin consultar nada
    job_id = r.text.split('id="job-')[1].split('"')[0]
    for _ in range(50):
        r = client.get(f"/pipeline/jobs/{job_id}")
        if "terminado" in r.text:
            break
        time.sleep(0.05)
    assert "modo offline" in r.text
    client.post("/pipeline/enrichment/toggle", data={"enabled": 0})
