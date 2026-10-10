"""Tests de ingesta: huellas, CSV limpio, idempotencia y solapamiento."""

import hashlib
import sqlite3
from pathlib import Path

from moooney.db import repo
from moooney.ingest.load import fingerprint, ingest_directory, ingest_file
from tests.conftest import FAKE_ROWS, make_santander_xlsx, make_settings


def test_huella_estable() -> None:
    fp = fingerprint("****0123", "2025-01-10", -54.20, "MERCADONA FALSA 001", 1000.0)
    esperado = hashlib.sha1(b"****0123|2025-01-10|-54.20|MERCADONA FALSA 001|1000.00").hexdigest()
    assert fp == esperado
    assert fp == fingerprint("****0123", "2025-01-10", -54.2, "MERCADONA FALSA 001", 1000.0)
    # el saldo desambigua movimientos identicos el mismo dia
    assert fp != fingerprint("****0123", "2025-01-10", -54.20, "MERCADONA FALSA 001", 999.0)


def _db(settings) -> sqlite3.Connection:
    return repo.connect(settings.paths.db_path)


def test_ingest_idempotente(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.paths.excel_dir.mkdir(parents=True, exist_ok=True)
    make_santander_xlsx(settings.paths.excel_dir / "extracto.xlsx")
    conn = _db(settings)

    primera = ingest_directory(settings.paths.excel_dir, conn, settings)
    assert len(primera) == 1
    assert primera[0].rows_total == len(FAKE_ROWS)
    assert primera[0].rows_new == len(FAKE_ROWS)

    segunda = ingest_directory(settings.paths.excel_dir, conn, settings)
    assert segunda[0].skipped is True
    assert segunda[0].rows_new == 0

    n = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert n == len(FAKE_ROWS)
    conn.close()


def test_mismo_contenido_distinto_fichero_no_duplica(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.paths.excel_dir.mkdir(parents=True, exist_ok=True)
    excel = settings.paths.excel_dir
    make_santander_xlsx(excel / "uno.xlsx")
    conn = _db(settings)
    ingest_directory(excel, conn, settings)

    # mismo contenido de movimientos pero exportado con otra hoja: hash nuevo,
    # y aun asi 0 filas nuevas gracias a las huellas
    make_santander_xlsx(excel / "copia-de-otro-nombre.xlsx", sheet="Otra hoja")
    outcomes = ingest_directory(excel, conn, settings)
    outcome = next(o for o in outcomes if not o.skipped)
    assert outcome.skipped is False
    assert outcome.rows_new == 0
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == len(FAKE_ROWS)
    conn.close()


def test_solapamiento_parcial(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.paths.excel_dir.mkdir(parents=True, exist_ok=True)
    excel = settings.paths.excel_dir
    make_santander_xlsx(excel / "ano_anterior.xlsx", rows=FAKE_ROWS[:2])
    conn = _db(settings)
    ingest_directory(excel, conn, settings)

    # segundo extracto que solapa: repite la nomina y la ultima fila del anterior
    # y anade una nueva
    filas_b = [
        FAKE_ROWS[1],
        FAKE_ROWS[2],
        ("28/03/2025", "29/03/2025", "PAGO FALSO FINAL", "-9,99", "3.486,51", "EUR"),
    ]
    make_santander_xlsx(excel / "ano_actual.xlsx", rows=filas_b)
    outcomes = ingest_directory(excel, conn, settings)
    # primer fichero tiene filas 0 y 1; el segundo repite la 1, trae la 2 y una nueva
    nuevo = [o for o in outcomes if not o.skipped]
    assert len(nuevo) == 1
    assert nuevo[0].rows_total == 3
    assert nuevo[0].rows_new == 2
    total = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    assert total == 4
    conn.close()


def test_csv_limpio_y_registro(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    settings.paths.excel_dir.mkdir(parents=True, exist_ok=True)
    make_santander_xlsx(settings.paths.excel_dir / "extracto.xlsx")
    conn = _db(settings)
    outcome = ingest_file(settings.paths.excel_dir / "extracto.xlsx", conn, settings)

    assert outcome.csv_path is not None and outcome.csv_path.exists()
    lineas = outcome.csv_path.read_text(encoding="utf-8").splitlines()
    assert lineas[0] == "date,value_date,concept,amount,balance,account"
    assert "-54.20" in lineas[1]
    assert lineas[1].startswith("2025-01-10,2025-01-11,MERCADONA FALSA 001,")

    imp = conn.execute("SELECT * FROM imports").fetchone()
    assert imp is not None
    row = conn.execute("SELECT date, value_date, amount, account FROM transactions LIMIT 1").fetchone()
    assert row == ("2025-01-10", "2025-01-11", -54.20, "****0123")

    st = repo.get_status(conn)
    assert st["transactions"] == len(FAKE_ROWS)
    assert st["date_min"] == "2025-01-10"
    assert st["date_max"] == "2025-02-15"
    assert st["imports"] == 1
    conn.close()
