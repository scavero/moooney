"""Excel subidos desde la web: se validan y se guardan en la carpeta de entrada.

Después la ingesta normal los trata igual que si el usuario los hubiera copiado a mano, así que
la deduplicación por hash de fichero y por huella de movimiento sigue valiendo.
"""

from __future__ import annotations

import hashlib
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path

from moooney.ingest.detect import EXCEL_SUFFIXES, list_excel_files, sha256_file

MAX_BYTES = 20 * 1024 * 1024  # un extracto de varios años ocupa bastante menos
# Firmas de los dos formatos: .xlsx es un zip, .xls un documento OLE.
MAGIC = {".xlsx": (b"PK\x03\x04",), ".xls": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", b"PK\x03\x04")}
UNSAFE = re.compile(r"[^\w.\- ()]+", re.UNICODE)


@dataclass
class UploadResult:
    filename: str
    saved_as: str | None = None  # nombre en la carpeta; None si no se guardó
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.saved_as is not None


def safe_name(filename: str) -> str:
    """Solo el nombre (nunca una ruta), sin caracteres raros. Lo que quede vacío pasa a 'extracto'."""
    name = Path(filename.replace("\\", "/")).name
    stem, dot, ext = name.rpartition(".")  # ".xlsx" a secas: Path lo toma por fichero oculto sin extensión
    stem, suffix = (stem, dot + ext.lower()) if dot else (name, "")
    stem = UNSAFE.sub("_", stem).strip(" ._") or "extracto"
    return stem[:100] + suffix


def _xlsx_sano(content: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            return "xl/workbook.xml" in z.namelist() and z.testzip() is None
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def save_upload(excel_dir: Path, filename: str, content: bytes, profiles: list | None = None) -> UploadResult:
    """Guarda un Excel subido en `excel_dir`. No pisa nada: si ya hay un fichero idéntico no lo
    duplica, y si el nombre está ocupado por otro contenido añade (2), (3)…"""
    name = safe_name(filename or "")
    suffix = Path(name).suffix
    if suffix not in EXCEL_SUFFIXES:
        return UploadResult(filename, message="no es un Excel (.xlsx o .xls)")
    if not content:
        return UploadResult(filename, message="está vacío")
    if len(content) > MAX_BYTES:
        return UploadResult(filename, message=f"ocupa más de {MAX_BYTES // (1024 * 1024)} MB")
    if not content.startswith(MAGIC[suffix]):
        return UploadResult(filename, message="la extensión dice Excel pero el contenido no lo es")
    if suffix == ".xlsx" and not _xlsx_sano(content):
        # Un fichero roto en la carpeta haría fallar todas las importaciones siguientes.
        return UploadResult(filename, message="el Excel está dañado o incompleto; descárgalo otra vez del banco")

    if profiles is not None and suffix == ".xlsx":
        # ¿Lo entiende algún perfil de banco? Se comprueba en memoria, antes de escribir nada: un
        # fichero ilegible en la carpeta se quedaría «pendiente» para siempre.
        from moooney.ingest.parse_santander import ParseError, inspect_file

        try:
            inspect_file(content, profiles)
        except ParseError as e:
            return UploadResult(filename, message=f"no reconozco el formato del extracto: {e}")

    excel_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()
    for existing in list_excel_files(excel_dir):
        if existing.stat().st_size == len(content) and sha256_file(existing) == digest:
            return UploadResult(filename, saved_as=existing.name, message=f"ya estaba en la carpeta como {existing.name}")

    target = excel_dir / name
    n = 2
    while target.exists():
        target = excel_dir / f"{Path(name).stem} ({n}){suffix}"
        n += 1
    # Se escribe con otra extensión y se renombra: la vigilancia de la carpeta nunca ve medio fichero.
    partial = target.with_name(target.name + ".part")
    partial.write_bytes(content)
    partial.replace(target)
    return UploadResult(filename, saved_as=target.name, message="guardado")
