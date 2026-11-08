"""Memoria de comercios: precedencia de fuentes y propagación a transacciones."""

import sqlite3

from moooney.db import repo


def _db(tmp_path) -> sqlite3.Connection:
    conn = repo.connect(tmp_path / "test.db")
    with conn:
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount,"
            " account, created_at) VALUES ('f1', '2025-01-05', 'COMPRA X, VIGO, TARJ. :*1',"
            " 'TIENDA PRUEBA', -12.5, 'principal', '2025-01-05T00:00:00')"
        )
    return conn


def test_regla_crea_merchant_y_propaga(tmp_path):
    conn = _db(tmp_path)
    ok = repo.upsert_merchant(
        conn,
        key="TIENDA PRUEBA",
        category="Compras",
        subcategory="online",
        flow_type="expense",
        source="rule",
        confidence=1.0,
        updated_at="2025-01-05T00:00:00",
    )
    assert ok
    n = repo.propagate_to_transactions(
        conn,
        key="TIENDA PRUEBA",
        category="Compras",
        subcategory="online",
        flow_type="expense",
        confidence=1.0,
    )
    assert n == 1
    row = conn.execute("SELECT category, flow_type FROM transactions").fetchone()
    assert (row["category"], row["flow_type"]) == ("Compras", "expense")


def test_user_nunca_lo_pisa_ninguna_fuente(tmp_path):
    conn = _db(tmp_path)
    for source, conf in (("clef", 0.6), ("user", 1.0), ("rule", 1.0)):
        repo.upsert_merchant(
            conn,
            key="TIENDA PRUEBA",
            category="Otros",
            subcategory="otros",
            flow_type="expense",
            source=source,
            confidence=conf,
            updated_at="2025-01-05T00:00:00",
        )
    merchant = repo.get_merchant(conn, "TIENDA PRUEBA")
    assert merchant["source"] == "user"


def test_regla_si_pisa_a_clef(tmp_path):
    conn = _db(tmp_path)
    repo.upsert_merchant(
        conn,
        key="TIENDA PRUEBA",
        category="Otros",
        subcategory="otros",
        flow_type="expense",
        source="clef",
        confidence=0.5,
        updated_at="2025-01-05T00:00:00",
    )
    ok = repo.upsert_merchant(
        conn,
        key="TIENDA PRUEBA",
        category="Compras",
        subcategory="online",
        flow_type="expense",
        source="rule",
        confidence=1.0,
        updated_at="2025-01-06T00:00:00",
    )
    assert ok
    assert repo.get_merchant(conn, "TIENDA PRUEBA")["source"] == "rule"


def test_clef_no_pisa_a_regla(tmp_path):
    conn = _db(tmp_path)
    repo.upsert_merchant(
        conn,
        key="TIENDA PRUEBA",
        category="Compras",
        subcategory="online",
        flow_type="expense",
        source="rule",
        confidence=1.0,
        updated_at="2025-01-05T00:00:00",
    )
    ok = repo.upsert_merchant(
        conn,
        key="TIENDA PRUEBA",
        category="Otros",
        subcategory="otros",
        flow_type="expense",
        source="clef",
        confidence=0.9,
        updated_at="2025-01-06T00:00:00",
    )
    assert not ok
    assert repo.get_merchant(conn, "TIENDA PRUEBA")["category"] == "Compras"


def test_rebuild_clean_keys_migra_la_memoria(tmp_path):
    from moooney.categorize.keys import rebuild_clean_keys
    from moooney.db import repo

    conn = repo.connect(tmp_path / "t.db")
    with conn:
        # clave antigua calculada con una normalizacion previa (prefijo sin quitar)
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount, account,"
            " created_at) VALUES ('f1', '2025-01-01', 'TRANSACCION CONTACTLESS EN TASCA DO RIO',"
            " 'TRANSACCION CONTACTLESS EN TASCA DO RIO', -5, 'p', 't')"
        )
        conn.execute(
            "INSERT INTO merchants (key, category, subcategory, flow_type, source, confidence,"
            " updated_at) VALUES ('TRANSACCION CONTACTLESS EN TASCA DO RIO', 'Alimentacion',"
            " 'restaurantes', 'expense', 'user', 1.0, 't')"
        )
    cambiadas, migradas = rebuild_clean_keys(conn)
    assert cambiadas == 1 and migradas == 1
    row = conn.execute("SELECT concept_clean, category FROM transactions").fetchone()
    assert row["concept_clean"] == "TASCA DO RIO" and row["category"] == "Alimentacion"
    m = repo.get_merchant(conn, "TASCA DO RIO")
    assert m["source"] == "user" and m["category"] == "Alimentacion"
    assert rebuild_clean_keys(conn) == (0, 0)  # idempotente
    # la clave antigua ya no la usa nadie: desaparece de la memoria
    assert repo.get_merchant(conn, 'TRANSACCION CONTACTLESS EN TASCA DO RIO') is None


def test_movimiento_nuevo_de_comercio_conocido_hereda_la_categoria(tmp_path):
    from moooney.categorize.keys import backfill_clean_keys

    conn = _db(tmp_path)
    repo.upsert_merchant(conn, key="TIENDA PRUEBA", category="Compras", subcategory="online",
                         flow_type="expense", source="rule", confidence=1.0, updated_at="t")
    # llega otro extracto con una compra más en la misma tienda, aún sin clave ni categoría
    with conn:
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, amount, account, created_at)"
            " VALUES ('f2', '2025-02-05', 'TIENDA PRUEBA', -7, 'principal', 't')"
        )
    assert backfill_clean_keys(conn) == 1
    row = conn.execute("SELECT concept_clean, category, flow_type FROM transactions WHERE fingerprint = 'f2'").fetchone()
    assert (row["concept_clean"], row["category"], row["flow_type"]) == ("TIENDA PRUEBA", "Compras", "expense")
