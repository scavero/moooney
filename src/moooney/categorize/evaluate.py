"""Evaluación del decider contra claves etiquetadas a mano.

Las etiquetas viven en data/eval/merchants_labeled.csv (fuera de git: contienen
claves reales). Aquí solo se mide: acierto de categoría, acierto de flow_type
y calibración (¿se corresponde la confianza declarada con el acierto real?).
Ninguna respuesta del modelo se guarda en merchants: evaluar no categoriza.
"""

import csv
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from moooney.categorize.pipeline import KeyJob, build_state
from moooney.categorize.taxonomy import Taxonomy
from moooney.llm.client import LLMError
from moooney.llm.decide import Answer, Decider

# Tramos de confianza para la calibración: [inf, sup) con etiqueta legible.
CALIBRATION_BINS: list[tuple[str, float, float]] = [
    ("0.0-0.5", 0.0, 0.5),
    ("0.5-0.7", 0.5, 0.7),
    ("0.7-0.9", 0.7, 0.9),
    ("0.9-1.0", 0.9, 1.001),
]


@dataclass
class LabeledKey:
    key: str
    category: str
    flow_type: str


@dataclass
class EvalReport:
    total: int = 0
    missing: list[str] = field(default_factory=list)
    errors: int = 0
    category_ok: int = 0
    flow_ok: int = 0
    cat_bins: dict[str, list[int]] = field(
        default_factory=lambda: {nombre: [0, 0] for nombre, _, _ in CALIBRATION_BINS}
    )
    misses: list[tuple[str, str, str, float]] = field(default_factory=list)
    flow_misses: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def evaluated(self) -> int:
        return self.total - len(self.missing) - self.errors

    def category_accuracy(self) -> float:
        return self.category_ok / self.evaluated if self.evaluated else 0.0

    def flow_accuracy(self) -> float:
        return self.flow_ok / self.evaluated if self.evaluated else 0.0


def load_labels(path: Path) -> list[LabeledKey]:
    """Lee el CSV de etiquetas: columnas key,category,flow_type."""
    if not path.exists():
        raise FileNotFoundError(f"no existe el fichero de etiquetas: {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        needed = {"key", "category", "flow_type"}
        if reader.fieldnames is None or not needed <= set(reader.fieldnames):
            raise ValueError("el CSV de etiquetas necesita las columnas key,category,flow_type")
        return [
            LabeledKey(
                key=row["key"].strip(),
                category=row["category"].strip(),
                flow_type=row["flow_type"].strip(),
            )
            for row in reader
            if row.get("key", "").strip()
        ]


def job_for_key(conn: sqlite3.Connection, key: str) -> KeyJob | None:
    """Agregados de una clave etiquetada, o None si ya no está en la base de datos."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, AVG(amount) AS avg_amount, MIN(date) AS dmin,"
        " MAX(date) AS dmax FROM transactions WHERE concept_clean = ?",
        (key,),
    ).fetchone()
    if row is None or not row["n"]:
        return None
    gap = None
    if row["n"] > 1 and row["dmin"] and row["dmax"]:
        gap = (date.fromisoformat(row["dmax"]) - date.fromisoformat(row["dmin"])).days / (
            row["n"] - 1
        )
    return KeyJob(key=key, n=int(row["n"]), avg_amount=float(row["avg_amount"]), gap_days=gap)


def _bin_of(confidence: float) -> str:
    for nombre, inf, sup in CALIBRATION_BINS:
        if inf <= confidence < sup:
            return nombre
    return CALIBRATION_BINS[-1][0]


def evaluate(
    conn: sqlite3.Connection,
    labels: list[LabeledKey],
    decider: Decider,
    taxonomy: Taxonomy,
    questions: dict,
    *,
    max_workers: int = 8,
) -> EvalReport:
    """Pregunta el decider por cada clave etiquetada y mide aciertos.

    Las llamadas se hacen en paralelo (solo red); la cuenta se lleva aquí, en
    el hilo principal. No se escribe nada en merchants.
    """
    report = EvalReport(total=len(labels))
    pairs: list[tuple[LabeledKey, str]] = []
    for label in labels:
        job = job_for_key(conn, label.key)
        if job is None:
            report.missing.append(label.key)
            continue
        pairs.append((label, build_state(job)))

    def ask(state: str) -> dict[str, Answer]:
        return decider.decide(state, questions)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pairs) or 1))) as pool:
        futures = [pool.submit(ask, state) for _, state in pairs]
        for (label, _), future in zip(pairs, futures, strict=True):
            try:
                answers = future.result()
            except (LLMError, ValueError):
                report.errors += 1
                continue
            cat = answers.get("category")
            flow = answers.get("flow_type")
            cat_choice = cat.choice if cat is not None and cat.choice in taxonomy.categories else None
            confianza = cat.confidence if cat is not None else 0.0
            acierto = cat_choice == label.category
            if acierto:
                report.category_ok += 1
            if flow is not None and flow.choice == label.flow_type:
                report.flow_ok += 1
            elif flow is not None:
                report.flow_misses.append((label.key, label.flow_type, flow.choice))
            if not acierto:
                report.misses.append((label.key, label.category, cat_choice or "—", confianza))
            tramo = report.cat_bins[_bin_of(confianza)]
            tramo[1] += 1
            if acierto:
                tramo[0] += 1
    return report
