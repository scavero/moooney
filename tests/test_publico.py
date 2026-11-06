"""Lo que permite tener el repo público sin perder configuración: la URL del proxy por entorno
y las reglas propias del usuario en un fichero aparte, con prioridad."""

from pathlib import Path

from moooney.categorize.rules import load_rules, match_rules
from moooney.llm.client import load_llm_settings


def test_base_url_del_entorno_manda_sobre_el_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "llm.yaml"
    cfg.write_text("base_url: https://marcador.invalid/v1\nmodels: {clef: clef-flash}\n", encoding="utf-8")
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    assert load_llm_settings(cfg).base_url == "https://marcador.invalid/v1"
    monkeypatch.setenv("LITELLM_BASE_URL", "https://proxy-real.invalid/v1")
    assert load_llm_settings(cfg).base_url == "https://proxy-real.invalid/v1"


def test_reglas_locales_van_antes_que_las_generales(tmp_path):
    (tmp_path / "rules.yaml").write_text(
        "rules:\n  - {pattern: 'GIMNASIO', category: Ocio, subcategory: deporte, flow_type: expense}\n",
        encoding="utf-8",
    )
    sin_local = load_rules(tmp_path / "rules.yaml")
    assert match_rules("GIMNASIO CENTRAL", sin_local).category == "Ocio"
    (tmp_path / "rules.local.yaml").write_text(
        "rules:\n  - {pattern: 'GIMNASIO CENTRAL', category: Salud, subcategory: otros, flow_type: expense}\n",
        encoding="utf-8",
    )
    con_local = load_rules(tmp_path / "rules.yaml")
    assert len(con_local) == 2 and con_local[0].pattern == "GIMNASIO CENTRAL"
    assert match_rules("GIMNASIO CENTRAL", con_local).category == "Salud"


def test_el_repo_no_lleva_la_url_del_proxy_ni_reglas_locales():
    raiz = Path(__file__).resolve().parents[1]
    assert "example.com" in (raiz / "config" / "llm.yaml").read_text(encoding="utf-8")
    ignorado = (raiz / ".gitignore").read_text(encoding="utf-8")
    assert "config/rules.local.yaml" in ignorado
