"""Empezar de cero: se borran movimientos y extractos, se conservan memoria y presupuestos.
Todo inventado, generado en tmp_path."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from moooney.config import load_settings
from moooney.db import repo
from moooney.demo import build_demo
from moooney.ingest.load import ingest_directory
from moooney.ingest.reset import reset_data, summary
from moooney.web.app import create_app
from tests.conftest import make_santander_xlsx

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def test_borra_movimientos_y_conserva_memoria(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    make_santander_xlsx(settings.paths.excel_dir / "a.xlsx")
    ingest_directory(settings.paths.excel_dir, conn, settings)
    repo.upsert_merchant(conn, key="TIENDA", category="Compras", subcategory="online",
                         flow_type="expense", source="user", confidence=1.0, updated_at="t")
    with conn:
        conn.execute("INSERT INTO budgets (category, month, limit_amount) VALUES ('Compras', '2025-02', 100)")
    informes = settings.paths.reports_dir
    assert tmp_path in informes.parents  # nunca la carpeta reports/ del repositorio
    informes.mkdir()
    (informes / "2025-02.md").write_text("informe inventado", encoding="utf-8")
    assert summary(conn, settings) == {"movimientos": 3, "importaciones": 1, "excel": 1}

    r = reset_data(conn, settings)
    assert (r.movimientos, r.importaciones, r.excel, r.informes) == (3, 1, 1, 1)
    assert summary(conn, settings) == {"movimientos": 0, "importaciones": 0, "excel": 0}
    assert not list(settings.paths.clean_dir.glob("*.csv"))
    assert repo.get_merchant(conn, "TIENDA")["category"] == "Compras"
    assert conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 1

    # al volver a subir el mismo Excel entra entero otra vez
    make_santander_xlsx(settings.paths.excel_dir / "a.xlsx")
    [o] = ingest_directory(settings.paths.excel_dir, conn, settings)
    assert o.rows_new == 3


def test_desde_la_web_hay_que_escribir_la_palabra(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    s = load_settings(settings_path)
    with TestClient(create_app(settings_path)) as c:
        assert 'hx-post="/pipeline/jobs/reset"' in c.get("/pipeline").text
        r = c.post("/pipeline/jobs/reset")  # sin escribir nada
        assert "No se ha borrado nada" in r.text
        r = c.post("/pipeline/jobs/reset", headers={"HX-Prompt": "borrar"})
        job_id = r.text.split('id="job-', 1)[1].split('"', 1)[0]
        for _ in range(100):
            r = c.get(f"/pipeline/jobs/{job_id}")
            if 'data-job-status="running"' not in r.text:
                break
            time.sleep(0.05)
        assert "terminado" in r.text and "se conservan la memoria" in r.text, r.text
    conn = repo.connect(s.paths.db_path)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] > 0
