"""Detección y hash de ficheros Excel pendientes de importar."""

import hashlib
from pathlib import Path

EXCEL_SUFFIXES = {".xlsx", ".xls"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def list_excel_files(directory: Path) -> list[Path]:
    """Ficheros .xlsx/.xls del directorio, ordenados por nombre."""
    directory = Path(directory)
    if not directory.is_dir():
        raise FileNotFoundError(f"no existe el directorio: {directory}")
    return sorted(
        p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in EXCEL_SUFFIXES
    )
