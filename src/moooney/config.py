"""Carga de configuración desde config/settings.yaml con validación pydantic."""

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

# Columnas destino del parser. 'account' se deriva del IBAN, no de una columna.
TARGET_COLUMNS = ["date", "value_date", "concept", "amount", "balance"]
# Columnas opcionales que algunos bancos traen y el constructor de concepto aprovecha.
OPTIONAL_COLUMNS = ["operation", "currency"]  # currency: solo para reconocer la cabecera


class PathsSettings(BaseModel):
    excel_dir: Path = Path("data/excel")
    clean_dir: Path = Path("data/clean")
    db_path: Path = Path("data/moooney.db")
    reports_dir: Path = Path("reports")  # informes en Markdown
    backups_dir: Path = Path("data/backups")  # copias de la base (db/backup.py)


class ParserSettings(BaseModel):
    """Perfil de un banco: dónde está la tabla y cómo se llaman sus columnas.

    `account`: "iban" deriva la cuenta del IBAN de las filas de cabecera; "card" es un extracto
    de tarjeta ("tarjeta" o "tarjeta ****NNNN" si enseña los últimos dígitos); cualquier otro
    valor es una etiqueta fija (el BBVA no exporta el IBAN). `concept_builder`: cómo se
    compone el concepto a partir de las celdas (ver ingest/banks.py).
    """

    name: str = "santander"
    sheet: str | None = None
    header_keyword: str = "fecha"
    required_columns: list[str] = ["date", "concept", "amount", "balance"]
    columns: dict[str, list[str]]
    account: str = "iban"
    concept_builder: str = "plain"
    # Columnas que este perfil no reclama nunca: si la cabecera las trae, es otro extracto
    # (la tarjeta no tiene saldo; una cuenta sin «Saldo» debe dar error, no pasar por tarjeta).
    reject_columns: list[str] = []

    @field_validator("columns")
    @classmethod
    def _todas_las_destino(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        faltan = [c for c in TARGET_COLUMNS if c not in v]
        if faltan:
            raise ValueError(f"faltan columnas destino en parser.columns: {faltan}")
        desconocidas = [c for c in v if c not in TARGET_COLUMNS and c not in OPTIONAL_COLUMNS]
        if desconocidas:
            raise ValueError(f"columnas destino desconocidas en parser.columns: {desconocidas}")
        return v

    @field_validator("required_columns")
    @classmethod
    def _requeridas_validas(cls, v: list[str]) -> list[str]:
        invalidas = [c for c in v if c not in TARGET_COLUMNS and c not in OPTIONAL_COLUMNS]
        if invalidas:
            raise ValueError(f"required_columns invalidas: {invalidas}")
        return v


class Settings(BaseModel):
    paths: PathsSettings = PathsSettings()
    parser: ParserSettings
    banks: list[ParserSettings] = []

    @property
    def profiles(self) -> list[ParserSettings]:
        """Perfiles en orden de prueba: el principal (Santander) y después los demás bancos."""
        return [self.parser, *self.banks]


def load_settings(path: Path | None = None) -> Settings:
    """Carga settings.yaml. Por defecto, config/settings.yaml bajo el directorio actual."""
    if path is None:
        path = Path("config/settings.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no se encuentra el fichero de configuración: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Settings.model_validate(data)
