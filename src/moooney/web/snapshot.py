"""Copia estática de la demo para GitHub Pages: recorre la web sobre los datos inventados y guarda
cada página como un fichero HTML con enlaces relativos.

No hay servidor detrás, así que:
- cada URL de la app (ruta + parámetros) se guarda en un fichero y `static/pages.js` lleva el mapa
  URL → fichero, que `static/snapshot.js` usa para los filtros, los selectores de mes y los gráficos;
- lo que escribe (formularios POST, hx-post) se queda en un aviso de solo lectura;
- se genera en una carpeta temporal con solo la configuración del repo y el entorno limpio: ni
  `data/`, ni `config/rules.local.yaml`, ni `budgets.yaml`, ni claves o URL del proxy pueden colarse.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from moooney.categorize.taxonomy import load_taxonomy
from moooney.envfile import MANAGED

HERE = Path(__file__).resolve().parent

# Solo lo que está en git: nada de rules.local.yaml ni budgets.yaml del usuario.
CONFIG_FILES = ("settings.yaml", "categories.yaml", "rules.yaml", "llm.yaml", "enrichment.yaml")
SEED_PATHS = ("/", "/trends", "/transactions", "/merchants", "/recurring", "/budgets", "/alerts",
              "/report", "/pipeline", "/settings")
PAGE_NAMES = {"/": "index.html"} | {p: p.strip("/") + ".html" for p in SEED_PATHS if p != "/"}
LINK_ATTR = re.compile(r'(href|src)="(/[^"]*)"')
MAX_PAGES = 3000


@dataclass
class SnapshotResult:
    pages: int = 0
    assets: int = 0
    missing: set[str] = field(default_factory=set)
    size_bytes: int = 0


def canonical(url: str) -> str:
    """Ruta + parámetros no vacíos, ordenados y sin codificar. Igual que `clave()` en snapshot.js."""
    parts = urlsplit(html.unescape(url))
    pairs = sorted((k, v) for k, v in parse_qsl(parts.query) if v != "")
    return parts.path + ("?" + "&".join(f"{k}={v}" for k, v in pairs) if pairs else "")


def file_for(key: str) -> str:
    if key in PAGE_NAMES:
        return PAGE_NAMES[key]
    path = key.split("?", 1)[0]
    ext = ".csv" if path.endswith(".csv") else ".html"
    stem = re.sub(r"[^a-z0-9]+", "-", path.removesuffix(".csv").lower()).strip("-") or "index"
    return f"{stem}-{hashlib.sha1(key.encode('utf-8')).hexdigest()[:10]}{ext}"


def _request_url(key: str) -> str:
    path, _, query = key.partition("?")
    return path + ("?" + urlencode(parse_qsl(query)) if query else "")


def _crawlable(key: str, months: set[str]) -> bool:
    """Páginas que merece la pena guardar: las de los meses con datos."""
    path, _, query = key.partition("?")
    if path.startswith(("/static/", "/pipeline/jobs/")):
        return False
    month = dict(parse_qsl(query)).get("month")
    return month is None or month in months  # las flechas de mes siguen más allá de los datos


def build_snapshot(out: Path, *, repo_config: Path = Path("config")) -> SnapshotResult:
    """Genera la web estática de la demo en `out` (se vacía antes)."""
    from fastapi.testclient import TestClient

    from moooney.web.app import create_app

    repo_config = Path(repo_config).resolve()
    out = Path(out).resolve()
    saved_env = {k: os.environ.pop(k) for k in (*MANAGED, "MOOONEY_CONFIG", "MOOONEY_FORCE_DEMO")
                 if k in os.environ}
    os.environ["MOOONEY_OFFLINE"] = "1"
    old_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="moooney-snapshot-") as tmp:
        work = Path(tmp)
        (work / "config").mkdir()
        (work / "data").mkdir()
        for name in CONFIG_FILES:
            shutil.copy(repo_config / name, work / "config" / name)
        os.chdir(work)  # rutas relativas en las páginas (data/excel), nunca las de la máquina
        try:
            app = create_app(Path("config/settings.yaml"), force_demo=True, snapshot=True)
            with TestClient(app) as client:
                _prepare_demo(client)
                return _crawl(client, out)
        finally:
            os.chdir(old_cwd)
            os.environ.pop("MOOONEY_OFFLINE", None)
            os.environ.update(saved_env)


def _prepare_demo(client) -> None:
    """Deja la demo como la vería alguien tras un rato de uso: presupuestos, alertas e informes."""
    client.post("/budgets/propose", follow_redirects=False)
    client.post("/alerts/refresh", follow_redirects=False)
    months = re.findall(r'<option value="(\d{4}-\d{2})"', client.get("/report").text)
    for m in sorted(set(months)):
        client.post(f"/report/generate-offline?month={m}", follow_redirects=False)


def _months(client) -> list[str]:
    return sorted(set(re.findall(r'<option value="(\d{4}-\d{2})"', client.get("/").text)))


def _seeds(months: list[str]) -> list[str]:
    seeds = list(SEED_PATHS)
    cats = list(load_taxonomy(Path("config/categories.yaml")).category_names)
    for m in months:
        seeds += [f"/?month={m}", f"/budgets?month={m}", f"/report?month={m}", f"/transactions?month={m}"]
        seeds += [f"/transactions?month={m}&category={c}" for c in sorted(cats)]
    seeds += [f"/trends?category={c}" for c in sorted(cats)]
    return seeds


def _crawl(client, out: Path) -> SnapshotResult:
    result = SnapshotResult()
    bodies: dict[str, bytes] = {}
    months = _months(client)
    queue = deque(canonical(s) for s in _seeds(months))
    seen = set(queue)
    while queue and len(bodies) < MAX_PAGES:
        key = queue.popleft()
        r = client.get(_request_url(key), follow_redirects=True)
        if r.status_code != 200:
            result.missing.add(key)
            continue
        bodies[key] = r.content
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        for _, link in LINK_ATTR.findall(r.text):
            k = canonical(link)
            if _crawlable(k, set(months)) and k not in seen:
                seen.add(k)
                queue.append(k)

    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(HERE / "static", out / "static")
    result.assets = sum(1 for p in (out / "static").rglob("*") if p.is_file())
    manifest = {k: file_for(k) for k in bodies}

    def rewrite(m: re.Match) -> str:
        attr, link = m.groups()
        if link.startswith("/static/"):
            return f'{attr}="{link[1:]}"'
        k = canonical(link)
        if k in manifest:
            return f'{attr}="{html.escape(manifest[k])}"'
        result.missing.add(k)
        return f'{attr}="#" data-falta="1"'

    for key, body in bodies.items():
        target = out / manifest[key]
        if manifest[key].endswith(".html"):
            target.write_text(LINK_ATTR.sub(rewrite, body.decode("utf-8")), encoding="utf-8")
        else:
            target.write_bytes(body)
    (out / "static" / "pages.js").write_text(
        "window.MOOONEY_PAGES = " + json.dumps(manifest, ensure_ascii=False, sort_keys=True) + ";\n",
        encoding="utf-8",
    )
    (out / ".nojekyll").write_text("", encoding="utf-8")
    result.pages = len(bodies)
    result.size_bytes = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    return result
