"""Reintento y segunda opinión sobre dudosas; tipo de operación en el estado. Todo sintético."""

import json

from moooney.categorize.normalize import operation_type
from moooney.categorize.pipeline import KeyJob, build_state, dudosa_jobs
from moooney.categorize.reask import reask_dudosas
from moooney.categorize.second_opinion import parse_opinions, run_second_opinion
from moooney.categorize.taxonomy import load_taxonomy
from moooney.db import repo
from moooney.llm.client import LLMSettings
from moooney.llm.decide import Answer
from moooney.web.queries import split_queue


def test_tipo_de_operacion_por_prefijo():
    assert operation_type("RECIBO DE AGUAS DEL PUEBLO") == "recibo domiciliado"
    assert operation_type("Compra en TIENDA INVENTADA, MADRID") == "compra con tarjeta"
    assert operation_type("TRANSACCION CONTACTLESS EN BAR FICTICIO") == "compra con tarjeta"
    assert operation_type("BIZUM A FAVOR DE ALGUIEN") == "bizum"
    assert operation_type("COMPRA BIZUM COMERCIO") == "bizum"
    assert operation_type("DEVOLUCION RECIBO SEGURO FALSO") == "recibo domiciliado"
    assert operation_type("TRANSFERENCIA DE PARTICULAR") == "transferencia"
    assert operation_type("RETIRADA DE EFECTIVO EN CAJERO") == "retirada de efectivo"
    assert operation_type("ALGO SIN PREFIJO") is None


def test_el_estado_lleva_el_tipo_de_operacion():
    job = KeyJob(key="AGUAS DEL PUEBLO", n=6, avg_amount=-23.0, gap_days=30.0, op_type="recibo domiciliado")
    state = build_state(job)
    assert "Tipo de operacion: recibo domiciliado." in state
    assert "Tipo de operacion" not in build_state(KeyJob(key="X", n=1, avg_amount=-5.0, gap_days=None))


def _db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    rows = [
        ("2026-09-01", "RECIBO DE AGUAS DEL PUEBLO", "AGUAS DEL PUEBLO", -23.0),
        ("2026-10-01", "RECIBO DE AGUAS DEL PUEBLO", "AGUAS DEL PUEBLO", -24.0),
        ("2026-10-03", "COMPRA EN KIOSCO RARO", "KIOSCO RARO", -3.5),
        ("2026-10-04", "COMPRA EN SUPER DE PRUEBA", "SUPER DE PRUEBA", -40.0),
    ]
    with conn:
        for i, (d, c, k, a) in enumerate(rows):
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount, account, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'ES00', '2026-10-11T00:00:00')",
                (f"fp{i}", d, c, k, a),
            )
    now = "2026-10-11T00:00:00"
    repo.upsert_merchant(conn, key="AGUAS DEL PUEBLO", category="Otros", subcategory="otros", flow_type="expense",
                         source="clef", confidence=0.31, updated_at=now)
    repo.upsert_merchant(conn, key="KIOSCO RARO", category="Otros", subcategory="otros", flow_type="expense",
                         source="clef", confidence=0.2, updated_at=now)
    repo.upsert_merchant(conn, key="SUPER DE PRUEBA", category="Compras", subcategory=None, flow_type="expense",
                         source="user", confidence=1.0, updated_at=now)
    return conn


class _Decider:
    def __init__(self, by_key):
        self.by_key = by_key
        self.states = []

    def decide(self, state, questions):
        self.states.append(state)
        for k, (cat, conf) in self.by_key.items():
            if f"Concepto de un apunte: {k}." in state:
                return {"category": Answer(choice=cat, confidence=conf), "flow_type": Answer(choice="expense", confidence=1)}
        return {"category": Answer(choice="Otros", confidence=0.1)}


