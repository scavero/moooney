"""Trabajos en segundo plano para la web (ingesta, categorización, enriquecimiento...).

Un trabajo corre en un hilo, escribe líneas en su registro y la página lo
consulta por HTMX cada pocos segundos hasta que termina. Solo un trabajo a la
vez: la base SQLite es una y las acciones de la pipeline no deben solaparse.

Además del registro, el trabajo lleva su estado para la persona que espera: en qué etapa va
(`stage`), qué está haciendo ahora (`detail`) y, cuando se sabe, cuánto lleva (`done` de `total`).
El trabajo se pasa a la función como `log`: se llama como un logger normal y, si la función sabe
más, usa `log.stage(...)` y `log.detail(...)`. Las funciones de `service.py` también aceptan un
logger simple (CLI, tests), así que esas llamadas van siempre protegidas por `report()`.
"""

import threading
import time
import traceback
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

Logger = Callable[[str], None]


def report(log: Logger, text: str, done: int | None = None, total: int | None = None) -> None:
    """Cuenta qué se está haciendo ahora, si el logger lo admite (un Job sí; un lambda no)."""
    fn = getattr(log, "detail", None)
    if fn is not None:
        fn(text, done, total)


def stage(log: Logger, name: str) -> None:
    fn = getattr(log, "stage", None)
    if fn is not None:
        fn(name)


@dataclass
class Job:
    id: str
    name: str
    status: str = "running"  # running | done | error
    started_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    finished_at: str | None = None
    lines: list[str] = field(default_factory=list)
    error: str | None = None
    stages: list[str] = field(default_factory=list)
    stage_index: int = 0
    current: str = ""  # qué está haciendo ahora, en palabras
    done: int | None = None
    total: int | None = None
    _t0: float = field(default_factory=time.monotonic, repr=False)
    _t1: float | None = field(default=None, repr=False)

    def log(self, line: str) -> None:
        self.lines.append(line)

    __call__ = log

    def stage(self, name: str) -> None:
        if name not in self.stages:
            self.stages.append(name)
        self.stage_index = self.stages.index(name)
        self.current, self.done, self.total = "", None, None

    def detail(self, text: str, done: int | None = None, total: int | None = None) -> None:
        self.current, self.done, self.total = text, done, total

    def finish(self, status: str) -> None:
        self.status = status
        self._t1 = time.monotonic()
        self.finished_at = datetime.now().isoformat(timespec="seconds")
        self.current, self.done, self.total = "", None, None
        if status == "done":
            self.stage_index = len(self.stages)

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def elapsed(self) -> str:
        s = int((self._t1 or time.monotonic()) - self._t0)
        return f"{s} s" if s < 60 else f"{s // 60} min {s % 60:02d} s"

    @property
    def seconds_since_finish(self) -> float:
        return float("inf") if self._t1 is None else time.monotonic() - self._t1

    @property
    def percent(self) -> int | None:
        if not self.total or self.done is None:
            return None
        return max(0, min(100, round(100 * self.done / self.total)))

    def stage_state(self, i: int) -> str:
        """done | current | error | todo, para pintar la lista de etapas."""
        if i < self.stage_index:
            return "done"
        if i == self.stage_index:
            return "error" if self.status == "error" else "current" if self.running else "done"
        return "todo"


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

    def latest(self) -> Job | None:
        """El último trabajo lanzado, esté en marcha o no."""
        return self._current

    def recent(self, n: int = 10) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.started_at, reverse=True)[:n]

    def start(self, name: str, fn: Callable[[Job], None], stages: Sequence[str] = ()) -> Job:
        """Lanza `fn(job)` en un hilo. Si ya hay uno corriendo, lanza RuntimeError."""
        with self._lock:
            if self.current() is not None:
                raise RuntimeError(f"ya hay un trabajo en marcha: {self._current.name}")
            job = Job(id=uuid.uuid4().hex[:8], name=name, stages=list(stages))
            self._jobs[job.id] = job
            self._current = job

        def run() -> None:
            try:
                fn(job)
                job.finish("done")
            except Exception as exc:  # el error se muestra en la página, no se pierde
                job.error = f"{exc.__class__.__name__}: {exc}"
                job.log(traceback.format_exc().strip().splitlines()[-1])
                job.finish("error")

        threading.Thread(target=run, name=f"moooney-job-{job.id}", daemon=True).start()
        return job
