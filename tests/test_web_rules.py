"""Reglas creadas desde la web. Todo inventado."""

from pathlib import Path

from fastapi.testclient import TestClient

from moooney.categorize import web_rules
from moooney.categorize.rules import Rule, load_rules, match_rules
from moooney.categorize.taxonomy import load_taxonomy
from moooney.config import load_settings
from moooney.db import repo
from moooney.demo import build_demo
from moooney.web.app import create_app

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def test_patron_desde_texto():
    p1 = web_rules.build_pattern("  gasolinera  del norte", "empieza")
    p2 = web_rules.build_pattern("Café (centro)", "contiene")
    assert p1 == "^GASOLINERA DEL NORTE"
    assert p2 == r"CAFE \(CENTRO\)"
    assert web_rules.describe(p1) == ("empieza por", "GASOLINERA DEL NORTE")
    assert web_rules.describe(p2) == ("contiene", "CAFE (CENTRO)")
    assert web_rules.describe("^A|B")[0] == "expresión"


def test_orden_local_web_general(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    for f in ("rules.yaml", "categories.yaml"):
        (cfg / f).write_text((REPO_CONFIG / f).read_text(encoding="utf-8"), encoding="utf-8")
    tx = load_taxonomy(cfg / "categories.yaml")
    web = web_rules.path_for(cfg / "rules.yaml")
    web_rules.add(web, Rule(pattern="^NOMINA", category="Ocio", subcategory=tx.categories["Ocio"].subcategories[0],
                            flow_type="expense"))
    assert match_rules("NOMINA EMPRESA", load_rules(cfg / "rules.yaml", tx)).category == "Ocio"  # web > general
    (cfg / "rules.local.yaml").write_text(
        "rules:\n  - {pattern: '^NOMINA', category: Ingresos, subcategory: nomina, flow_type: income}\n", encoding="utf-8")
    assert match_rules("NOMINA EMPRESA", load_rules(cfg / "rules.yaml", tx)).category == "Ingresos"  # local > web
    assert web_rules.remove(web, "^NOMINA") and web_rules.read(web) == []


def test_desde_la_web(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    s = load_settings(settings_path)
    conn = repo.connect(s.paths.db_path)
    key = conn.execute("SELECT key FROM merchants WHERE source != 'user' ORDER BY key LIMIT 1").fetchone()[0]
    conn.close()
    prefijo = key[:6]
    with TestClient(create_app(settings_path)) as c:
        assert "Tus reglas" in c.get("/merchants").text
        r = c.get("/merchants/rules/preview", params={"text": prefijo.lower(), "mode": "empieza"})
        assert "Casa con" in r.text and key in r.text
        assert "al menos 3" in c.get("/merchants/rules/preview", params={"text": "a"}).text
        r = c.post("/merchants/rules", data={"text": prefijo, "mode": "empieza", "target": "Ocio|nada"})
        assert "regla_error" in r.headers.get("location", "") or r.status_code == 200
        sub = load_taxonomy(settings_path.parent / "categories.yaml").categories["Ocio"].subcategories[0]
        r = c.post("/merchants/rules", data={"text": prefijo, "mode": "empieza", "target": f"Ocio|{sub}"})
        assert "Regla guardada" in r.text
    conn = repo.connect(s.paths.db_path)
    cat = conn.execute("SELECT category, source FROM merchants WHERE key = ?", (key,)).fetchone()
    assert tuple(cat) == ("Ocio", "rule")
    assert web_rules.read(settings_path.parent / "rules.web.yaml")[0].pattern.startswith("^")
