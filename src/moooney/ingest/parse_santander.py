"""Parser de extractos bancarios (.xlsx): Santander y, por perfiles, otros bancos.

Filas superiores con titular/IBAN/saldo o con el título del informe, luego una fila
de cabecera y después los movimientos. La cabecera se localiza por sus columnas: la
primera fila en la que casan al menos dos alias del perfil; si a esa fila le falta una
obligatoria, el error dice cuál (nunca valores de celdas). Se prueban los perfiles de
`settings.profiles` en orden (Santander primero, luego `banks:`) y gana el primero que
mapea todas sus columnas obligatorias. Todas las cadenas comparables se normalizan a
minúsculas sin acentos ni espacios dobles.
"""

import re
import unicodedata
import warnings
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import pandas as pd
from openpyxl import load_workbook

from moooney.config import TARGET_COLUMNS, ParserSettings
from moooney.ingest.banks import BUILDERS
from moooney.ingest.banks import clean_concept as clean_concept  # reexportado: lo usan tests y load

TARGET_COLUMNS_FULL = [*TARGET_COLUMNS, "account"]

# IBAN: 2 letras + 2 digitos + hasta 30 alfanumericos/espacios. En la cabecera del
# Santander pueden aparecer digitos sueltos detras, por eso luego se recorta.
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9 ]{10,32}")
ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DMY_RE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


class ParseError(Exception):
    """Formato de fichero no interpretable. El mensaje nunca contiene valores."""


@dataclass
class InspectResult:
    sheet_name: str
    header_row: int  # 1-based, como lo ve el usuario en Excel
    bank: str = "santander"
    mapping: dict[str, str] = field(default_factory=dict)  # destino -> cabecera original
    unmapped: list[str] = field(default_factory=list)
    account_found: bool = False
    df: pd.DataFrame | None = None


def normalize_name(value: object) -> str:
    """Minusculas, sin acentos, espacios colapsados y sin puntos/suces de separacion."""
    s = unicodedata.normalize("NFD", str(value))
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().strip().strip(":.").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", s)


def find_header_row(rows: list[tuple], keyword: str) -> int | None:
    """Indice 0-based de la fila cuya primera celda no vacia contiene la palabra clave."""
    kw = normalize_name(keyword)
    for i, row in enumerate(rows):
        for cell in row:
            if cell is None or (isinstance(cell, str) and not cell.strip()):
                continue
            if kw in normalize_name(cell):
                return i
            break  # la primera celda no vacia de esta fila no es la cabecera
    return None


def _match_header(header: tuple, parser: ParserSettings) -> tuple[dict[str, int], dict[int, str]]:
    """Columnas destino que casan en esta fila (sin exigir las obligatorias)."""
    found_by_index: dict[int, str] = {}
    normalized: list[str | None] = []
    for cell in header:
        if cell is None or (isinstance(cell, str) and not cell.strip()):
            normalized.append(None)
            continue
        normalized.append(normalize_name(cell))
        found_by_index[len(normalized) - 1] = str(cell).strip()

    aliases = {target: {normalize_name(a) for a in al} for target, al in parser.columns.items()}
    mapping: dict[str, int] = {}
    for target, alias_set in aliases.items():
        for idx, name in enumerate(normalized):
            if name is not None and name in alias_set and target not in mapping and idx not in mapping.values():
                mapping[target] = idx
    return mapping, found_by_index


def map_columns(header: tuple, parser: ParserSettings) -> tuple[dict[str, int], dict[int, str]]:
    """Mapea columna destino -> indice de columna, y devuelve {indice: nombre original}."""
    mapping, found_by_index = _match_header(header, parser)
    missing = [c for c in parser.required_columns if c not in mapping]
    if missing:
        raise ParseError(
            f"faltan columnas obligatorias {missing}; columnas encontradas en la cabecera: "
            f"{list(found_by_index.values())}"
        )
    return mapping, found_by_index


MAX_HEADER_SEARCH = 60


def detect_header(
    rows: list[tuple], profiles: list[ParserSettings]
) -> tuple[ParserSettings, int, dict[str, int], dict[int, str]]:
    """(perfil, índice 0-based de la cabecera, mapeo, nombres originales).

    Para cada perfil, la primera fila en la que casan al menos dos columnas destino es su
    candidata; si le faltan obligatorias se guarda el error más informativo y se sigue.
    """
    best_error: ParseError | None = None
    best_score = -1
    for profile in profiles:
        for i, row in enumerate(rows[:MAX_HEADER_SEARCH]):
            partial, _ = _match_header(row, profile)
            if len(partial) < 2:
                continue
            try:
                mapping, found = map_columns(row, profile)
            except ParseError as e:
                if len(partial) > best_score:
                    best_score, best_error = len(partial), e
                continue
            return profile, i, mapping, found
    if best_error is not None:
        raise best_error
    raise ParseError(
        "no se localizo la fila de cabecera (ninguna fila casa con las columnas de los perfiles "
        f"{[p.name for p in profiles]}); filas leidas: {len(rows)}"
    )


def extract_account(prelude_rows: list[tuple]) -> str:
    """''****NNNN'' a partir del IBAN de las filas de cabecera, o 'principal'."""
    for row in prelude_rows:
        for cell in row:
            if not isinstance(cell, str):
                continue
            for m in IBAN_RE.finditer(cell.upper()):
                candidate = re.sub(r"[^A-Z0-9]", "", m.group(0))
                if candidate.startswith("ES") and len(candidate) > 24:
                    # El Santander añade digitos sueltos detras del IBAN en la celda
                    candidate = candidate[:24]
                if re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", candidate):
                    return f"****{candidate[-4:]}"
    return "principal"


