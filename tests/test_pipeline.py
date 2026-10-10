"""Cascada de categorización con deciders y clientes falsos: sin red ni datos reales."""

import sqlite3

import numpy as np
import pytest
import yaml

from moooney.categorize import embed
from moooney.categorize.pipeline import (
    KeyJob,
    apply_decider,
    apply_embeddings,
    apply_rules,
    build_questions,
    build_state,
    pending_jobs,
)
from moooney.categorize.rules import Rule
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
        "Otros": {
            "flow_type": "expense",
            "description": "Lo que no encaja",
            "subcategories": ["otros"],
        },
        "Ingresos": {"flow_type": "income", "description": "Dinero que entra"},
    },
    "flow_types": {
        "expense": "Gasto",
        "income": "Ingreso",
        "transfer_internal": "Traspaso propio",
    },
}


@pytest.fixture()
def taxonomy(tmp_path):
    path = tmp_path / "categories.yaml"
    path.write_text(yaml.safe_dump(MINI_CATEGORIES, allow_unicode=True), encoding="utf-8")
    return load_taxonomy(path)


def _db(tmp_path, rows) -> sqlite3.Connection:
    """rows: lista de (concept_clean, date, amount)."""
    conn = repo.connect(tmp_path / "test.db")
    with conn:
        for i, (key, day, amount) in enumerate(rows):
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, concept, concept_clean,"
                " amount, account, created_at) VALUES (?, ?, 'X', ?, ?, 'principal',"
                " '2025-01-01T00:00:00')",
                (f"f{i}", day, key, amount),
            )
    return conn


REGLAS = [
    Rule(pattern=r"^MERCADONA", category="Alimentacion", subcategory="supermercado", flow_type="expense"),
]


def test_pending_jobs_agrega_y_calcula_intervalo(tmp_path):
    conn = _db(
        tmp_path,
        [("MERCADONA", "2025-01-01", -10.0), ("MERCADONA", "2025-03-01", -20.0),
         ("LOCALES RAROS", "2025-02-01", -5.0)],
    )
    jobs = pending_jobs(conn)
    by_key = {j.key: j for j in jobs}
    assert set(by_key) == {"MERCADONA", "LOCALES RAROS"}
    assert by_key["MERCADONA"].n == 2
    assert by_key["MERCADONA"].avg_amount == pytest.approx(-15.0)
    assert by_key["MERCADONA"].gap_days == pytest.approx(59.0)
    assert by_key["LOCALES RAROS"].gap_days is None
    # una clave ya categorizada deja de estar pendiente
    repo.upsert_merchant(
        conn, key="LOCALES RAROS", category="Otros", subcategory="otros",
        flow_type="expense", source="clef", confidence=0.5, updated_at="2025-01-01T00:00:00",
    )
    assert [j.key for j in pending_jobs(conn)] == ["MERCADONA"]


def test_build_state_redondea_a_decenas_sin_digitos_largos():
    job = KeyJob(key="FROIZ", n=12, avg_amount=-42.37, gap_days=5.4)
    state = build_state(job)
    assert "FROIZ" in state
    assert "-40" in state and "42" not in state
    assert "gasto" in state and "12 apariciones" in state and "5 dias" in state
    assert build_state(KeyJob(key="X", n=1, avg_amount=25.0, gap_days=None)).count("intervalo") == 0


def test_reglas_resuelven_y_dejan_restantes(tmp_path):
    conn = _db(tmp_path, [("MERCADONA NANTE", "2025-01-01", -10.0),
                          ("COSAS DE TODO TIPO", "2025-01-02", -30.0)])
    jobs = pending_jobs(conn)
    n, restantes = apply_rules(conn, jobs, REGLAS)
    assert n == 1
    assert [j.key for j in restantes] == ["COSAS DE TODO TIPO"]
    row = conn.execute(
        "SELECT category FROM transactions WHERE concept_clean = 'MERCADONA NANTE'"
    ).fetchone()
    assert row["category"] == "Alimentacion"


