"""Reglas: orden, precedencia y validación contra la taxonomía."""

from pathlib import Path

import pytest

from moooney.categorize.rules import Rule, load_rules, match_rules
from moooney.categorize.taxonomy import load_taxonomy

CONFIG_DIR = Path(__file__).resolve().parents[1] / "config"


def _rules() -> list[Rule]:
    return load_rules(CONFIG_DIR / "rules.yaml", load_taxonomy(CONFIG_DIR / "categories.yaml"))


def test_config_reales_validas():
    rules = _rules()
    assert len(rules) > 20


def test_primera_regla_gana_streaming_sobre_compras():
    rules = _rules()
    match = match_rules("AMAZON PRIME", rules)
    assert match is not None and match.category == "Suscripciones"
    assert match.subcategory == "streaming"


def test_amazon_normal_es_compras():
    match = match_rules("WWW AMAZON", _rules())
    assert match is not None and match.category == "Compras"


def test_bizum_enviado_con_comercio_es_transferencia():
    match = match_rules("BIZUM ENVIADO PIZZERIA CUALQUIERA", _rules())
    assert match is not None
    assert (match.category, match.flow_type) == ("Transferencias", "expense")


def test_inversion_antes_que_estado():
    match = match_rules("SUSCRIPCION FONDOS MI BANCO GLOBAL FI", _rules())
    assert match is not None
    assert (match.category, match.flow_type) == ("Ahorro e inversion", "investment")


def test_suscripcion_de_marca_no_es_inversion():
    match = match_rules("SUSCRIPCION NETFLIX MENSUAL", _rules())
    assert match is not None and match.category == "Suscripciones"


def test_via_te_peaje(tmp_path):
    rules = load_rules(_write(tmp_path, "VIA.?T|PEAJE|AUDASA|WETACA"), taxonomy=None)
    for key in ("VIA-T SANTANDER", "PEAJE AP-9", "AUDASA", "WETACA"):
        match = match_rules(key, rules)
        assert match is not None and match.subcategory == "peajes", key


def test_sin_regla_devuelve_none():
    assert match_rules("ESTABLECIMIENTO MISTERIOSO XYZ", _rules()) is None


def _write(tmp_path, pattern: str) -> Path:
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        f"  - {{pattern: '{pattern}', category: Transporte, subcategory: peajes,"
        f" flow_type: expense}}\n",
        encoding="utf-8",
    )
    return path


def test_categoria_inexistente_rechazada(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "  - {pattern: 'X', category: Cosas raras, subcategory: peajes, flow_type: expense}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="categoria desconocida"):
        load_rules(path, load_taxonomy(CONFIG_DIR / "categories.yaml"))


def test_subcategoria_inexistente_rechazada(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "  - {pattern: 'X', category: Transporte, subcategory: submarino, flow_type: expense}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="subcategoria"):
        load_rules(path, load_taxonomy(CONFIG_DIR / "categories.yaml"))


def test_regex_invalida_rechazada(tmp_path):
    path = tmp_path / "rules.yaml"
    path.write_text(
        "rules:\n"
        "  - {pattern: '(((', category: Transporte, subcategory: peajes, flow_type: expense}\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="regex invalida"):
        load_rules(path)


def test_reglas_reales_estaciones_de_servicio_y_marcas_vistas():
    from pathlib import Path

    from moooney.categorize.rules import load_rules, match_rules
    from moooney.categorize.taxonomy import load_taxonomy

    raiz = Path(__file__).resolve().parent.parent
    taxonomy = load_taxonomy(raiz / "config" / "categories.yaml")
    rules = load_rules(raiz / "config" / "rules.yaml", taxonomy=taxonomy)
    casos = {
        "E S FICTICIA": ("Transporte", "combustible"),
        "ES PUEBLO INVENTADO": ("Transporte", "combustible"),
        "O XANTAR DE NADIE": ("Alimentacion", "restaurantes"),
        "TASCA IMAGINARIA": ("Alimentacion", "restaurantes"),
        "FIVE GUYS SPAIN": ("Alimentacion", "restaurantes"),
        "OCINE PREMIUM": ("Ocio", "cultura"),
        "ENTRADAS EVENTO": ("Ocio", "cultura"),
        "INTIM UOMO NERV": ("Compras", "ropa y calzado"),
        "ANA AEROPORTOS": ("Transporte", "transporte publico"),
        "UKVI": ("Impuestos y tasas", "trafico"),
        "GA VI C C C": ("Alimentacion", "supermercado"),
        "ALIPAY TIENDA FICTICIA": ("Ocio", "viajes"),
        "SOCIEDAD ESPANOLA DE ALGO": ("Educacion", "formacion"),
    }
    for clave, (cat, sub) in casos.items():
        m = match_rules(clave, rules)
        assert m is not None, clave
        assert (m.category, m.subcategory) == (cat, sub), clave
    # "ESTUDIO MA" o "ESTRELLA" no son estaciones de servicio
    for clave in ("ESTUDIO MA", "ESTRELLA", "ESPACIO X"):
        m = match_rules(clave, rules)
        assert m is None or m.subcategory != "combustible", clave
