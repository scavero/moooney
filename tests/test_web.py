"""Fase 4: la web sobre la base de demostración (sintética). Sin red: offline forzado."""

import re
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
    assert 'class="verdict-figure' in r.text and ("has ahorrado" in r.text or "Has ahorrado" in r.text or "gastado" in r.text)
    r = client.get("/trends")
    assert "Gasto previsto fin de mes" in r.text and "Dónde se ha ido el dinero" in r.text
    r = client.get("/transactions?month=2026-09&category=Alimentacion&q=MERCADONA")
    assert r.status_code == 200 and "MERCADONA" in r.text


def test_no_se_navega_a_meses_sin_datos(client):
    """Las flechas se paran en el primer y el último mes con datos, y una URL fuera de rango vuelve dentro."""
    for page in ("/", "/budgets", "/report"):
        r = client.get(page)
        assert "es el último mes con datos" in r.text and 'aria-label="mes siguiente"' not in r.text, page
        assert 'aria-label="mes anterior"' in r.text
        futuro = client.get(page, params={"month": "2031-01"})
        assert futuro.status_code == 200 and "es el último mes con datos" in futuro.text, page
        pasado = client.get(page, params={"month": "1999-01"})
        assert "no hay datos anteriores" in pasado.text and 'aria-label="mes siguiente"' in pasado.text, page
    assert client.get("/", params={"month": "basura"}).status_code == 200


def test_navegacion_movil(client):
    """Barra inferior con cuatro accesos y «Más», que controla el menú completo y se marca fuera de ellos."""
    r = client.get("/")
    assert 'class="tabbar"' in r.text and 'aria-controls="side-nav"' in r.text and "viewport-fit=cover" in r.text
    assert 'id="more-btn" class=""' in r.text
    assert 'id="more-btn" class="active"' in client.get("/budgets").text
    assert 'id="more-btn" class=""' in client.get("/transactions").text


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
    assert r.status_code == 200 and "Revisar dudosas" in r.text and "Últimos movimientos" in r.text
    assert "<details class=\"all-keys\" >" in r.text  # la tabla, plegada
    assert "<details class=\"all-keys\" open>" in client.get("/merchants?mode=all").text
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


def test_ajustes_guardar_probar_y_quitar_clave(client, monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    client.app.state.moooney.env_sources = {"MOOONEY_OFFLINE": "entorno"}
    r = client.get("/settings")
    assert r.status_code == 200 and "sin configurar" in r.text and "Ajustes" in r.text

    r = client.post("/settings/key", data={"name": "LITELLM_API_KEY", "value": " sk-prueba-1234wxyz "}, follow_redirects=True)
    assert r.status_code == 200 and "Clave guardada" in r.text and "configurada" in r.text
    assert "…wxyz" in r.text and "sk-prueba-1234wxyz" not in r.text  # nunca la clave entera
    env_path = client.app.state.moooney.settings.paths.db_path.parent / ".env"
    assert "LITELLM_API_KEY=sk-prueba-1234wxyz" in env_path.read_text(encoding="utf-8")

    # offline forzado en el fixture: la prueba de conexión lo dice sin salir a la red
    r = client.post("/settings/test")
    assert r.status_code == 200 and "offline" in r.text.lower()

    r = client.post("/settings/key", data={"name": "LITELLM_API_KEY", "value": "  "}, follow_redirects=True)
    assert "vacía" in r.text
    r = client.post("/settings/key", data={"name": "OTRA_COSA", "value": "x"})
    assert r.status_code == 404

    r = client.post("/settings/key/remove", data={"name": "LITELLM_API_KEY"}, follow_redirects=True)
    assert "Clave quitada" in r.text and "sin configurar" in r.text
    assert "LITELLM_API_KEY=" not in env_path.read_text(encoding="utf-8")


def test_ajustes_interruptor_offline(client):
    import os

    r = client.post("/settings/offline", data={"enabled": "0"}, follow_redirects=True)
    assert r.status_code == 200 and os.environ["MOOONEY_OFFLINE"] == "0" and "desactivado" in r.text
    r = client.post("/settings/offline", data={"enabled": "1"}, follow_redirects=True)
    assert os.environ["MOOONEY_OFFLINE"] == "1" and "activado" in r.text


def test_prueba_de_conexion_con_transporte_simulado(tmp_path, monkeypatch):
    import httpx

    from moooney.web import service

    monkeypatch.delenv("MOOONEY_OFFLINE", raising=False)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-x")
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [
        {"id": m} for m in ("clef-flash", "qwen3.8-flash-next-fp8", "bge-m3", "gemma-4-e2b", "otro")]}))
    res = service.test_llm_connection(service.Paths(REPO_CONFIG), transport=ok)
    assert res["ok"] and "Conexión correcta" in res["message"]
    corto = httpx.MockTransport(lambda r: httpx.Response(200, json={"data": [{"id": "clef-flash"}]}))
    res = service.test_llm_connection(service.Paths(REPO_CONFIG), transport=corto)
    assert not res["ok"] and "no ofrece" in res["message"]
    res = service.test_llm_connection(service.Paths(REPO_CONFIG), transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    assert not res["ok"] and "401" in res["message"]


def test_movimientos_agrupados_por_dia_con_desplegable(client):
    r = client.get("/transactions?month=2026-09")
    assert r.status_code == 200
    # cabecera de día en castellano con el total del día, y una fila plegada por movimiento
    assert 'class="day-row"' in r.text and " de septiembre" in r.text
    n_rows = r.text.count('class="tx-more"')
    assert n_rows > 0 and r.text.count('class="more"') == n_rows
    # flujo y origen viven en el desplegable, no en la fila
    assert "<dt>Flujo</dt>" in r.text and "<dt>Origen</dt>" in r.text


def test_agrupar_por_dia_suma_importes():
    from moooney.web.app import fecha_larga, group_by_day

    rows = [{"date": "2026-09-02", "amount": -5.0}, {"date": "2026-09-02", "amount": 20.0}, {"date": "2026-09-01", "amount": -1.5}]
    g = group_by_day(rows)
    assert [x["date"] for x in g] == ["2026-09-02", "2026-09-01"]
    assert g[0]["total"] == 15.0 and len(g[0]["rows"]) == 2 and g[1]["total"] == -1.5
    assert fecha_larga("2026-09-02") == "miércoles 2 de septiembre"


def test_modo_oscuro_selector_y_colores_en_variables(client):
    r = client.get("/")
    assert 'id="theme-toggle"' in r.text and "moooney-theme" in r.text
    css = (Path(__file__).resolve().parents[1] / "src" / "moooney" / "web" / "static" / "app.css").read_text(encoding="utf-8")
    claro = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    oscuro_ini = css.index(':root[data-theme="dark"] {')
    oscuro = css[oscuro_ini:css.index("}", oscuro_ini)]
    definidas = set(re.findall(r"(--[a-z0-9-]+):", claro))
    redefinidas = set(re.findall(r"(--[a-z0-9-]+):", oscuro))
    assert redefinidas and redefinidas <= definidas  # el oscuro solo cambia tokens que existen
    # fuera de los dos bloques de tokens no hay colores fijos: el tema oscuro los alcanza todos
    resto = css[:css.index(":root {")] + css[css.index("}", oscuro_ini):]
    resto = re.sub(r"/\*.*?\*/", "", resto, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,6}\b|rgba\(14", resto)