def parse_amount(value: object) -> float:
    """Importe a float. Acepta numericos y texto estilo es-ES: 1.234,56 €."""
    if isinstance(value, bool):
        raise ParseError("importe no valido")
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace("\u00a0", " ")
    s = re.sub(r"(?i)\s?(eur|€)", "", s).replace(" ", "").replace("\u2212", "-")
    if not s:
        raise ParseError("importe vacio")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    elif "." in s:
        # Unico punto: miles si el grupo final tiene 3 digitos y hay varios grupos
        if re.fullmatch(r"-?\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        raise ParseError("importe no parseable") from None


def parse_date_value(value: object) -> str:
    """Fecha a ISO YYYY-MM-DD. Acepta celdas de fecha y texto dd/mm/yyyy o ISO."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    m = DMY_RE.fullmatch(s)
    if m:
        d, mo, y = (int(g) for g in m.groups())
        try:
            return date(y, mo, d).isoformat()
        except ValueError:
            raise ParseError("fecha no valida") from None
    m = ISO_DATE_RE.fullmatch(s)
    if m:
        return s
    raise ParseError("fecha con formato no reconocido")


def _read_rows(path: Path, parser: ParserSettings) -> tuple[str, list[tuple]]:
    if path.suffix.lower() == ".xls":
        raise ParseError("formato .xls no soportado; exporta el extracto a .xlsx")
    with warnings.catch_warnings():
        # Los Excel del banco vienen sin estilo por defecto y openpyxl avisa cada vez
        warnings.filterwarnings("ignore", message="Workbook contains no default style")
        wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_name = parser.sheet or wb.sheetnames[0]
        if parser.sheet and parser.sheet not in wb.sheetnames:
            raise ParseError(f"hoja no encontrada: {parser.sheet!r}")
        ws = wb[sheet_name]
        rows = [tuple(c for c in row) for row in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return sheet_name, rows


def inspect_file(path: Path, parser: ParserSettings | list[ParserSettings]) -> InspectResult:
    """Parsea devolviendo tambien los metadatos que enseña el comando inspect.

    Acepta un perfil o la lista de perfiles (`settings.profiles`): gana el primero que
    reconoce la cabecera.
    """
    profiles = [parser] if isinstance(parser, ParserSettings) else list(parser)
    by_sheet: dict[str | None, tuple[str, list[tuple]]] = {}
    for p in profiles:
        if p.sheet not in by_sheet:
            by_sheet[p.sheet] = _read_rows(path, p)
    # detect_header necesita filas de la hoja de cada perfil: se agrupan por hoja
    chosen = None
    error: ParseError | None = None
    for sheet, (sheet_name, rows) in by_sheet.items():
        try:
            profile, header_idx, mapping_idx, found_by_index = detect_header(
                rows, [p for p in profiles if p.sheet == sheet]
            )
        except ParseError as e:
            error = error or e
            continue
        chosen = (profile, sheet_name, rows, header_idx, mapping_idx, found_by_index)
        break
    if chosen is None:
        assert error is not None
        raise error
    profile, sheet_name, rows, header_idx, mapping_idx, found_by_index = chosen
    parser = profile
    account = extract_account(rows[:header_idx]) if parser.account == "iban" else parser.account
    build_concept = BUILDERS[parser.concept_builder]

    def cell(row: tuple, target: str):
        idx = mapping_idx.get(target)
        return None if idx is None or idx >= len(row) else row[idx]

    records = []
    for offset, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if all(c is None or (isinstance(c, str) and not c.strip()) for c in row):
            continue
        raw_date, raw_amount = cell(row, "date"), cell(row, "amount")
        if raw_date in (None, "") or raw_amount in (None, ""):
            continue  # fila incompleta (p. ej. separadores o totales)
        try:
            iso_date = parse_date_value(raw_date)
            raw_vdate = cell(row, "value_date")
            iso_vdate = parse_date_value(raw_vdate) if raw_vdate not in (None, "") else None
            raw_balance = cell(row, "balance")
            amount = parse_amount(raw_amount)
            balance = parse_amount(raw_balance) if raw_balance not in (None, "") else None
        except ParseError as e:
            raise ParseError(f"{e} (fila {offset}, hoja {sheet_name!r})") from None
        records.append(
            {
                "date": iso_date,
                "value_date": iso_vdate,
                "concept": build_concept(cell(row, "concept"), cell(row, "operation")),
                "amount": amount,
                "balance": balance,
                "account": account,
            }
        )

    df = pd.DataFrame(records, columns=TARGET_COLUMNS_FULL)
    result = InspectResult(
        sheet_name=sheet_name,
        header_row=header_idx + 1,
        bank=parser.name,
        mapping={t: found_by_index[i] for t, i in sorted(mapping_idx.items())},
        unmapped=[
            n for idx, n in found_by_index.items() if idx not in mapping_idx.values()
        ],
        account_found=parser.account == "iban" and account != "principal",
        df=df,
    )
    return result


def parse(path: Path | str, settings: object) -> pd.DataFrame:
    """Parsea el Excel y devuelve un DataFrame con las columnas exactas del diseño.

    Acepta un Settings completo o directamente su ParserSettings.
    """
    profiles = getattr(settings, "profiles", None)
    if profiles is None:
        if not isinstance(settings, ParserSettings):
            raise TypeError("se espera Settings o ParserSettings")
        profiles = [settings]
    result = inspect_file(Path(path), profiles)
    assert result.df is not None
    return result.df
