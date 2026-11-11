"""Vigilancia de la carpeta de Excel: cuando aparece un fichero nuevo, corre la pipeline.

Sondeo sencillo (sin dependencias): cada `interval` segundos mira los .xlsx de
`excel_dir`, calcula su hash y, si alguno no está en `imports`, ejecuta
ingesta → categorización → (enriquecimiento si está activado) → alertas.
Sirve igual en el portátil, como tarea programada o dentro del contenedor.
"""

import time
from collections.abc import Callable
from pathlib import Path

from moooney.config import Settings
from moooney.db import repo
from moooney.db.backup import backup_if_due
from moooney.ingest.detect import list_excel_files, sha256_file

Logger = Callable[[str], None]


def pending_files(settings: Settings) -> list[Path]:
    """Ficheros de excel_dir cuyo hash no está importado todavía."""
    excel_dir = settings.paths.excel_dir
    if not excel_dir.exists():
        return []
    conn = repo.connect(settings.paths.db_path)
    try:
        return [p for p in list_excel_files(excel_dir) if repo.get_import_by_hash(conn, sha256_file(p)) is None]
    finally:
        conn.close()


def run_pipeline_once(settings: Settings, config_dir: Path, log: Logger, *, enrich: bool = True) -> None:
    """Ingesta + categorización + enriquecimiento (si procede) + alertas, con el mismo código que la web."""
    from moooney.web import service

    paths = service.Paths(config_dir)
    service.job_ingest(settings, log)
    service.job_categorize(settings, paths, log)
    enr = service.enrichment_settings_or_none(paths)
    if enrich and enr is not None and enr.enabled:
        service.job_enrich(settings, paths, log)
    service.job_alerts(settings, paths, log)


def watch(settings: Settings, config_dir: Path, log: Logger, *, interval: float = 60.0,
          once: bool = False, sleeper: Callable[[float], None] = time.sleep,
          max_iterations: int | None = None) -> int:
    """Bucle de vigilancia. Devuelve cuántas veces corrió la pipeline.

    Nunca se cae por un fichero o un error puntual: un Excel que la ingesta no sabe leer
    se aparta (por su hash) y no vuelve a lanzar la pipeline hasta que el fichero cambie;
    cualquier otro error se informa y se reintenta en la vuelta siguiente.
    """
    runs = 0
    iterations = 0
    ignorados: set[str] = set()  # hashes de Excel que no se han podido importar
    while True:
        try:  # copia diaria de la base: la vigilancia es lo único que está siempre en marcha
            copia = backup_if_due(settings.paths.db_path, settings.paths.backups_dir)
            if copia is not None:
                log(f"copia de seguridad: {copia.name}")
        except Exception as e:
            log(f"no se ha podido hacer la copia de seguridad ({type(e).__name__}: {e})")
        try:
            nuevos = [p for p in pending_files(settings) if sha256_file(p) not in ignorados]
            if nuevos:
                log(f"{len(nuevos)} fichero(s) nuevo(s): " + ", ".join(p.name for p in nuevos))
                run_pipeline_once(settings, config_dir, log)
                runs += 1
                log("pipeline completada")
                for p in pending_files(settings):  # tras la ingesta, solo quedan los ilegibles
                    h = sha256_file(p)
                    if h not in ignorados:
                        ignorados.add(h)
                        log(f"{p.name}: no se ha podido importar; se ignora hasta que cambie el fichero")
            elif once:
                log("nada nuevo")
        except Exception as e:  # la vigilancia sigue viva pase lo que pase en una vuelta
            log(f"error en la vigilancia ({type(e).__name__}: {e}); se reintenta en la próxima vuelta")
        if once:
            return runs
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return runs
        sleeper(interval)
