"""Tests del parser con ficheros sintéticos generados en tmp_path."""

import datetime as dt
from pathlib import Path

import pytest

from moooney.config import Settings
from moooney.ingest.parse_santander import (
    ParseError,
    clean_concept,
    extract_account,
    find_header_row,
    inspect_file,
    map_columns,
    normalize_name,
    parse,
    parse_amount,
    parse_date_value,
)
from tests.conftest import (
    FAKE_IBAN,
    FAKE_ROWS,
    HEADER_LABELS,
    make_santander_xlsx,
    make_settings,
)


def test_deteccion_cabecera(tmp_path: Path, settings: Settings) -> None:
    f = make_santander_xlsx(tmp_path / "a.xlsx")
    result = inspect_file(f, settings.parser)
    assert result.header_row == 8
    assert result.sheet_name == "Movimientos de tu cuenta"
    assert set(result.mapping) == {"date", "value_date", "concept", "amount", "balance"}
    assert result.mapping["date"] == "Fecha Operación"
    assert "Divisa" in result.unmapped


def test_cabecera_sin_filas_previas_y_con_maxusculas(tmp_path: Path, settings: Settings) -> None:
    # "fecha operacion" con acento y mayusculas debe detectar el keyword "fecha"
    f = make_santander_xlsx(
        tmp_path / "b.xlsx", header_labels=["FECHA OPERACIÓN", "Fecha Valor", "Concepto", "Importe", "", "Saldo", ""]
    )
    result = inspect_file(f, settings.parser)
    assert result.header_row == 8
    assert result.unmapped == []


def test_columnas_destino_exactas(tmp_path: Path, settings: Settings) -> None:
    f = make_santander_xlsx(tmp_path / "c.xlsx")
    df = parse(f, settings)
    assert list(df.columns) == ["date", "value_date", "concept", "amount", "balance", "account"]
    assert len(df) == len(FAKE_ROWS)


def test_mapeo_con_alias_configurables(tmp_path: Path) -> None:
    labels = ["Fecha Operación", "Fecha Valor", "Detalle", "Neto", "", "Saldo Final", ""]
    f = make_santander_xlsx(tmp_path / "d.xlsx", header_labels=labels, iban=None)
    settings = make_settings(tmp_path)
    settings.parser.columns = {
        "date": ["fecha operacion"],
        "value_date": ["fecha valor"],
        "concept": ["detalle"],
        "amount": ["neto"],
        "balance": ["saldo final"],
    }
    result = inspect_file(f, settings.parser)
    assert result.mapping == {
        "date": "Fecha Operación",
        "value_date": "Fecha Valor",
        "concept": "Detalle",
        "amount": "Neto",
        "balance": "Saldo Final",
    }
    assert result.account_found is False


def test_normalizacion_importes() -> None:
    assert parse_amount("-1.234,56 €") == pytest.approx(-1234.56)
    assert parse_amount("1.234,56€") == pytest.approx(1234.56)
    assert parse_amount("-2,00") == pytest.approx(-2.0)
    assert parse_amount("56,00") == pytest.approx(56.0)
    assert parse_amount(-50.0) == pytest.approx(-50.0)
    assert parse_amount("1,234.56") == pytest.approx(1234.56)  # formato en-US
    assert parse_amount("1.234") == pytest.approx(1234.0)  # miles sin coma
    with pytest.raises(ParseError):
        parse_amount("no es un numero")


def test_normalizacion_fechas() -> None:
    assert parse_date_value("10/01/2025") == "2025-01-10"
    assert parse_date_value("3/2/2025") == "2025-02-03"  # d/m/y
    assert parse_date_value(dt.datetime(2025, 2, 3, 12, 30)) == "2025-02-03"
    assert parse_date_value(dt.date(2025, 2, 3)) == "2025-02-03"
    assert parse_date_value("2025-02-03") == "2025-02-03"
    with pytest.raises(ParseError):
        parse_date_value("31/02/2025")
    with pytest.raises(ParseError):
        parse_date_value("ayer")


def test_concepto_mayusculas_espacios() -> None:
    assert clean_concept("  pago   móvil en \tBar Falso ") == "PAGO MÓVIL EN BAR FALSO"


def test_enmascarado_iban() -> None:
    assert extract_account([("Cuenta", None), (FAKE_IBAN, "x")]) == "****0123"
    assert extract_account([("Cuenta", "sin iban aqui"), ("Titular", "X")]) == "principal"
    # digito suelto detras del IBAN, como en la cabecera real
    assert extract_account([(FAKE_IBAN + " 3",)]) == "****0123"


def test_account_en_parse(tmp_path: Path, settings: Settings) -> None:
    f = make_santander_xlsx(tmp_path / "e.xlsx")
    assert set(parse(f, settings)["account"]) == {"****0123"}
    f2 = make_santander_xlsx(tmp_path / "f.xlsx", iban=None)
    assert set(parse(f2, settings)["account"]) == {"principal"}


def test_error_columna_obligatoria_sin_valores(tmp_path: Path) -> None:
    labels = list(HEADER_LABELS)
    labels[2] = ""  # quitamos Concepto
    f = make_santander_xlsx(tmp_path / "g.xlsx", header_labels=labels)
    settings = make_settings(tmp_path)
    with pytest.raises(ParseError) as excinfo:
        inspect_file(f, settings.parser)
    msg = str(excinfo.value)
    assert "concept" in msg  # la obligatoria que falta
    assert "Importe" in msg and "Saldo" in msg  # nombres encontrados
    assert "54,20" not in msg and "MERCADONA" not in msg  # nunca valores de celdas


def test_error_sin_fila_de_cabecera(tmp_path: Path, settings: Settings) -> None:
    f = make_santander_xlsx(tmp_path / "h.xlsx", header_labels=["A", "B", "C"])
    with pytest.raises(ParseError, match="cabecera"):
        inspect_file(f, settings.parser)


def test_normaliza_nombre() -> None:
    assert normalize_name("  Fecha  Operación: ") == "fecha operacion"


def test_find_header_row() -> None:
    rows = [
        (None, None, "Cuenta", "Fecha"),  # la primera no vacia es "Cuenta": no es cabecera
        ("", "", "Movimientos", None),
        (None,),
        ("fecha operacion", "fecha valor", "concepto"),
    ]
    assert find_header_row(rows, "fecha") == 3
    assert find_header_row([("a",), ("b",)], "fecha") is None


def test_map_columns_requiere_config_valida() -> None:
    settings = make_settings(Path("."))
    mapping, found = map_columns(tuple(HEADER_LABELS), settings.parser)
    assert mapping["amount"] == 3
    assert found[6] == "Divisa"


def test_filas_incompletas_se_omiten(tmp_path: Path, settings: Settings) -> None:
    rows = list(FAKE_ROWS) + [("20/02/2025", "", "SIN IMPORTE", "", "", "")]
    f = make_santander_xlsx(tmp_path / "i.xlsx", rows=rows)
    df = parse(f, settings)
    assert len(df) == len(FAKE_ROWS)
    assert not df["amount"].isna().any()
    assert str(df["amount"].dtype).startswith("float")
