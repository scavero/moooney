"""Perfiles de banco: el Excel del BBVA entra por la misma pipeline que el del Santander."""

from pathlib import Path

import pytest

from moooney.categorize.normalize import clean_concept, operation_type
from moooney.config import Settings
from moooney.db import repo
from moooney.ingest.banks import bbva
from moooney.ingest.load import ingest_directory
from moooney.ingest.parse_santander import ParseError, inspect_file, parse
from tests.conftest import make_bbva_xlsx, make_santander_xlsx, make_settings


def test_constructor_de_concepto_bbva():
    assert bbva("Mercado Ficticio   villaficticia   es", "Pago con tarjeta") == "COMPRA EN MERCADO FICTICIO VILLAFICTICIA ES"
    assert bbva("Bizum", "Enviado: cena") == "BIZUM ENVIADO: CENA"
    assert bbva("Bizum", "Recibido: cena") == "BIZUM RECIBIDO: CENA"
    assert bbva("Adeudo seguros ficticios", "Adeudo nº 123") == "RECIBO SEGUROS FICTICIOS"
    assert bbva("Ret. efectivo  a debito con tarj. en cajero. aut.", "Otros") == "RETIRADA DE EFECTIVO EN CAJERO"
    assert bbva("Transferencia realizada", "Alquiler piso") == "TRANSFERENCIA ENVIADA ALQUILER PISO"
    assert bbva("Abono de nómina", "Empresa SL") == "NOMINA EMPRESA SL"
    # y la normalización del Santander los entiende tal cual
    assert clean_concept(bbva("Mercado Ficticio   villaficticia   es", "Pago con tarjeta")) == "MERCADO FICTICIO VILLAFICTICIA ES"
    assert clean_concept(bbva("Bizum", "Enviado: cena")) == "BIZUM ENVIADO"
    assert clean_concept(bbva("Adeudo seguros ficticios", "Adeudo nº 1")) == "SEGUROS FICTICIOS"
    assert operation_type(bbva("Adeudo seguros ficticios", "Adeudo nº 1")) == "recibo domiciliado"
    assert operation_type(bbva("X", "Pago con tarjeta")) == "compra con tarjeta"
    assert clean_concept(bbva("Transferencia realizada", "Alquiler piso")) == "TRANSFERENCIA A PARTICULAR"
    assert clean_concept(bbva("Abono de nómina", "Empresa SL")) == "NOMINA"


def test_bbva_se_detecta_y_parsea(tmp_path: Path, settings: Settings):
    f = make_bbva_xlsx(tmp_path / "bbva.xlsx")
    result = inspect_file(f, settings.profiles)
    assert result.bank == "bbva" and result.header_row == 5 and result.account_found is False
    assert result.mapping["date"] == "F.Valor" and result.mapping["balance"] == "Disponible"
    assert "operation" in result.mapping and "Observaciones" in result.unmapped
    df = result.df
    assert len(df) == 6 and set(df["account"]) == {"bbva"}
    assert list(df["date"])[:2] == ["2026-10-11", "2026-10-10"] and df["value_date"].iloc[0] == "2026-10-13"
    assert df["amount"].iloc[4] == -600.0 and df["balance"].iloc[4] == 1115.15  # texto es-ES
    assert df["concept"].iloc[0].startswith("COMPRA EN TIENDA INVENTADA")
    assert df["concept"].iloc[2] == "RECIBO SEGUROS FICTICIOS"
    # nada de la columna Observaciones (número de tarjeta) llega al resultado
    assert not df.astype(str).apply(lambda col: col.str.contains("4000111122223333")).any().any()
    # el Santander sigue entrando por su perfil
    s = make_santander_xlsx(tmp_path / "san.xlsx")
    assert inspect_file(s, settings.profiles).bank == "santander"
    assert parse(s, settings)["account"].iloc[0] == "****0123"


def test_bbva_y_santander_conviven_en_la_misma_base(tmp_path: Path):
    settings = make_settings(tmp_path)
    excel = settings.paths.excel_dir
    excel.mkdir(parents=True, exist_ok=True)
    make_bbva_xlsx(excel / "bbva.xlsx")
    make_santander_xlsx(excel / "santander.xlsx")
    conn = repo.connect(settings.paths.db_path)
    try:
        outcomes = ingest_directory(excel, conn, settings)
        assert sorted(o.rows_new for o in outcomes) == [3, 6]
        assert conn.execute("SELECT COUNT(DISTINCT account) FROM transactions").fetchone()[0] == 2
        # idempotente también con dos bancos
        again = ingest_directory(excel, conn, settings)
        assert all(o.skipped for o in again)
        assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 9
        assert conn.execute("SELECT COUNT(*) FROM transactions WHERE concept LIKE '%4000111122223333%'").fetchone()[0] == 0
    finally:
        conn.close()


def test_cabecera_de_bbva_sin_disponible_dice_que_falta(tmp_path: Path, settings: Settings):
    f = make_bbva_xlsx(tmp_path / "b.xlsx")
    from openpyxl import load_workbook

    wb = load_workbook(f)
    wb.active["H5"] = "Otra cosa"
    wb.save(f)
    with pytest.raises(ParseError, match="balance"):
        inspect_file(f, settings.profiles)
