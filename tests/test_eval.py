"""Evaluación del decider con un decider falso: sin red ni datos reales."""

import sqlite3

import pytest
import yaml

from moooney.categorize.evaluate import evaluate, load_labels
from moooney.categorize.pipeline import build_questions, build_state
from moooney.categorize.taxonomy import load_taxonomy
from moooney.db import repo
from moooney.llm.client import LLMError
from moooney.llm.decide import Answer

MINI_CATEGORIES = {
    "categories": {
        "Alimentacion": {
            "flow_type": "expense",
            "description": "Compra de comida",
            "subcategories": ["supermercado"],
        },
        "Otros": {"flow_type": "expense", "description": "Lo que no encaja"},
    },
    "flow_types": {"expense": "Gasto", "income": "Ingreso"},
}


@pytest.fixture()
def taxonomy(tmp_path):
    path = tmp_path / "categories.yaml"
    path.write_text(yaml.safe_dump(MINI_CATEGORIES, allow_unicode=True), encoding="utf-8")
    return load_taxonomy(path)


def _db(tmp_path, keys) -> sqlite3.Connection:
    conn = repo.connect(tmp_path / "test.db")
    with conn:
        for i, key in enumerate(keys):
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, concept, concept_clean,"
                " amount, account, created_at) VALUES (?, '2025-01-01', 'X', ?, -10.0,"
                " 'principal', '2025-01-01T00:00:00')",
                (f"f{i}", key),
            )
    return conn


class FakeDecider:
    """Responde según un mapa clave -> (categoria, flow, confianza)."""

    def __init__(self, respuestas: dict[str, tuple[str, str, float]]):
        self.respuestas = respuestas
        self.estados_vistos: list[str] = []

    def decide(self, state: str, questions: dict) -> dict[str, Answer]:
        self.estados_vistos.append(state)
        for clave, (cat, flow, conf) in self.respuestas.items():
            if f"Concepto de un apunte: {clave}." in state:
                return {
                    "category": Answer(choice=cat, confidence=conf),
                    "flow_type": Answer(choice=flow, confidence=conf),
                }
        raise LLMError("clave fuera del mapa")


def test_load_labels(tmp_path):
    csv_path = tmp_path / "labels.csv"
    csv_path.write_text(
        "key,category,flow_type\nMERCADONA,Alimentacion,expense\n\n"
        "OTRA COSA,Otros,expense\n",
        encoding="utf-8",
    )
    labels = load_labels(csv_path)
    assert [(lab.key, lab.category, lab.flow_type) for lab in labels] == [
        ("MERCADONA", "Alimentacion", "expense"),
        ("OTRA COSA", "Otros", "expense"),
    ]


def test_load_labels_sin_columnas(tmp_path):
    csv_path = tmp_path / "labels.csv"
    csv_path.write_text("key,category\nA,B\n", encoding="utf-8")
    with pytest.raises(ValueError, match="flow_type"):
        load_labels(csv_path)


def test_load_labels_fichero_inexistente(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_labels(tmp_path / "no.csv")


def test_evaluate_mide_aciertos_y_fallos(tmp_path, taxonomy):
    conn = _db(tmp_path, ["MERCADONA", "COSAS RARAS"])
    labels = load_labels_ok(tmp_path, [
        ("MERCADONA", "Alimentacion", "expense"),
        ("COSAS RARAS", "Otros", "expense"),
        ("FANTASMA", "Otros", "expense"),
    ])
    decider = FakeDecider({
        "MERCADONA": ("Alimentacion", "expense", 0.95),
        "COSAS RARAS": ("Alimentacion", "income", 0.6),
    })
    report = evaluate(
        conn, labels, decider, taxonomy, build_questions(taxonomy), max_workers=2
    )
    assert report.total == 3
    assert report.missing == ["FANTASMA"]
    assert report.evaluated == 2
    assert report.category_ok == 1
    assert report.flow_ok == 1
    assert report.category_accuracy() == pytest.approx(0.5)
    assert report.misses == [("COSAS RARAS", "Otros", "Alimentacion", 0.6)]
    # calibración: un acierto en el tramo alto, un fallo en el medio
    assert report.cat_bins["0.9-1.0"] == [1, 1]
    assert report.cat_bins["0.5-0.7"] == [0, 1]
    # evaluar no categoriza: merchants queda vacío
    assert conn.execute("SELECT COUNT(*) FROM merchants").fetchone()[0] == 0


def test_evaluate_ignora_clase_inexistente_y_errores(tmp_path, taxonomy):
    conn = _db(tmp_path, ["MERCADONA", "OTRA COSA"])
    labels = load_labels_ok(tmp_path, [
        ("MERCADONA", "Alimentacion", "expense"),
        ("OTRA COSA", "Otros", "expense"),
    ])
    decider = FakeDecider({
        "MERCADONA": ("Alimentacion", "expense", 0.9),
        # OTRA COSA fuera del mapa -> LLMError
    })
    report = evaluate(
        conn, labels, decider, taxonomy, build_questions(taxonomy), max_workers=2
    )
    assert report.errors == 1
    assert report.evaluated == 1
    assert report.category_accuracy() == pytest.approx(1.0)


def test_evaluate_ignora_choice_fuera_de_taxonomia(tmp_path, taxonomy):
    conn = _db(tmp_path, ["MERCADONA"])
    labels = load_labels_ok(tmp_path, [("MERCADONA", "Alimentacion", "expense")])
    decider = FakeDecider({"MERCADONA": ("Telefonía", "expense", 0.9)})
    report = evaluate(
        conn, labels, decider, taxonomy, build_questions(taxonomy), max_workers=1
    )
    assert report.category_ok == 0
    assert report.misses[0][2] == "—"


def load_labels_ok(tmp_path, triples) -> list:
    csv_path = tmp_path / "labels_ok.csv"
    lineas = ["key,category,flow_type"] + [",".join(t) for t in triples]
    csv_path.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return load_labels(csv_path)


def test_build_state_neutro():
    """El estado no debe mencionar el banco: sesga a Clef hacia comisiones."""
    from moooney.categorize.pipeline import KeyJob

    state = build_state(KeyJob(key="LA TIENDA", n=3, avg_amount=-12.0, gap_days=None))
    assert "LA TIENDA" in state
    assert "banc" not in state.lower()
    assert "-10" in state  # redondeo a la decena
