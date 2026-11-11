"""Gastos compartidos. Movimientos inventados."""

import pytest

from moooney.analytics import shared
from moooney.analytics.metrics import monthly_summary
from moooney.db import repo
from tests.test_analytics import _tx


@pytest.fixture()
def db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    _tx(conn, [
        ("2025-03-01", "NOMINA", 2000.0, "Ingresos", "income"),
        ("2025-03-10", "RESTAURANTE FALSO", -80.0, "Alimentacion", "expense"),
        ("2025-03-11", "BIZUM RECIBIDO ANA", 20.0, "Transferencias", "income"),
        ("2025-03-11", "BIZUM RECIBIDO LUIS", 20.0, "Transferencias", "income"),
        ("2025-03-12", "SUPER FALSO", -35.0, "Alimentacion", "expense"),
        ("2025-03-15", "BIZUM RECIBIDO EVA", 15.0, "Transferencias", "income"),
    ])
    for k, cat, flow in [("RESTAURANTE FALSO", "Alimentacion", "expense"), ("BIZUM RECIBIDO ANA", "Transferencias", "income")]:
        repo.upsert_merchant(conn, key=k, category=cat, subcategory=None, flow_type=flow, source="rule",
                             confidence=1.0, updated_at="t")
    return conn


def _id(conn, key):
    return conn.execute("SELECT id FROM transactions WHERE concept_clean = ?", (key,)).fetchone()[0]


def test_propone_el_reparto_a_partes_iguales_primero(db):
    sug = {s.income["concept_clean"]: s for s in shared.suggestions(db)}
    ana = sug["BIZUM RECIBIDO ANA"].candidates
    assert ana[0].expense["concept_clean"] == "RESTAURANTE FALSO" and ana[0].parts == 4
    eva = sug["BIZUM RECIBIDO EVA"].candidates  # 15 € no cuadra a partes iguales: solo la última semana
    assert {c.expense["concept_clean"] for c in eva} == {"RESTAURANTE FALSO", "SUPER FALSO"}
    _tx(db, [("2025-03-30", "BIZUM RECIBIDO LEJOS", 15.0, "Transferencias", "income"),
             ("2025-03-29", "BIZUM ENVIADO PEPE", -50.0, "Transferencias", "expense")])
    nombres = {s.income["concept_clean"] for s in shared.suggestions(db)}
    assert "BIZUM RECIBIDO LEJOS" not in nombres  # un bizum enviado no es un gasto compartido


def test_emparejar_deja_el_gasto_neto_y_no_infla_ingresos(db):
    antes = monthly_summary(db, "2025-03")
    shared.link(db, _id(db, "BIZUM RECIBIDO ANA"), _id(db, "RESTAURANTE FALSO"))
    shared.link(db, _id(db, "BIZUM RECIBIDO LUIS"), _id(db, "RESTAURANTE FALSO"))
    despues = monthly_summary(db, "2025-03")
    assert despues.expenses == pytest.approx(antes.expenses - 40)
    assert despues.income == pytest.approx(antes.income - 40)
    # la memoria de comercios vuelve a escribir los movimientos: el vínculo se mantiene
    repo.propagate_to_transactions(db, key="BIZUM RECIBIDO ANA", category="Transferencias", subcategory=None,
                                   flow_type="income", confidence=1.0)
    assert monthly_summary(db, "2025-03").expenses == pytest.approx(despues.expenses)
    # ya no se propone, y del restaurante quedan 40 € por devolver
    restantes = {s.income["concept_clean"] for s in shared.suggestions(db)}
    assert restantes == {"BIZUM RECIBIDO EVA"}
    assert len(shared.linked(db)) == 2


def test_deshacer_y_descartar(db):
    ana = _id(db, "BIZUM RECIBIDO ANA")
    shared.link(db, ana, _id(db, "RESTAURANTE FALSO"))
    shared.unlink(db, ana)
    row = db.execute("SELECT flow_type, category FROM transactions WHERE id = ?", (ana,)).fetchone()
    assert tuple(row) == ("income", "Transferencias")
    shared.dismiss(db, ana)
    assert "BIZUM RECIBIDO ANA" not in {s.income["concept_clean"] for s in shared.suggestions(db)}
    with pytest.raises(ValueError):
        shared.link(db, _id(db, "RESTAURANTE FALSO"), ana)  # al revés


def test_pagina(tmp_path, monkeypatch):
    from pathlib import Path

    from fastapi.testclient import TestClient

    from moooney.config import load_settings
    from moooney.demo import build_demo
    from moooney.web.app import create_app

    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=Path(__file__).resolve().parent.parent / "config",
                               months=4)
    conn = repo.connect(load_settings(settings_path).paths.db_path)
    _tx(conn, [("2025-03-10", "CENA INVENTADA", -90.0, "Alimentacion", "expense"),
               ("2025-03-11", "BIZUM RECIBIDO INVENTADO", 30.0, "Transferencias", "income")])
    inc, exp = _id(conn, "BIZUM RECIBIDO INVENTADO"), _id(conn, "CENA INVENTADA")
    conn.close()
    with TestClient(create_app(settings_path)) as c:
        r = c.get("/shared")
        assert r.status_code == 200 and "CENA INVENTADA" in r.text and "entre 3" in r.text
        r = c.post("/shared/link", data={"income_id": inc, "expense_id": exp})
        assert r.status_code == 200 and "Emparejados" in r.text
        r = c.post("/shared/unlink", data={"income_id": inc})
        assert "Emparejados" not in r.text
