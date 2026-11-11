"""Helpers compartidos: Excel sintético con estructura del Santander y settings de test."""

import datetime as dt
from pathlib import Path

import pytest
import yaml
from openpyxl import Workbook

from moooney.config import Settings

# Datos inventados y obviamente falsos. IBAN falso (0081 es una entidad inexistente
# con ese prefijo completo) y conceptos de relleno.
FAKE_IBAN = "ES66 0081 5700 23 4567890123"
FAKE_ROWS = [
    # (fecha operacion, fecha valor, concepto, importe, saldo, divisa)
    ("10/01/2025", "11/01/2025", "MERCADONA FALSA 001", "-54,20", "1.000,00", "EUR"),
    (dt.datetime(2025, 2, 3), dt.datetime(2025, 2, 3), "NOMINA ACME DE PRUEBAS SL", "2.500,00", "3.500,00", "EUR"),
    ("15/02/2025", "16/02/2025", "CAFETERIA EL GATO AZUL", "-3,50", "3.496,50", "EUR"),
]
HEADER_LABELS = ["Fecha Operación", "Fecha Valor", "Concepto", "Importe", "", "Saldo", "Divisa"]

REPO_ROOT = Path(__file__).resolve().parent.parent


def make_santander_xlsx(
    path: Path,
    rows: list[tuple] = FAKE_ROWS,
    *,
    iban: str | None = FAKE_IBAN,
    header_labels: list[str] = HEADER_LABELS,
    sheet: str = "Movimientos de tu cuenta",
) -> Path:
    """Genera un .xlsx con la estructura del extracto real del Santander.

    Filas 1-4 con cuenta/IBAN/titular/saldo, columna vacia intermedia antes del
    Saldo, importes y fechas como texto o como celdas de fecha segun la fila.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    ws["C1"] = "Cuenta"
    ws["D1"] = "Fecha"
    if iban:
        ws["C2"] = iban + " 3"  # digito suelto detras, como en la exportacion real
        ws["D2"] = "01/01/2030 | 00:00:00"
    ws["C3"] = "Titular"
    ws["D3"] = "Saldo"
    ws["C4"] = "APELLIDOS SIN NOMBRE REAL"
    ws["D4"] = "9.999,99 €"
    ws["A6"] = "Movimientos"
    header_row = 8
    for j, label in enumerate(header_labels, start=1):
        if label:
            ws.cell(row=header_row, column=j, value=label)
    for i, row in enumerate(rows, start=header_row + 1):
        for j, value in enumerate(row, start=1):
            col = j if j <= 4 else j + 1  # deja la columna 5 vacia
            ws.cell(row=i, column=col, value=value)
    wb.save(path)
    return path


FAKE_BBVA_ROWS = [
    # (F.Valor, Fecha, Concepto, Movimiento, Importe, Disponible) — inventados
    ("11/10/2026", "13/10/2026", "Tienda Inventada            villaficticia   es", "Pago con tarjeta", -12.5, 987.65),
    ("10/10/2026", "13/10/2026", "Bizum", "Enviado: nota de prueba", -20.0, 1000.15),
    ("06/10/2026", "06/10/2026", "Adeudo seguros ficticios", "Adeudo nº 20262780020093", -45.0, 1020.15),
    ("04/10/2026", "05/10/2026", "Ret. efectivo  a debito con tarj. en cajero. aut.", "Otros", -50.0, 1065.15),
    ("02/10/2026", "02/10/2026", "Transferencia realizada", "Alquiler piso de prueba", "-600,00", "1.115,15"),
    ("01/10/2026", "01/10/2026", "Abono de nómina", "Empresa Ficticia SL", 1700.0, 1715.15),
]


def make_bbva_xlsx(path: Path, rows: list[tuple] = FAKE_BBVA_ROWS) -> Path:
    """Genera un .xlsx con la estructura del "Últimos movimientos" del BBVA.

    Columna A vacía, título y fecha de generación arriba (la fila 3 empieza por "Fecha"
    y NO es la cabecera), cabecera en la fila 5 con dos columnas "Divisa" y una
    "Observaciones" que lleva un número con pinta de tarjeta y nunca debe acabar en la base.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Informe BBVA"
    ws["B2"] = "Últimos movimientos"
    ws["B3"] = "Fecha de generación del informe: 11/10/2026"
    labels = ["F.Valor", "Fecha", "Concepto", "Movimiento", "Importe", "Divisa", "Disponible", "Divisa", "Observaciones"]
    for j, label in enumerate(labels, start=2):
        ws.cell(row=5, column=j, value=label)
    for i, (vdate, fdate, concept, movement, amount, balance) in enumerate(rows, start=6):
        values = [vdate, fdate, concept, movement, amount, "EUR", balance, "EUR", f"4000111122223333 {concept.upper()}"]
        for j, value in enumerate(values, start=2):
            ws.cell(row=i, column=j, value=value)
    wb.save(path)
    return path


def make_settings(tmp_path: Path) -> Settings:
    """Settings reales del repo con las rutas apuntando a tmp_path."""
    data = yaml.safe_load((REPO_ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    data["paths"] = {
        "excel_dir": str(tmp_path / "excel"),
        "clean_dir": str(tmp_path / "clean"),
        "db_path": str(tmp_path / "moooney.db"),
        "reports_dir": str(tmp_path / "reports"),
        "backups_dir": str(tmp_path / "backups"),
    }
    return Settings.model_validate(data)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    excel_dir = tmp_path / "excel"
    excel_dir.mkdir(parents=True, exist_ok=True)
    return make_settings(tmp_path)
