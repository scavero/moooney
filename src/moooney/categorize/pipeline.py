"""Cascada de categorización: reglas → memoria por embedding → Clef/Qwen.

Reglas y métricas son deterministas; el modelo solo interpreta claves que
ninguna regla ni la memoria de comercios han podido resolver.
"""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

from moooney.categorize import embed
from moooney.categorize.rules import Rule, match_rules
from moooney.categorize.taxonomy import Taxonomy
from moooney.db import repo
from moooney.llm.client import LLMError
from moooney.llm.decide import Answer, Decider


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class KeyJob:
    """Estadísticas agregadas de una clave pendiente (lo único que ve el modelo)."""

    key: str
    n: int
    avg_amount: float
    gap_days: float | None


def pending_jobs(conn: sqlite3.Connection) -> list[KeyJob]:
    """Claves sin entrada en merchants, con sus agregados."""
    rows = conn.execute(
        """
        SELECT t.concept_clean AS k, COUNT(*) AS n, AVG(t.amount) AS avg_amount,
               MIN(t.date) AS dmin, MAX(t.date) AS dmax
        FROM transactions t
        LEFT JOIN merchants m ON m.key = t.concept_clean
        WHERE t.concept_clean IS NOT NULL AND m.key IS NULL
        GROUP BY t.concept_clean
        ORDER BY n DESC
        """
    ).fetchall()
    jobs = []
    for r in rows:
        gap = None
        if r["n"] > 1 and r["dmin"] and r["dmax"]:
            d0 = date.fromisoformat(r["dmin"])
            d1 = date.fromisoformat(r["dmax"])
            gap = (d1 - d0).days / (r["n"] - 1)
        jobs.append(KeyJob(key=r["k"], n=r["n"], avg_amount=float(r["avg_amount"]), gap_days=gap))
    return jobs


def build_state(job: KeyJob) -> str:
    """Redacción del estado para Clef. Importe redondeado a la decena: ni
    saldos ni importes exactos salen de la máquina."""
    media = round(job.avg_amount / 10) * 10
    tipo = "gasto" if job.avg_amount < 0 else "abono"
    gap = f", intervalo medio entre apariciones {round(job.gap_days)} dias" if job.gap_days else ""
    return (
        f"Movimiento bancario. Concepto normalizado: {job.key}. "
        f"Importe medio: {media:+.0f} EUR ({tipo}) en {job.n} apariciones{gap}."
    )


def build_questions(taxonomy: Taxonomy) -> dict:
    return {
        "category": {
            "type": "choice",
            "instructions": "Categoria del movimiento",
            "criteria": {name: spec.description for name, spec in taxonomy.categories.items()},
        },
        "flow_type": {
            "type": "choice",
            "instructions": "Tipo de flujo",
            "criteria": dict(taxonomy.flow_types),
        },
        "is_recurring": {
            "type": "noul",
            "instructions": "Parece un cargo recurrente o periodico?",
        },
    }


def _save(
    conn: sqlite3.Connection,
    *,
    key: str,
    category: str,
    subcategory: str | None,
    flow_type: str,
    source: str,
    confidence: float,
    dry_run: bool,
) -> bool:
    if dry_run:
        return True
    saved = repo.upsert_merchant(
        conn,
        key=key,
        category=category,
        subcategory=subcategory,
        flow_type=flow_type,
        source=source,
        confidence=confidence,
        updated_at=_now(),
    )
    if saved:
        repo.propagate_to_transactions(
            conn,
            key=key,
            category=category,
            subcategory=subcategory,
            flow_type=flow_type,
            confidence=confidence,
        )
    return saved


def apply_rules(
    conn: sqlite3.Connection,
    jobs: list[KeyJob],
    rules: list[Rule],
    *,
    dry_run: bool = False,
) -> tuple[int, list[KeyJob]]:
    """Pasa las reglas por las claves pendientes. Devuelve (resueltas, restantes)."""
    resueltas = 0
    restantes: list[KeyJob] = []
    for job in jobs:
        match = match_rules(job.key, rules)
        if match and _save(
            conn,
            key=job.key,
            category=match.category,
            subcategory=match.subcategory,
            flow_type=match.flow_type,
            source="rule",
            confidence=1.0,
            dry_run=dry_run,
        ):
            resueltas += 1
        elif not match:
            restantes.append(job)
    return resueltas, restantes


def apply_embeddings(
    conn: sqlite3.Connection,
    jobs: list[KeyJob],
    client,
    model: str,
    *,
    dry_run: bool = False,
) -> tuple[int, list[KeyJob]]:
    """Hereda categorías de claves user/rule parecidas (>0.92). Sin cliente (None) salta."""
    if client is None:
        return 0, jobs
    vectors = embed.embed_keys(client, model, [j.key for j in jobs])
    resueltas = 0
    restantes: list[KeyJob] = []
    for job in jobs:
        vector = vectors[job.key]
        if not dry_run:
            embed.store_embedding(conn, key=job.key, model=model, vector=vector, updated_at=_now())
        hit = embed.find_similar_labeled(conn, vector, model=model)
        if hit is not None:
            merchant, sim = hit
            if _save(
                conn,
                key=job.key,
                category=merchant["category"],
                subcategory=merchant["subcategory"],
                flow_type=merchant["flow_type"],
                source="embedding",
                confidence=sim,
                dry_run=dry_run,
            ):
                resueltas += 1
                continue
        restantes.append(job)
    return resueltas, restantes


def apply_decider(
    conn: sqlite3.Connection,
    jobs: list[KeyJob],
    decider: Decider,
    taxonomy: Taxonomy,
    questions: dict,
    *,
    max_workers: int = 8,
    dry_run: bool = False,
    on_progress: Callable[[str, str], None] | None = None,
) -> tuple[int, int]:
    """Consulta el decider para las claves restantes, en paralelo.

    Devuelve (resueltas, fallidas). Las escritas van a la DB desde este hilo
    principal; los workers solo hacen red.
    """
    if not jobs:
        return 0, 0

    def ask(job: KeyJob) -> dict[str, Answer]:
        return decider.decide(build_state(job), questions)

    resueltas = fallidas = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
        futures = {pool.submit(ask, job): job for job in jobs}
        for future, job in futures.items():
            try:
                answers = future.result()
            except (LLMError, ValueError):
                fallidas += 1
                continue
            saved = _save_answers(
                conn, job, answers, taxonomy, source="clef", dry_run=dry_run
            )
            if saved:
                resueltas += 1
                if on_progress:
                    on_progress(job.key, "clef")
            else:
                fallidas += 1
    return resueltas, fallidas


def _save_answers(
    conn: sqlite3.Connection,
    job: KeyJob,
    answers: dict[str, Answer],
    taxonomy: Taxonomy,
    *,
    source: str,
    dry_run: bool,
) -> bool:
    category = answers.get("category")
    if category is None or category.choice not in taxonomy.categories:
        return False
    flow = answers.get("flow_type")
    flow_type = (
        flow.choice
        if flow is not None and flow.choice in taxonomy.flow_types
        else taxonomy.default_flow_type(category.choice)
    )
    spec = taxonomy.categories[category.choice]
    subcategory = "otros" if "otros" in spec.subcategories else None
    return _save(
        conn,
        key=job.key,
        category=category.choice,
        subcategory=subcategory,
        flow_type=flow_type,
        source=source,
        confidence=category.confidence,
        dry_run=dry_run,
    )
