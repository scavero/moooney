"""Extractos que se solapan y extractos de tarjeta. Todo inventado, generado en tmp_path."""

from pathlib import Path

import pytest
from openpyxl import Workbook

from moooney.db import repo
from moooney.ingest.load import (
    find_overlap_duplicates,
    ingest_directory,
    recover_missing,
    remove_overlap_duplicates,
)
from moooney.ingest.parse_santander import ParseError, inspect_file
from moooney.ingest.upload import save_upload
from tests.conftest import FAKE_IBAN, make_santander_xlsx

CARD_HEADER = ["Fecha operación", "Concepto", "Importe", "Divisa"]
CARD_ROWS = [
    ("06/11/2026", "TIENDA ONLINE FICTICIA", "-4,99€", "EUR"),
    ("06/11/2026", "TIENDA ONLINE FICTICIA", "-4,99€", "EUR"),  # dos compras iguales el mismo día
    ("05/11/2026", "TIENDA ONLINE FICTICIA*AB12CD", "-279,43€", "EUR"),
]


def make_card_xlsx(path: Path, rows=CARD_ROWS, *, prelude: str | None = "Tarjeta de crédito ****9999") -> Path:
    wb = Workbook()
    ws = wb.active
    if prelude:
        ws["A1"] = prelude
    for j, label in enumerate(CARD_HEADER, start=1):
        ws.cell(row=3, column=j, value=label)
    for i, row in enumerate(rows, start=4):
        for j, v in enumerate(row, start=1):
            ws.cell(row=i, column=j, value=v)
    wb.save(path)
    return path


def _count(conn, where="1=1", args=()):
    return conn.execute(f"SELECT COUNT(*) FROM transactions WHERE {where}", args).fetchone()[0]


def test_extracto_de_tarjeta_se_reconoce(tmp_path, settings):
    r = inspect_file(make_card_xlsx(tmp_path / "t.xlsx"), settings.profiles)
    assert r.bank == "tarjeta"
    assert list(r.df.account.unique()) == ["tarjeta ****9999"]
    assert r.df.concept.tolist()[0] == "COMPRA EN TIENDA ONLINE FICTICIA"
    assert r.df.amount.tolist() == [-4.99, -4.99, -279.43]
    assert r.df.balance.isna().all()
    # sin número de tarjeta delante, la cuenta es «tarjeta» a secas
    sin = inspect_file(make_card_xlsx(tmp_path / "s.xlsx", prelude=None), settings.profiles)
    assert list(sin.df.account.unique()) == ["tarjeta"]


def test_una_cuenta_sin_saldo_no_pasa_por_tarjeta(tmp_path, settings):
    labels = ["Fecha Operación", "Fecha Valor", "Concepto", "Importe", "", "", "Divisa"]
    f = make_santander_xlsx(tmp_path / "c.xlsx", header_labels=labels)
    with pytest.raises(ParseError, match="balance"):
        inspect_file(f, settings.profiles)


