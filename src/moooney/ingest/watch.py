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
    """Bucle de vigilancia. Devuelve cuántas veces corrió la pipeline."""
    runs = 0
    iterations = 0
    while True:
        nuevos = pending_files(settings)
        if nuevos:
            log(f"{len(nuevos)} fichero(s) nuevo(s): " + ", ".join(p.name for p in nuevos))
            run_pipeline_once(settings, config_dir, log)
            runs += 1
            log("pipeline completada")
        elif once:
            log("nada nuevo")
        if once:
            return runs
        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return runs
        sleeper(interval)
