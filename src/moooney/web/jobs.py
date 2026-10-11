"""Trabajos en segundo plano para la web (ingesta, categorización, enriquecimiento...).

Un trabajo corre en un hilo, escribe líneas en su registro y la página lo
consulta por HTMX cada pocos segundos hasta que termina. Solo un trabajo a la
vez: la base SQLite es una y las acciones de la pipeline no deben solaparse.
"""

import threading
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

Logger = Callable[[str], None]


@dataclass
class Job:
    id: str
    name: str
    status: str = "running"  # running | done | error
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    lines: list[str] = field(default_factory=list)
    error: str | None = None

    def log(self, line: str) -> None:
        self.lines.append(line)

    @property
    def running(self) -> bool:
        return self.status == "running"


class JobRegistry:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._current: Job | None = None

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def current(self) -> Job | None:
        if self._current is not None and self._current.running:
            return self._current
        return None

    def recent(self, n: int = 10) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[:n]

    def start(self, name: str, fn: Callable[[Logger], None]) -> Job:
        """Lanza `fn(log)` en un hilo. Si ya hay uno corriendo, lanza RuntimeError."""
        with self._lock:
            if self.current() is not None:
                raise RuntimeError(f"ya hay un trabajo en marcha: {self._current.name}")
            job = Job(id=uuid.uuid4().hex[:8], name=name)
            self._jobs[job.id] = job
            self._current = job

        def run() -> None:
            try:
                fn(job.log)
                job.status = "done"
            except Exception as exc:  # el error se muestra en la página, no se pierde
                job.status = "error"
                job.error = f"{exc.__class__.__name__}: {exc}"
                job.log(traceback.format_exc().strip().splitlines()[-1])
            finally:
                job.finished_at = datetime.now().isoformat(timespec="seconds")

        threading.Thread(target=run, name=f"moooney-job-{job.id}", daemon=True).start()
        return job
