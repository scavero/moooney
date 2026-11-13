"""Reintento de las claves dudosas con el decider.

Las claves que Clef dejó con poca confianza o en Otros se vuelven a preguntar con el
estado completo: tipo de operación (recibo domiciliado, compra con tarjeta…) y, si
existe, el resumen web del enriquecimiento. Se guarda solo si la confianza mejora.
Nunca toca claves corregidas por el usuario, por regla ni por parecido.
"""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from moooney.categorize.pipeline import KeyJob, build_state, decide_all, dudosa_jobs, subcategory_of
from moooney.categorize.taxonomy import Taxonomy
from moooney.db import repo
from moooney.enrich.providers import Enrichment
from moooney.enrich.runner import SOURCE_WEB, build_enriched_state
from moooney.llm.client import LLMError
from moooney.llm.decide import Decider


@dataclass
class ReaskSummary:
    examined: int = 0
    improved: int = 0
    changed: int = 0
    failed: int = 0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _enrichment_for(conn: sqlite3.Connection, key: str) -> Enrichment | None:
    r = conn.execute(
        "SELECT provider, query, summary, place_types FROM merchant_enrichment WHERE key = ?", (key,)
    ).fetchone()
    if r is None or not r["summary"]:
        return None
    tipos = [t for t in (r["place_types"] or "").split(",") if t]
    return Enrichment(provider=r["provider"] or "web", query=r["query"], summary=r["summary"],
                      place_types=tipos)


def state_for(conn: sqlite3.Connection, job: KeyJob, *, max_summary_chars: int) -> tuple[str, bool]:
    """(estado, con_web): el estado enriquecido si hay resumen web, si no el básico."""
    enr = _enrichment_for(conn, job.key)
    if enr is None:
        return build_state(job), False
    return build_enriched_state(job, enr, max_summary_chars), True


def reask_dudosas(
    conn: sqlite3.Connection, decider: Decider, taxonomy: Taxonomy, questions: dict, *,
    threshold: float = 0.7, max_workers: int = 8, limit: int | None = None,
    max_summary_chars: int = 300, on_progress: Callable[[str, str], None] | None = None,
) -> ReaskSummary:
    jobs = dudosa_jobs(conn, threshold=threshold, sources=("clef", "clef_web", "llm"))
    if limit:
        jobs = jobs[:limit]
    summary = ReaskSummary(examined=len(jobs))
    if not jobs:
        return summary
    states = {j.key: state_for(conn, j, max_summary_chars=max_summary_chars) for j in jobs}

    def ask(job: KeyJob):
        return decide_all(decider, states[job.key][0], questions, taxonomy)

    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(jobs)))) as pool:
        futures = {pool.submit(ask, job): job for job in jobs}
        for future, job in futures.items():
            try:
                answers = future.result()
            except (LLMError, ValueError):
                summary.failed += 1
                continue
            category = answers.get("category")
            if category is None or category.choice not in taxonomy.categories:
                summary.failed += 1
                continue
            existing = repo.get_merchant(conn, job.key)
            if existing is None or existing["source"] == "user":
                continue
            prev_conf = float(existing["confidence"] or 0.0)
            if category.confidence <= prev_conf:
                continue
            if category.choice == "Otros" and existing["category"] == "Otros":
                continue
            flow = answers.get("flow_type")
            flow_type = (
                flow.choice if flow is not None and flow.choice in taxonomy.flow_types
                else taxonomy.default_flow_type(category.choice)
            )
            spec = taxonomy.categories[category.choice]
            subcategory = subcategory_of(answers, spec)
            source = SOURCE_WEB if states[job.key][1] else "clef"
            repo.upsert_merchant(
                conn, key=job.key, category=category.choice, subcategory=subcategory,
                flow_type=flow_type, source=source, confidence=category.confidence,
                updated_at=_now(), force=True,
            )
            repo.propagate_to_transactions(
                conn, key=job.key, category=category.choice, subcategory=subcategory,
                flow_type=flow_type, confidence=category.confidence,
            )
            summary.improved += 1
            if category.choice != existing["category"]:
                summary.changed += 1
            if on_progress:
                on_progress(job.key, f"{existing['category']} -> {category.choice} ({category.confidence:.2f})")
    return summary
