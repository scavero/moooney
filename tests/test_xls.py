"""Extractos en .xls antiguo (Excel 97-2003). Todo inventado, generado en tmp_path."""

from datetime import datetime
from pathlib import Path

import xlwt

from moooney.db import repo
from moooney.ingest.load import ingest_directory
from moooney.ingest.parse_santander import inspect_file
from moooney.ingest.upload import save_upload


def make_card_xls(path: Path) -> Path:
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Movimientos")
    ws.write(0, 0, "Tarjeta de crédito ****9999")
    for j, label in enumerate(["Fecha operación", "Concepto", "Importe", "Divisa"]):
        ws.write(2, j, label)
    fecha = xlwt.easyxf(num_format_str="DD/MM/YYYY")
    filas = [
        (datetime(2026, 11, 6), "TIENDA ONLINE FICTICIA", -4.99),  # celda de fecha y número
        ("05/11/2026", "OTRA TIENDA INVENTADA", "-279,43€"),  # todo como texto
    ]
    for i, (d, concepto, importe) in enumerate(filas, start=3):
        if isinstance(d, datetime):
            ws.write(i, 0, d, fecha)
        else:
            ws.write(i, 0, d)
        ws.write(i, 1, concepto)
        ws.write(i, 2, importe)
        ws.write(i, 3, "EUR")
    wb.save(str(path))
    return path


def test_xls_de_tarjeta_se_lee(tmp_path, settings):
    r = inspect_file(make_card_xls(tmp_path / "t.xls"), settings.profiles)
    assert r.bank == "tarjeta"
    assert list(r.df.account.unique()) == ["tarjeta ****9999"]
    assert r.df.date.tolist() == ["2026-11-06", "2026-11-05"]
    assert r.df.amount.tolist() == [-4.99, -279.43]


def test_xls_se_importa_y_se_sube(tmp_path, settings):
    contenido = make_card_xls(tmp_path / "t.xls").read_bytes()
    assert save_upload(settings.paths.excel_dir, "extracto.xls", contenido, settings.profiles).ok
    conn = repo.connect(settings.paths.db_path)
    [o] = ingest_directory(settings.paths.excel_dir, conn, settings)
    assert o.error is None and o.rows_new == 2


def test_xlsx_renombrado_a_xls_tambien_vale(tmp_path, settings):
    from tests.test_overlap import make_card_xlsx

    renombrado = make_card_xlsx(tmp_path / "t.xlsx").rename(tmp_path / "t.xls")
    assert inspect_file(renombrado, settings.profiles).bank == "tarjeta"
