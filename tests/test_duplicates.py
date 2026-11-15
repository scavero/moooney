"""Posibles duplicados: candidatos por código, veredicto del decider. Movimientos inventados."""

from pathlib import Path

from fastapi.testclient import TestClient

from moooney.analytics import duplicates
from moooney.db import repo
from moooney.demo import build_demo
from moooney.llm.anonymize import assert_safe_state
from moooney.llm.decide import Answer
from moooney.web.app import create_app

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"
IBAN = "ES9121000418450200051332"


def _add(conn, rows):
    for i, (d, concept, key, amount, account) in enumerate(rows):
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount, account, flow_type, category,"
            " created_at) VALUES (?, ?, ?, ?, ?, ?, 'expense', 'Otros', 't')",
            (f"f{i}-{d}-{key}-{account}", d, concept, key, amount, account))
    conn.commit()


ROWS = [
    ("2025-03-10", f"RECIBO SEGUROS FALSOS REF {IBAN}", "SEGUROS FALSOS", -120.0, "c1"),
    ("2025-03-11", "RECIBO SEGUROS FALSOS", "SEGUROS FALSOS", -120.0, "c1"),      # grupo 1: a un día
    ("2025-03-10", "COMPRA EN CAFE FALSO", "CAFE FALSO", -1.5, "c1"),
    ("2025-03-10", "COMPRA EN CAFE FALSO", "CAFE FALSO", -1.5, "c1"),              # grupo 2: mismo día
    ("2025-03-13", "COMPRA EN CAFE FALSO", "CAFE FALSO", -1.5, "c1"),              # …y a 3 días: mismo grupo
    ("2025-03-20", "COMPRA EN CAFE FALSO", "CAFE FALSO", -1.5, "c1"),              # a 7 días: fuera
    ("2025-03-10", "COMPRA EN TIENDA FALSA", "TIENDA FALSA", -30.0, "c1"),
    ("2025-03-10", "COMPRA EN TIENDA FALSA", "TIENDA FALSA", -30.0, "c2"),         # otra cuenta: no es grupo
    ("2025-03-10", "COMPRA EN TIENDA FALSA", "TIENDA FALSA", -31.0, "c1"),         # otro importe: no es grupo
]


class FakeDecider:
    def __init__(self):
        self.states = []

    def decide(self, state, questions):
        self.states.append(state)
        assert questions == duplicates.QUESTION
        return {"tipo": Answer(choice="periodico" if "SEGUROS" in state else "cotidiano", confidence=0.9)}


def test_candidatos_veredicto_y_descarte(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    _add(conn, ROWS)
    groups = {g.key: g for g in duplicates.candidates(conn)}
    assert set(groups) == {"SEGUROS FALSOS", "CAFE FALSO"}
    assert len(groups["CAFE FALSO"].ids) == 3 and groups["CAFE FALSO"].span_days == 3
    assert groups["SEGUROS FALSOS"].span_days == 1 and groups["SEGUROS FALSOS"].verdict is None

    d = FakeDecider()
    assert duplicates.review(conn, d, now="t") == (2, 1, 0)
    for s in d.states:  # al modelo no va el concepto original (aquí lleva un IBAN) ni la cuenta
        assert_safe_state(s)
        assert IBAN not in s and "RECIBO SEGUROS" not in s and "c1" not in s.split()
    seguros = next(s for s in d.states if "SEGUROS" in s)
    assert "2 cargos de 120.00 EUR en dias distintos, 1 dias" in seguros and "Tipo de operacion: recibo domiciliado" in seguros
    groups = {g.key: g for g in duplicates.candidates(conn)}
    assert (groups["SEGUROS FALSOS"].verdict, groups["SEGUROS FALSOS"].source) == ("duplicado", "clef")
    assert groups["CAFE FALSO"].verdict == "normal"
    assert duplicates.review(conn, d, now="t") == (0, 0, 0) and len(d.states) == 2  # no se vuelve a preguntar

    # el usuario descarta el grupo; un id inventado no existe
    assert duplicates.dismiss(conn, groups["SEGUROS FALSOS"].gid, now="t") and not duplicates.dismiss(conn, "1,999", now="t")
    g = next(g for g in duplicates.candidates(conn) if g.key == "SEGUROS FALSOS")
    assert (g.verdict, g.source) == ("normal", "user")
    # apartar uno de los dos deshace el grupo
    repo.remove_transaction(conn, g.ids[1], "duplicado", now="t")
    assert {g.key for g in duplicates.candidates(conn)} == {"CAFE FALSO"}


def test_un_fallo_del_modelo_no_guarda_nada(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    _add(conn, ROWS[:2])

    class Roto:
        def decide(self, state, questions):
            return {"tipo": Answer(choice="cualquier cosa", confidence=0.5)}

    assert duplicates.review(conn, Roto(), now="t") == (1, 0, 1)
    assert duplicates.candidates(conn)[0].verdict is None
    # una compra ocasional repetida solo es probable duplicado si es del mismo día
    assert [duplicates.verdict_for("ocasional", d) for d in (0, 2)] == ["duplicado", "dudoso"]


def test_pagina_de_duplicados(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=3)
    conn = repo.connect(tmp_path / "demo" / "moooney.db")
    conn.execute("DELETE FROM transactions")
    _add(conn, ROWS[:2])
    gid = duplicates.candidates(conn)[0].gid
    conn.close()
    with TestClient(create_app(settings_path)) as c:
        assert 'href="/transactions/duplicates"' in c.get("/transactions").text
        page = c.get("/transactions/duplicates").text
        assert "SEGUROS FALSOS" in page and "sin revisar" in page and "Revisar con IA (1)" in page
        assert c.post("/transactions/duplicates/dismiss", data={"ids": "7,8"}).status_code == 404
        r = c.post("/transactions/duplicates/dismiss", data={"ids": gid}, follow_redirects=False)
        assert r.status_code == 303
        assert "descartado por ti" in c.get("/transactions/duplicates").text
        assert 'href="/transactions/duplicates"' not in c.get("/transactions").text  # ya no avisa
        # el trabajo sin modelo (offline) no rompe
        assert c.post("/pipeline/jobs/review-duplicates").status_code == 200
