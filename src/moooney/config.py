"""Carga de configuración desde config/settings.yaml con validación pydantic."""

from pathlib import Path

import yaml
from pydantic import BaseModel, field_validator

# Columnas destino del parser. 'account' se deriva del IBAN, no de una columna.
TARGET_COLUMNS = ["date", "value_date", "concept", "amount", "balance"]


class PathsSettings(BaseModel):
    excel_dir: Path = Path("data/excel")
    clean_dir: Path = Path("data/clean")
    db_path: Path = Path("data/moooney.db")


class ParserSettings(BaseModel):
    sheet: str | None = None
    header_keyword: str = "fecha"
    required_columns: list[str] = ["date", "concept", "amount", "balance"]
    columns: dict[str, list[str]]

    @field_validator("columns")
    @classmethod
    def _todas_las_destino(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        faltan = [c for c in TARGET_COLUMNS if c not in v]
        if faltan:
            raise ValueError(f"faltan columnas destino en parser.columns: {faltan}")
        desconocidas = [c for c in v if c not in TARGET_COLUMNS]
        if desconocidas:
            raise ValueError(f"columnas destino desconocidas en parser.columns: {desconocidas}")
        return v

    @field_validator("required_columns")
    @classmethod
    def _requeridas_validas(cls, v: list[str]) -> list[str]:
        invalidas = [c for c in v if c not in TARGET_COLUMNS]
        if invalidas:
            raise ValueError(f"required_columns invalidas: {invalidas}")
        return v


class Settings(BaseModel):
    paths: PathsSettings = PathsSettings()
    parser: ParserSettings


def load_settings(path: Path | None = None) -> Settings:
    """Carga settings.yaml. Por defecto, config/settings.yaml bajo el directorio actual."""
    if path is None:
        path = Path("config/settings.yaml")
    if not path.exists():
        raise FileNotFoundError(f"no se encuentra el fichero de configuración: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Settings.model_validate(data)
