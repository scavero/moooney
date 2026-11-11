"""Copias de seguridad de la base. Todo inventado, en tmp_path."""

import sqlite3
from datetime import datetime, timedelta

from typer.testing import CliRunner

from moooney.db import repo
from moooney.db.backup import backup_if_due, list_backups, make_backup, restore_backup
from moooney.ingest.load import ingest_directory
from moooney.ingest.reset import reset_data
from tests.conftest import make_santander_xlsx


def _db(tmp_path, n=3):
    db = tmp_path / "x.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (v INTEGER)")
    conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(n)])
    conn.commit()
    conn.close()
    return db


def _count(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_copia_coherente_y_poda(tmp_path):
    db, dest = _db(tmp_path), tmp_path / "copias"
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    for i in range(5):
        make_backup(db, dest, keep=3, now=t0 + timedelta(days=i))
    copias = list_backups(dest)
    assert len(copias) == 3
    assert copias[0].created == t0 + timedelta(days=4)
    assert _count(copias[0].path) == 3
    assert not list(dest.glob("*.part"))


def test_una_al_dia(tmp_path):
    db, dest = _db(tmp_path), tmp_path / "copias"
    t0 = datetime(2026, 1, 1, 8, 0, 0)
    assert backup_if_due(db, dest, now=t0) is not None
    assert backup_if_due(db, dest, now=t0 + timedelta(hours=5)) is None
    assert backup_if_due(db, dest, now=t0 + timedelta(days=1, minutes=1)) is not None
    assert backup_if_due(tmp_path / "no-existe.db", dest) is None


def test_restaurar_guarda_la_actual(tmp_path):
    db, dest = _db(tmp_path, n=3), tmp_path / "copias"
    copia = make_backup(db, dest, now=datetime(2026, 1, 1))
    conn = sqlite3.connect(db)
    conn.execute("DELETE FROM t")
    conn.commit()
    conn.close()
    previa = restore_backup(copia, db, dest)
    assert _count(db) == 3
    assert "antes-de-restaurar" in previa.name and _count(previa) == 0


def test_empezar_de_cero_deja_copia(tmp_path, settings):
    conn = repo.connect(settings.paths.db_path)
    make_santander_xlsx(settings.paths.excel_dir / "a.xlsx")
    ingest_directory(settings.paths.excel_dir, conn, settings)
    r = reset_data(conn, settings)
    conn.close()
    assert r.copia is not None and "antes-de-borrar" in r.copia.name
    assert tmp_path in r.copia.parents
    c = sqlite3.connect(r.copia)
    assert c.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 3
    c.close()


def test_cli(tmp_path, settings):
    import yaml

    from moooney.cli import app

    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(settings.model_dump(mode="json")), encoding="utf-8")
    repo.connect(settings.paths.db_path).close()
    runner = CliRunner()
    r = runner.invoke(app, ["backup", "--config", str(settings_path)])
    assert r.exit_code == 0, r.output
    r = runner.invoke(app, ["backup", "--list", "--config", str(settings_path)])
    assert "moooney-" in r.output
