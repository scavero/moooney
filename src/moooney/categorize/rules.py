"""Motor de reglas deterministas aplicadas sobre la clave de comercio.

La lista de config/rules.yaml está ordenada: la primera que casa gana.
Se casa con re.search sobre la clave normalizada (mayúsculas, sin acentos).
"""

import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from moooney.categorize.taxonomy import Taxonomy

_COMPILED: dict[str, re.Pattern[str]] = {}


class Rule(BaseModel):
    pattern: str
    category: str
    subcategory: str
    flow_type: str


class RuleMatch(BaseModel):
    category: str
    subcategory: str
    flow_type: str
    pattern: str


def _compiled(pattern: str) -> re.Pattern[str]:
    if pattern not in _COMPILED:
        try:
            _COMPILED[pattern] = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"regex invalida en rules.yaml: {pattern!r} ({exc})") from exc
    return _COMPILED[pattern]


def load_rules(path: Path | None = None, taxonomy: Taxonomy | None = None) -> list[Rule]:
    """Carga la lista ordenada de reglas y la valida contra la taxonomía."""
    if path is None:
        path = Path("config/rules.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no se encuentra la configuracion de reglas: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    rules = [Rule.model_validate(r) for r in data.get("rules", [])]
    for rule in rules:
        _compiled(rule.pattern)
        if taxonomy is None:
            continue
        if rule.category not in taxonomy.categories:
            raise ValueError(f"categoria desconocida en rules.yaml: {rule.category!r}")
        spec = taxonomy.categories[rule.category]
        if rule.subcategory not in spec.subcategories:
            raise ValueError(
                f"subcategoria {rule.subcategory!r} invalida para {rule.category!r} en rules.yaml"
            )
        if rule.flow_type not in taxonomy.flow_types:
            raise ValueError(f"flow_type desconocido en rules.yaml: {rule.flow_type!r}")
    return rules


def match_rules(key: str, rules: list[Rule]) -> RuleMatch | None:
    """Primera regla que casa con la clave, o None."""
    for rule in rules:
        if _compiled(rule.pattern).search(key):
            return RuleMatch(
                category=rule.category,
                subcategory=rule.subcategory,
                flow_type=rule.flow_type,
                pattern=rule.pattern,
            )
    return None
