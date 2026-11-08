"""Subir Excel desde la web: validación, nombres seguros, sin duplicar y con importación en el acto."""

import time
from pathlib import Path

from fastapi.testclient import TestClient

from moooney.config import load_settings
from moooney.demo import build_demo
from moooney.ingest.upload import MAX_BYTES, safe_name, save_upload
from moooney.web.app import create_app
from tests.conftest import make_santander_xlsx

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def _xlsx(tmp_path) -> bytes:
    path = tmp_path / "origen" / "extracto.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    make_santander_xlsx(path)
    return path.read_bytes()


def test_nombre_seguro_sin_rutas():
    assert safe_name("../../etc/passwd.xlsx") == "passwd.xlsx"
    ruta_windows = chr(92).join(["C:", "Users", "x", "Mis movs: <octubre>.XLSX"])
    assert safe_name(ruta_windows) == "Mis movs_ _octubre.xlsx"
    assert safe_name(".xlsx") == "extracto.xlsx"


def test_guardar_valida_y_no_duplica(tmp_path):
    carpeta = tmp_path / "excel"
    contenido = _xlsx(tmp_path)
    assert not save_upload(carpeta, "notas.txt", b"hola").ok
    assert not save_upload(carpeta, "falso.xlsx", b"esto no es un zip").ok
    assert not save_upload(carpeta, "vacio.xlsx", b"").ok
    roto = bytearray(contenido)
    roto[len(roto) // 2] ^= 0xFF  # un byte cambiado en medio: CRC incorrecto
    assert "dañado" in save_upload(carpeta, "roto.xlsx", bytes(roto)).message
    assert not save_upload(carpeta, "enorme.xlsx", b"PK\x03\x04" + b"0" * MAX_BYTES).ok

    r = save_upload(carpeta, "extracto.xlsx", contenido)
    assert r.ok and r.saved_as == "extracto.xlsx" and (carpeta / "extracto.xlsx").read_bytes() == contenido
    otra = save_upload(carpeta, "otro nombre.xlsx", contenido)  # mismo contenido: no se duplica
    assert otra.saved_as == "extracto.xlsx" and "ya estaba" in otra.message
    distinto = save_upload(carpeta, "extracto.xlsx", contenido + b"\0")  # mismo nombre, otro contenido
    assert distinto.saved_as == "extracto (2).xlsx"
    assert not list(carpeta.glob("*.part"))


def test_web_sube_e_importa(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    excel_dir = load_settings(settings_path).paths.excel_dir
    contenido = _xlsx(tmp_path)
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/pipeline")
        assert 'hx-post="/pipeline/upload"' in r.text and 'id="dropzone"' in r.text
        r = c.post("/pipeline/upload", files=[("files", ("notas.txt", b"hola", "text/plain"))])
        assert "No se ha guardado nada" in r.text and "no es un Excel" in r.text
        r = c.post("/pipeline/upload", files=[("files", ("mi extracto.xlsx", contenido, "application/octet-stream"))])
        assert r.status_code == 200 and "Importar Excel subidos" in r.text
        assert (excel_dir / "mi extracto.xlsx").exists()
        job_id = r.text.split('id="job-', 1)[1].split('"', 1)[0]
        for _ in range(100):
            r = c.get(f"/pipeline/jobs/{job_id}")
            if "en marcha" not in r.text:
                break
            time.sleep(0.1)
        assert "terminado" in r.text, r.text
        assert "mi extracto.xlsx: 3 filas, 3 nuevas" in r.text
        assert "importado" in c.get("/pipeline").text