def test_reglas_dry_run_no_escribe(tmp_path):
    conn = _db(tmp_path, [("MERCADONA NANTE", "2025-01-01", -10.0)])
    n, restantes = apply_rules(conn, pending_jobs(conn), REGLAS, dry_run=True)
    assert n == 1 and restantes == []
    assert conn.execute("SELECT COUNT(*) AS n FROM merchants").fetchone()["n"] == 0
    assert conn.execute("SELECT category FROM transactions").fetchone()["category"] is None


class FakeDecider:
    """Devuelve respuestas fijas; explota si el estado trae la clave maldita."""

    def __init__(self, answers_by_key, boom=()):
        self.answers_by_key = answers_by_key
        self.boom = boom
        self.states: list[str] = []

    def decide(self, state, questions):
        self.states.append(state)
        if any(k in state for k in self.boom):
            raise LLMError("el modelo se ha caido")
        for key, answers in self.answers_by_key.items():
            if f"Concepto normalizado: {key}." in state:
                return answers
        return {}


def test_decider_guarda_y_propaga(tmp_path, taxonomy):
    conn = _db(tmp_path, [("GUARNICION S L", "2025-01-01", -50.0),
                          ("MISTERIOS VARIA", "2025-02-01", 30.0),
                          ("CAIDA CONSTANTE", "2025-03-01", -5.0)])
    decider = FakeDecider(
        {
            "GUARNICION S L": {
                "category": Answer(choice="Alimentacion", confidence=0.8),
                "flow_type": Answer(choice="expense", confidence=0.9),
            },
            "MISTERIOS VARIA": {"category": Answer(choice="Ingresos", confidence=0.6)},
        },
        boom=["CAIDA CONSTANTE"],
    )
    n, fallidas = apply_decider(
        conn, pending_jobs(conn), decider, taxonomy, build_questions(taxonomy), max_workers=4
    )
    assert (n, fallidas) == (2, 1)
    merc = {r["key"]: r for r in conn.execute("SELECT key, source, confidence FROM merchants")}
    assert merc["GUARNICION S L"]["source"] == "clef"
    assert merc["GUARNICION S L"]["confidence"] == pytest.approx(0.8)
    tx = conn.execute(
        "SELECT flow_type FROM transactions WHERE concept_clean = 'MISTERIOS VARIA'"
    ).fetchone()
    # sin respuesta de flow_type, se usa el flow por defecto de la categoría
    assert tx["flow_type"] == "income"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM merchants WHERE key = 'CAIDA CONSTANTE'"
    ).fetchone()["n"] == 0


def test_decider_categoria_inventada_no_se_guarda(tmp_path, taxonomy):
    conn = _db(tmp_path, [("COMERCIO UNO", "2025-01-01", -50.0)])
    decider = FakeDecider(
        {"COMERCIO UNO": {"category": Answer(choice="Videojuegos", confidence=0.9)}}
    )
    n, fallidas = apply_decider(conn, pending_jobs(conn), decider, taxonomy, build_questions(taxonomy))
    assert (n, fallidas) == (0, 1)
    assert conn.execute("SELECT COUNT(*) AS n FROM merchants").fetchone()["n"] == 0


def test_decider_dry_run_no_escribe(tmp_path, taxonomy):
    conn = _db(tmp_path, [("COMERCIO UNO", "2025-01-01", -50.0)])
    decider = FakeDecider(
        {"COMERCIO UNO": {"category": Answer(choice="Otros", confidence=0.4)}}
    )
    n, _ = apply_decider(
        conn, pending_jobs(conn), decider, taxonomy, build_questions(taxonomy), dry_run=True
    )
    assert n == 1
    assert conn.execute("SELECT COUNT(*) AS n FROM merchants").fetchone()["n"] == 0


class FakeEmbedClient:
    """Vector fijo por clave en dimensiones 2."""

    def __init__(self, vectors):
        self.vectors = vectors
        self.calls = 0

    def embed(self, *, model, texts, purpose):
        self.calls += 1
        return [self.vectors[t] for t in texts]


