"""Copias de seguridad de la base (decisión 2026-11-11).

Se copia con la API de copia de SQLite (`Connection.backup`), que da un fichero coherente
aunque la web o la vigilancia estén escribiendo; copiar el `.db` a pelo puede dejar una copia
a medias. Cada copia es una base SQLite normal que se abre tal cual.

Nombre: `moooney-AAAAMMDD-HHMMSS[-motivo].db` en `paths.backups_dir`. Se conservan las
`keep` más recientes. La vigilancia hace una al día y *Empezar de cero* otra antes de borrar,
así que también ese borrado tiene vuelta atrás.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

KEEP = 14
PREFIX = "moooney-"


@dataclass
class BackupInfo:
    path: Path
    created: datetime
    reason: str
    size: int


def _parse(path: Path) -> BackupInfo | None:
    stem = path.stem.removeprefix(PREFIX)
    try:
        created = datetime.strptime(stem[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    return BackupInfo(path, created, stem[16:], path.stat().st_size)


def list_backups(dest_dir: Path) -> list[BackupInfo]:
    """Copias de la carpeta, de la más reciente a la más antigua."""
    if not dest_dir.exists():
        return []
    found = [b for p in dest_dir.glob(f"{PREFIX}*.db") if (b := _parse(p)) is not None]
    return sorted(found, key=lambda b: b.created, reverse=True)


def make_backup(db_path: Path, dest_dir: Path, *, reason: str = "", keep: int = KEEP,
                now: datetime | None = None) -> Path:
    """Copia coherente de la base y poda de las antiguas. Devuelve la ruta de la copia."""
    now = now or datetime.now()
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = f"{PREFIX}{now:%Y%m%d-%H%M%S}{'-' + reason if reason else ''}.db"
    target = dest_dir / name
    tmp = target.with_suffix(".part")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(target)
    for old in list_backups(dest_dir)[keep:]:
        old.path.unlink(missing_ok=True)
    return target


def backup_if_due(db_path: Path, dest_dir: Path, *, every: timedelta = timedelta(days=1),
                  keep: int = KEEP, now: datetime | None = None) -> Path | None:
    """Hace una copia si la última tiene más de `every`. Para la vigilancia: una al día."""
    if not db_path.exists():
        return None
    now = now or datetime.now()
    last = list_backups(dest_dir)
    if last and now - last[0].created < every:
        return None
    return make_backup(db_path, dest_dir, keep=keep, now=now)


def restore_backup(backup: Path, db_path: Path, dest_dir: Path) -> Path:
    """Vuelve a una copia. Antes guarda la base actual como copia «antes-de-restaurar»,
    así restaurar tampoco pierde nada. Devuelve la ruta de esa copia previa."""
    if not backup.exists():
        raise FileNotFoundError(backup)
    check = sqlite3.connect(backup)
    try:
        check.execute("PRAGMA schema_version").fetchone()  # falla si no es una base SQLite
    finally:
        check.close()
    previa = make_backup(db_path, dest_dir, reason="antes-de-restaurar", keep=10**6) if db_path.exists() else None
    src = sqlite3.connect(backup)
    try:
        dst = sqlite3.connect(db_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return previa or backup


__all__ = ["BackupInfo", "backup_if_due", "list_backups", "make_backup", "restore_backup"]
