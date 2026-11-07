"""Web estática de la demo (GitHub Pages): solo datos inventados, enlaces relativos y nada del entorno."""

import json
import os
import re
from pathlib import Path

from moooney.web.snapshot import build_snapshot, canonical, file_for

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def test_canonical_ordena_y_quita_vacios():
    assert canonical("/transactions?month=2026-10&category=Ocio&flow=") == "/transactions?category=Ocio&month=2026-10"
    assert canonical("/transactions?key=PAGO%20MOVIL&amp;month=2026-10") == "/transactions?key=PAGO MOVIL&month=2026-10"
    assert file_for("/") == "index.html" and file_for("/trends") == "trends.html"
    assert file_for("/transactions/export.csv?month=2026-10").endswith(".csv")


def test_snapshot_relativo_y_sin_nada_del_entorno(tmp_path, monkeypatch):
    monkeypatch.setenv("LITELLM_API_KEY", "sk-secreto-que-no-debe-salir")
    monkeypatch.setenv("LITELLM_BASE_URL", "https://proxy-secreto.invalid/v1")
    cwd = Path.cwd()
    out = tmp_path / "site"
    r = build_snapshot(out, repo_config=REPO_CONFIG)
    # deja el proceso como estaba
    assert Path.cwd() == cwd and os.environ["LITELLM_API_KEY"] == "sk-secreto-que-no-debe-salir"
    assert "MOOONEY_OFFLINE" not in os.environ

    assert r.pages > 100 and (out / "index.html").exists() and (out / ".nojekyll").exists()
    pages = json.loads(re.search(r"=\s*(\{.*\});", (out / "static" / "pages.js").read_text(encoding="utf-8")).group(1))
    assert pages["/"] == "index.html" and "/?month=2026-05" in pages
    assert all((out / f).exists() for f in pages.values())

    index = (out / "index.html").read_text(encoding="utf-8")
    assert "Demo de solo lectura" in index and 'hx-boost="false"' in index
    assert 'src="static/snapshot.js"' in index
    for f in out.glob("*.html"):
        text = f.read_text(encoding="utf-8")
        assert not re.search(r'(href|src)="/', text), f.name  # todo relativo: vale en /moooney/
        assert "secreto" not in text and str(tmp_path) not in text
    assert "tu-proxy.example.com" in (out / "settings.html").read_text(encoding="utf-8")