def test_dudosas_ordenadas_por_euros_y_reintento_solo_si_mejora(tmp_path):
    conn = _db(tmp_path)
    tx = load_taxonomy()
    jobs = dudosa_jobs(conn)
    assert [j.key for j in jobs] == ["AGUAS DEL PUEBLO", "KIOSCO RARO"]  # la de usuario no entra
    assert jobs[0].op_type == "recibo domiciliado"
    dec = _Decider({"AGUAS DEL PUEBLO": ("Vivienda", 0.84), "KIOSCO RARO": ("Compras", 0.15)})
    rs = reask_dudosas(conn, dec, tx, {}, max_workers=2)
    assert rs.examined == 2 and rs.improved == 1 and rs.changed == 1 and rs.failed == 0
    assert any("Tipo de operacion: recibo domiciliado" in s for s in dec.states)
    m = repo.get_merchant(conn, "AGUAS DEL PUEBLO")
    assert m["category"] == "Vivienda" and m["source"] == "clef" and m["confidence"] == 0.84
    assert conn.execute("SELECT category FROM transactions WHERE concept_clean='AGUAS DEL PUEBLO'").fetchone()[0] == "Vivienda"
    assert repo.get_merchant(conn, "KIOSCO RARO")["confidence"] == 0.2  # no mejoró: se queda
    assert repo.get_merchant(conn, "SUPER DE PRUEBA")["source"] == "user"


class _Client:
    def __init__(self, answers):
        self.answers = answers
        self.calls = []

    def chat(self, **kw):
        self.calls.append(kw)
        return json.dumps({"answers": self.answers})


def test_segunda_opinion_por_lotes_respeta_usuario_y_umbral(tmp_path):
    conn = _db(tmp_path)
    tx = load_taxonomy()
    llm = LLMSettings(base_url="https://test.local/v1", models={"clef": "c", "redaccion": "q", "embeddings": "e"})
    client = _Client([
        {"key": "AGUAS DEL PUEBLO", "category": "Vivienda", "confidence": 0.9, "why": "suministro de agua"},
        {"key": "KIOSCO RARO", "category": "Compras", "confidence": 0.4, "why": "no está claro"},
        {"key": "SUPER DE PRUEBA", "category": "Alimentacion", "confidence": 0.99, "why": "supermercado"},
    ])
    ss = run_second_opinion(conn, client, llm, tx)
    assert ss.examined == 2 and ss.changed == 1 and ss.kept == 1 and ss.failed == 0
    assert len(client.calls) == 1 and client.calls[0]["purpose"] == "second_opinion"
    body = json.loads(client.calls[0]["messages"][1]["content"])
    assert [c["key"] for c in body["claves"]] == ["AGUAS DEL PUEBLO", "KIOSCO RARO"]
    assert body["claves"][0]["tipo_operacion"] == "recibo domiciliado" and "categorias" in body
    assert "SUPER DE PRUEBA" not in client.calls[0]["messages"][1]["content"]
    m = repo.get_merchant(conn, "AGUAS DEL PUEBLO")
    assert m["category"] == "Vivienda" and m["source"] == "llm" and m["confidence"] == 0.9
    assert repo.get_merchant(conn, "KIOSCO RARO")["category"] == "Otros"  # 0.4 < umbral
    assert repo.get_merchant(conn, "SUPER DE PRUEBA")["category"] == "Compras"  # user manda siempre
    assert dudosa_jobs(conn) and dudosa_jobs(conn)[0].key == "KIOSCO RARO"


def test_parse_opinions_tolerante():
    tx = load_taxonomy()
    raw = "```json\n{\"answers\": [{\"key\": \"A\", \"category\": \"Ocio\", \"confidence\": \"0.8\"},"
    raw += " {\"key\": \"B\", \"category\": \"NoExiste\", \"confidence\": 1}, 3]}\n```"
    ops = parse_opinions(raw, tx)
    assert len(ops) == 1 and ops[0].key == "A" and ops[0].confidence == 0.8


def test_cola_separa_las_que_importan():
    rows = [
        {"key": "A", "n": 1, "total": -12.0}, {"key": "B", "n": 3, "total": -9.0},
        {"key": "C", "n": 1, "total": -80.0},
    ]
    importantes, resto = split_queue(rows)
    assert [r["key"] for r in importantes] == ["B", "C"] and [r["key"] for r in resto] == ["A"]