def test_tarjeta_respeta_compras_repetidas_y_no_duplica_al_solapar(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_card_xlsx(d / "a.xlsx")
    [o] = ingest_directory(d, conn, settings)
    assert o.rows_total == 3 and o.rows_new == 3  # las dos compras iguales entran las dos
    # un segundo extracto que se solapa: repite las tres y trae una nueva
    make_card_xlsx(d / "b.xlsx", rows=[("07/11/2026", "OTRA TIENDA INVENTADA", "-10,00€", "EUR"), *CARD_ROWS])
    outs = ingest_directory(d, conn, settings)
    assert [o.rows_new for o in outs if not o.skipped] == [1]
    assert _count(conn) == 4
    # si el nuevo trae tres compras iguales y había dos, entra solo la tercera
    make_card_xlsx(d / "c.xlsx", rows=[CARD_ROWS[0]] * 3)
    outs = ingest_directory(d, conn, settings)
    assert [o.rows_new for o in outs if not o.skipped] == [1]
    assert _count(conn, "amount = -4.99") == 3


def test_cuenta_mismo_movimiento_con_concepto_distinto_no_se_duplica(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_santander_xlsx(d / "a.xlsx")
    otro = [("15/02/2025", "16/02/2025", "COMPRA EN CAFETERIA EL GATO AZUL, CON LA TARJETA XXXX", "-3,50", "3.496,50", "EUR"),
            ("20/02/2025", "20/02/2025", "CAFETERIA EL GATO AZUL", "-3,50", "3.493,00", "EUR")]
    make_santander_xlsx(d / "b.xlsx", rows=otro)
    outs = ingest_directory(d, conn, settings)
    assert [o.rows_new for o in outs] == [3, 1]  # el del 15/02 ya estaba aunque el concepto cambie
    assert _count(conn) == 4


# Cargo, devolución y otra vez el mismo cargo el mismo día: el saldo vuelve a la misma cifra.
# Los saldos encadenan fila a fila (de la más reciente a la más antigua), como en el Santander.
REPETIDO = [
    ("03/03/2025", "03/03/2025", "TIENDA INVENTADA", "-5,00", "95,00", "EUR"),
    ("03/03/2025", "03/03/2025", "DEVOLUCION TIENDA INVENTADA", "5,00", "100,00", "EUR"),
    ("03/03/2025", "03/03/2025", "TIENDA INVENTADA", "-5,00", "95,00", "EUR"),
    ("02/03/2025", "02/03/2025", "INGRESO INVENTADO", "100,00", "100,00", "EUR"),
]


def test_cuenta_con_movimiento_repetido_legitimo_entra_entero(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_santander_xlsx(d / "a.xlsx", rows=REPETIDO)
    [o] = ingest_directory(d, conn, settings)
    assert o.rows_new == 4
    # un extracto que se solapa con los mismos cuatro no añade nada
    make_santander_xlsx(d / "b.xlsx", rows=[*REPETIDO, ("04/03/2025", "04/03/2025", "OTRA", "-1,00", "94,00", "EUR")])
    outs = ingest_directory(d, conn, settings)
    assert [o.rows_new for o in outs if not o.skipped] == [1]
    assert find_overlap_duplicates(conn) == []  # las repeticiones legítimas no se tocan


def test_recuperar_lo_que_la_huella_antigua_juntaba(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_santander_xlsx(d / "a.xlsx", rows=REPETIDO)
    ingest_directory(d, conn, settings)
    # como lo dejaba la versión anterior: la segunda aparición no entró
    conn.execute("DELETE FROM transactions WHERE fingerprint LIKE '%#2'")
    conn.commit()
    assert _count(conn) == 3
    assert recover_missing(conn, settings) == 1
    assert _count(conn) == 4 and recover_missing(conn, settings) == 0  # y no repite


def test_un_fichero_ilegible_no_bloquea_a_los_demas(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    wb = Workbook()
    wb.active["A1"] = "nada que ver"
    wb.save(d / "a_raro.xlsx")
    make_santander_xlsx(d / "b.xlsx")
    outs = ingest_directory(d, conn, settings)
    assert outs[0].error and outs[1].rows_new == 3


def test_reparar_duplicados_ya_cargados(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    d = settings.paths.excel_dir
    make_santander_xlsx(d / "a.xlsx")
    ingest_directory(d, conn, settings)
    # como entraba antes de 1.5.1: la misma fila con otro concepto y otra huella
    row = dict(conn.execute("SELECT * FROM transactions ORDER BY id LIMIT 1").fetchone())
    conn.execute(
        "INSERT INTO transactions (fingerprint, date, concept, amount, balance, account, created_at)"
        " VALUES ('x', ?, 'OTRO TEXTO', ?, ?, ?, 'hoy')",
        (row["date"], row["amount"], row["balance"], row["account"]),
    )
    conn.commit()
    ids = find_overlap_duplicates(conn)
    assert len(ids) == 1 and ids[0] != row["id"]  # se queda el primero cargado
    assert remove_overlap_duplicates(conn, ids) == 1
    assert find_overlap_duplicates(conn) == [] and _count(conn) == 3


def test_liquidacion_deja_de_ser_gasto_si_hay_compras_de_tarjeta(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)

    def tx(fp, date, account, flow=None, concept="X"):
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, amount, account, flow_type, created_at)"
            " VALUES (?, ?, ?, -100, ?, ?, 'hoy')", (fp, date, concept, account, flow))

    tx("liq-oct", "2026-10-03", "****0123", "card_settlement")  # tarjeta no importada para septiembre
    tx("liq-nov", "2026-11-03", "****0123", "card_settlement")
    conn.commit()
    assert repo.mark_card_settlements(conn) == 0
    tx("t1", "2026-10-01", "tarjeta ****9999")
    tx("t2", "2026-10-31", "tarjeta ****9999")
    conn.commit()
    assert repo.mark_card_settlements(conn) == 1
    flows = dict(conn.execute("SELECT fingerprint, flow_type FROM transactions WHERE fingerprint LIKE 'liq%'").fetchall())
    assert flows == {"liq-oct": "card_settlement", "liq-nov": "card_settlement_covered"}
    # al recategorizar la clave, la marca se recalcula y no se pierde
    conn.execute("UPDATE transactions SET concept_clean = 'LIQ' WHERE fingerprint LIKE 'liq%'")
    repo.propagate_to_transactions(conn, key="LIQ", category="Tarjeta de credito", subcategory="liquidacion",
                                   flow_type="card_settlement", confidence=1.0)
    assert conn.execute("SELECT flow_type FROM transactions WHERE fingerprint = 'liq-nov'").fetchone()[0] == "card_settlement_covered"


def test_subida_rechaza_formato_desconocido_y_acepta_tarjeta(tmp_path, settings):
    wb = Workbook()
    wb.active["A1"] = "Fecha"
    wb.active["B1"] = "Otra cosa"
    wb.save(tmp_path / "raro.xlsx")
    raro = save_upload(settings.paths.excel_dir, "raro.xlsx", (tmp_path / "raro.xlsx").read_bytes(), settings.profiles)
    assert not raro.ok and "no reconozco el formato" in raro.message
    assert not list(settings.paths.excel_dir.iterdir())  # no deja nada en la carpeta
    tarjeta = make_card_xlsx(tmp_path / "tarjeta.xlsx").read_bytes()
    assert save_upload(settings.paths.excel_dir, "tarjeta.xlsx", tarjeta, settings.profiles).ok


def test_iban_falso_sigue_siendo_cuenta(tmp_path, settings):
    assert FAKE_IBAN  # el extracto de cuenta conserva su perfil
    r = inspect_file(make_santander_xlsx(tmp_path / "c.xlsx"), settings.profiles)
    assert r.bank == "santander" and r.df.account.iloc[0].startswith("****")