def test_embedding_hereda_de_clave_reglada(tmp_path):
    conn = _db(tmp_path, [("FROIZ PORRINO", "2025-01-01", -10.0),
                          ("FROIZ PORRINO I", "2025-01-02", -8.0),
                          ("OTRA COSA", "2025-01-03", -7.0)])
    repo.upsert_merchant(
        conn, key="FROIZ PORRINO", category="Alimentacion", subcategory="supermercado",
        flow_type="expense", source="rule", confidence=1.0, updated_at="2025-01-01T00:00:00",
    )
    client = FakeEmbedClient({
        "FROIZ PORRINO": [1.0, 0.0],
        "FROIZ PORRINO I": [0.99, 0.01],  # coseno ~0.9995 > 0.92
        "OTRA COSA": [0.0, 1.0],
    })
    embed.store_embedding(
        conn, key="FROIZ PORRINO", model="bge-m3",
        vector=np.array([1.0, 0.0], dtype=np.float32), updated_at="2025-01-01T00:00:00",
    )
    jobs = [j for j in pending_jobs(conn) if j.key in ("FROIZ PORRINO I", "OTRA COSA")]
    n, restantes = apply_embeddings(conn, jobs, client, "bge-m3")
    assert n == 1
    assert [j.key for j in restantes] == ["OTRA COSA"]
    m = repo.get_merchant(conn, "FROIZ PORRINO I")
    assert (m["source"], m["category"]) == ("embedding", "Alimentacion")
    assert m["confidence"] > 0.92
    # el vector de la nueva clave queda guardado
    assert embed.get_embedding(conn, "FROIZ PORRINO I", "bge-m3") is not None


def test_embedding_sin_cliente_no_toca_nada(tmp_path):
    conn = _db(tmp_path, [("OTRA COSA", "2025-01-03", -7.0)])
    jobs = pending_jobs(conn)
    n, restantes = apply_embeddings(conn, jobs, None, "bge-m3")
    assert n == 0 and restantes == jobs


def test_cosine_y_blob_roundtrip(tmp_path):
    assert embed.cosine(np.array([2.0, 0.0]), np.array([5.0, 0.0])) == pytest.approx(1.0)
    assert embed.cosine(np.array([1.0, 0.0]), np.array([0.0, 3.0])) == pytest.approx(0.0)
    assert embed.cosine(np.zeros(3), np.zeros(3)) == 0.0
    conn = repo.connect(tmp_path / "emb.db")
    v = np.array([0.5, -1.25, 3.0], dtype=np.float32)
    embed.store_embedding(conn, key="K", model="bge-m3", vector=v, updated_at="t")
    assert np.allclose(embed.get_embedding(conn, "K", "bge-m3"), v)
    assert embed.get_embedding(conn, "K", "otro-modelo") is None
    # upsert del vector
    embed.store_embedding(conn, key="K", model="bge-m3", vector=np.ones(3), updated_at="t2")
    assert embed.get_embedding(conn, "K", "bge-m3")[0] == 1.0


def test_find_similar_solo_usa_user_y_rule(tmp_path):
    conn = repo.connect(tmp_path / "emb.db")
    for source in ("user", "rule", "clef"):
        repo.upsert_merchant(
            conn, key=f"M-{source}", category="Otros", subcategory="otros",
            flow_type="expense", source=source, confidence=1.0, updated_at="t",
        )
        embed.store_embedding(
            conn, key=f"M-{source}", model="bge-m3",
            vector=np.array([1.0, 0.0]), updated_at="t",
        )
    hit = embed.find_similar_labeled(conn, np.array([1.0, 0.1]), model="bge-m3")
    assert hit is not None
    assert hit[0]["key"] in ("M-user", "M-rule")
    # por debajo del umbral no hay hit
    assert embed.find_similar_labeled(conn, np.array([0.0, 1.0]), model="bge-m3") is None
