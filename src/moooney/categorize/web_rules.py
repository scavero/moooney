"""Reglas creadas desde la web (decisión 2026-11-11).

Viven en `config/rules.web.yaml`, junto a `rules.yaml`, fuera de git y de la imagen Docker
porque nombran comercios del usuario. Solo las escribe la web: así se puede reescribir el
fichero entero sin perder comentarios de nadie; `rules.local.yaml` sigue siendo del usuario a
mano. Orden al casar: `rules.local.yaml`, después estas y al final las generales.

La web no pide expresiones regulares: el usuario escribe un texto y elige «empieza por» o
«contiene». El texto se normaliza como las claves (mayúsculas, sin acentos, espacios simples)
y se escapa.
"""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path

import yaml

from moooney.categorize.rules import Rule

FILENAME = "rules.web.yaml"
MODES = {"empieza": "empieza por", "contiene": "contiene"}
HEADER = (
    "# Reglas creadas desde la web (Comercios → Tus reglas). Las reescribe la web: para reglas\n"
    "# escritas a mano usa rules.local.yaml, que tiene prioridad sobre estas.\n"
)


def path_for(rules_path: Path) -> Path:
    return rules_path.with_name(FILENAME)


def normalize_text(text: str) -> str:
    """Como las claves de comercio: mayúsculas, sin acentos, espacios simples."""
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c)).upper()
    return " ".join(t.split())


def build_pattern(text: str, mode: str) -> str:
    norm = normalize_text(text)
    if len(norm) < 3:
        raise ValueError("escribe al menos 3 letras")
    if mode not in MODES:
        raise ValueError(f"modo desconocido: {mode}")
    return ("^" if mode == "empieza" else "") + _escape(norm)


def _escape(text: str) -> str:
    """re.escape sin escapar los espacios, para que la regla se lea bien en el YAML."""
    return re.escape(text).replace(r"\ ", " ")


def describe(pattern: str) -> tuple[str, str]:
    """(«empieza por» | «contiene» | «expresión», texto) para enseñar una regla."""
    body = pattern[1:] if pattern.startswith("^") else pattern
    plain = re.sub(r"\\(.)", r"\1", body)
    if _escape(plain) == body:
        return (MODES["empieza"] if pattern.startswith("^") else MODES["contiene"]), plain
    return "expresión", pattern


def read(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return [Rule.model_validate(r) for r in data.get("rules", [])]


def _write(path: Path, rules: list[Rule]) -> None:
    body = yaml.safe_dump({"rules": [r.model_dump() for r in rules]}, allow_unicode=True, sort_keys=False)
    tmp = path.with_suffix(".part")
    tmp.write_text(HEADER + body, encoding="utf-8")
    tmp.replace(path)


def add(path: Path, rule: Rule) -> None:
    """Añade la regla delante: la más reciente gana sobre las web anteriores. Si ya había una con
    el mismo patrón, se sustituye."""
    rules = [r for r in read(path) if r.pattern != rule.pattern]
    _write(path, [rule, *rules])


def remove(path: Path, pattern: str) -> bool:
    rules = read(path)
    keep = [r for r in rules if r.pattern != pattern]
    if len(keep) == len(rules):
        return False
    _write(path, keep)
    return True


def preview(conn: sqlite3.Connection, pattern: str, limit: int = 12) -> dict:
    """Qué claves casarían: las que cambiarían y las que no se tocan por estar corregidas a mano."""
    rx = re.compile(pattern)
    rows = conn.execute(
        "SELECT m.key, m.category, m.source, COUNT(t.id) AS n, COALESCE(SUM(t.amount), 0) AS total "
        "FROM merchants m LEFT JOIN transactions t ON t.concept_clean = m.key GROUP BY m.key"
    ).fetchall()
    hits = [r for r in rows if rx.search(r["key"])]
    hits.sort(key=lambda r: -r["n"])
    return {
        "total": len(hits),
        "user": sum(1 for r in hits if r["source"] == "user"),
        "movimientos": sum(r["n"] for r in hits if r["source"] != "user"),
        "sample": hits[:limit],
    }
