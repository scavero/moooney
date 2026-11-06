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
    for path in ("/", "/trends", "/trends?category=Alimentacion", "/transactions", "/merchants", "/merchants?mode=all", "/recurring",
                 "/recurring?all=1", "/budgets", "/alerts", "/alerts?all=1", "/pipeline"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "moooney" in r.text
    r = client.get("/?month=2026-09")
    assert r.status_code == 200 and "2026-09" in r.text and "Diagnóstico" in r.text
    r = client.get("/trends")
    assert "Gasto previsto fin de mes" in r.text and "Dónde se ha ido el dinero" in r.text
    r = client.get("/transactions?month=2026-09&category=Alimentacion&q=MERCADONA")
    assert r.status_code == 200 and "MERCADONA" in r.text


def test_correccion_de_categoria_aplica_a_movimientos(client):
    r = client.post("/merchants/TIENDA RARA XYZ/category", data={"category": "Compras", "flow_type": ""})
    assert r.status_code == 200
    assert "corregido por ti" in r.text and "✓ " in r.text and "mov." in r.text
    r = client.get("/transactions?key=TIENDA%20RARA%20XYZ")
    # cada fila lleva un select con todas las categorías; la seleccionada debe ser la corregida
    assert 'value="Compras" selected' in r.text and 'value="Otros" selected' not in r.text
    r = client.post("/merchants/TIENDA RARA XYZ/category", data={"category": "NoExiste"})
    assert r.status_code == 400


def test_presupuestos_proponer_y_fijar(client):
    r = client.get("/budgets")
    assert "Proponer presupuesto" in r.text
    r = client.post("/budgets/propose", data={"months": 6, "cut": 10}, follow_redirects=True)  # recorte en %
    assert r.status_code == 200 and "Seguimiento" in r.text and "Mes transcurrido" in r.text
    assert "Alimentacion" in r.text
    r = client.post("/budgets/Ocio", data={"amount": 50, "month": "2026-10"})
    assert r.status_code == 200 and "Ocio" in r.text and 'value="50"' in r.text
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


def test_cola_de_dudosas_y_saltar(client):
    r = client.get("/merchants")
    assert r.status_code == 200 and "Revisar dudosas" in r.text
    first = r.text.split('<div class="key">')[1].split("</div>")[0]
    r = client.get(f"/merchants?skip={first}")
    assert r.status_code == 200 and "2 de" in r.text
    second = r.text.split('<div class="key">')[1].split("</div>")[0]
    assert second != first
    # etiquetas humanas, nunca identificadores internos
    r = client.get("/transactions")
    # (solo pueden aparecer como value= de un formulario, nunca como texto visible)
    for ident in ("card_settlement", "transfer_internal", "clef_web", "anomalous_amount"):
        assert f">{ident}<" not in r.text and f"{ident} ·" not in r.text
    assert "ingresos" in r.text and "gastos" in r.text


def test_alertas_filtro_por_nivel_y_grupo(client):
    client.post("/alerts/refresh")
    r = client.get("/alerts?severity=info")
    assert r.status_code == 200
    assert "crítica" not in r.text.split('class="alert-group"')[-1] if "alert-group" in r.text else True
    r = client.get("/alerts")
    if "ack-type/" in r.text:
        alert_type = r.text.split("ack-type/")[1].split('"')[0]
        r = client.post(f"/alerts/ack-type/{alert_type}", follow_redirects=True)
        assert r.status_code == 200 and f"ack-type/{alert_type}" not in r.text


def test_exportar_csv_respeta_el_filtro(client, tmp_path):
    r = client.get("/transactions/export.csv?month=2026-09&category=Alimentacion")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'filename="moooney-movimientos-2026-09-alimentacion.csv"' in r.headers["content-disposition"]
    lineas = r.text.lstrip("\ufeff").splitlines()
    assert lineas[0].startswith("fecha;concepto;comercio;categoria")
    n = client.get("/transactions?month=2026-09&category=Alimentacion").text.split(" movimientos")[0].rsplit(">", 1)[-1]
    assert len(lineas) == int(n) + 1
    assert all(";Alimentacion;" in ln for ln in lineas[1:]) and all(ln.startswith("2026-09-") for ln in lineas[1:])
    # misma función desde la CLI, a fichero con BOM para Excel
    from moooney.db import repo
    from moooney.export import write_csv

    conn = repo.connect(client.app.state.moooney.settings.paths.db_path)
    try:
        n_fich = write_csv(conn, tmp_path / "out" / "m.csv", month="2026-09", category="Alimentacion")
    finally:
        conn.close()
    assert n_fich == int(n) and (tmp_path / "out" / "m.csv").read_bytes().startswith(b"\xef\xbb\xbf")


def test_etiquetas_en_movimientos(client):
    r = client.get("/transactions?month=2026-09")
    tx_id = int(r.text.split('hx-post="/transactions/')[1].split("/")[0])
    r = client.post(f"/transactions/{tx_id}/tags", data={"tag": "  Viaje   China "})
    assert r.status_code == 200 and ">viaje china<" in r.text
    r = client.post(f"/transactions/{tx_id}/tags", data={"tag": "viaje china"})  # repetir no duplica
    assert r.text.count(">viaje china<") == 1
    r = client.get("/transactions?tag=viaje%20china")
    assert "1 movimientos" in r.text and "etiqueta «viaje china»" in r.text
    r = client.get("/transactions/export.csv?tag=viaje%20china")
    lineas = r.text.lstrip("\ufeff").splitlines()
    assert len(lineas) == 2 and ";viaje china;" in lineas[1] and "cuenta;etiquetas" in lineas[0]
    r = client.post(f"/transactions/{tx_id}/tags/remove", data={"tag": "viaje china"})
    assert r.status_code == 200 and "viaje china" not in r.text
    assert client.post("/transactions/999999/tags", data={"tag": "x"}).status_code == 404


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


def test_sin_datos_reales_se_sirve_la_demo_y_desaparece_al_importar(tmp_path, monkeypatch):
    """Base real vacía → demo marcada como tal; en cuanto hay movimientos reales, se usan."""
    import yaml

    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    for name in ("categories.yaml", "rules.yaml", "llm.yaml", "enrichment.yaml"):
        (cfg / name).write_text((REPO_CONFIG / name).read_text(encoding="utf-8"), encoding="utf-8")
    base = yaml.safe_load((REPO_CONFIG / "settings.yaml").read_text(encoding="utf-8"))
    base["paths"] = {"excel_dir": "data/excel", "clean_dir": "data/clean", "db_path": "data/moooney.db"}
    (cfg / "settings.yaml").write_text(yaml.safe_dump(base), encoding="utf-8")

    app = create_app(cfg / "settings.yaml")
    with TestClient(app) as c:
        r = c.get("/")
        assert r.status_code == 200
        assert "Datos de demostración" in r.text and "MERCADONA" in r.text
        assert (tmp_path / "data" / "demo" / "moooney.db").exists()
        r = c.get("/pipeline")
        assert "En la demo" in r.text

        # llega un Excel real: la ingesta va a la base real y la demo desaparece
        from tests.conftest import make_santander_xlsx

        (tmp_path / "data" / "excel").mkdir(parents=True, exist_ok=True)
        make_santander_xlsx(tmp_path / "data" / "excel" / "real.xlsx")
        r = c.post("/pipeline/jobs/ingest")
        job_id = r.text.split('id="job-')[1].split('"')[0]
        for _ in range(50):
            r = c.get(f"/pipeline/jobs/{job_id}")
            if "terminado" in r.text:
                break
            time.sleep(0.05)
        assert "3 nuevas" in r.text
        r = c.get("/")
        assert "Datos de demostración" not in r.text
        assert "2025-02" in r.text  # el mes del Excel real, no el de la demo (2026-10)

    # --demo fuerza la demo aunque haya datos reales
    app = create_app(cfg / "settings.yaml", force_demo=True)
    with TestClient(app) as c:
        assert "Datos de demostración" in c.get("/").text
