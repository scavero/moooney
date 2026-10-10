"""Ejecutor del enriquecimiento: qué claves, qué consultas, y la segunda ronda con Clef.

Flujo por clave candidata:
  1. build_query → si None, se descarta (no se consulta nada).
  2. Proveedores en orden hasta el primero con resultado. Cada intento queda
     en web_calls (hit / miss / error), haya o no resultado.
  3. Resultado en caché merchant_enrichment (una vez por clave, salvo --force).
  4. Segunda ronda: mismo esquema de preguntas, state ampliado con el resumen.
     Solo se guarda si mejora la confianza; nunca pisa source=user.
"""

import hashlib
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from moooney.categorize.pipeline import KeyJob, build_state, pending_jobs_for_keys
from moooney.categorize.taxonomy import Taxonomy
from moooney.db import repo
from moooney.enrich.providers import Enrichment, Provider, ProviderError
from moooney.enrich.query import build_query, sanitize_summary
from moooney.enrich.settings import EnrichmentSettings
from moooney.llm.anonymize import AnonymizeError
from moooney.llm.client import LLMError
from moooney.llm.decide import Decider

SOURCE_WEB = "clef_web"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Candidate:
    key: str
    category: str | None
    confidence: float
    source: str
    n: int
    total: float
    query: str | None


@dataclass
class EnrichSummary:
    candidates: int = 0
    skipped_no_query: int = 0
    queried: int = 0
    hits: int = 0
    misses: int = 0  # consultas sin resultado (incluye las que solo dieron error)
    errors: int = 0  # intentos de proveedor o de modelo que fallaron
    improved: int = 0
    changed_category: int = 0
    still_low: list[str] = field(default_factory=list)
    by_provider: dict[str, int] = field(default_factory=dict)


def select_candidates(
    conn: sqlite3.Connection,
    settings: EnrichmentSettings,
    *,
    force: bool = False,
    only_key: str | None = None,
) -> list[Candidate]:
    """Claves que cumplen el disparador, con su consulta (o None si se descartan)."""
    trig = settings.trigger
    placeholders_cat = ",".join("?" for _ in trig.also_if_category) or "''"
    placeholders_flow = ",".join("?" for _ in trig.skip_flow_types) or "''"
    sql = f"""
        SELECT m.key, m.category, m.confidence, m.source,
               COUNT(t.id) AS n, COALESCE(SUM(t.amount), 0) AS total
        FROM merchants m
        LEFT JOIN transactions t ON t.concept_clean = m.key
        LEFT JOIN merchant_enrichment e ON e.key = m.key
        WHERE m.source != 'user'
          AND (m.confidence < ? OR m.category IN ({placeholders_cat}))
          AND (m.flow_type IS NULL OR m.flow_type NOT IN ({placeholders_flow}))
          {"" if force else "AND e.key IS NULL"}
          {"AND m.key = ?" if only_key else ""}
        GROUP BY m.key
        ORDER BY ABS(COALESCE(SUM(t.amount), 0)) DESC
    """
    params: list = [trig.max_confidence, *trig.also_if_category, *trig.skip_flow_types]
    if only_key:
        params.append(only_key)
    rows = conn.execute(sql, params).fetchall()
    out: list[Candidate] = []
    for r in rows:
        try:
            query = build_query(
                r["key"], region_hint=settings.region_hint, skip_patterns=trig.skip_patterns
            )
        except AnonymizeError:
            query = None
        out.append(
            Candidate(
                key=r["key"],
                category=r["category"],
                confidence=float(r["confidence"] or 0.0),
                source=r["source"],
                n=int(r["n"]),
                total=float(r["total"]),
                query=query,
            )
        )
    return out


def _record(conn: sqlite3.Connection, provider: str, query: str, status: str) -> None:
    repo.insert_web_call(
        conn,
        ts=_now(),
        provider=provider,
        query=query,
        status=status,
        payload_hash=hashlib.sha256(query.encode("utf-8")).hexdigest(),
    )


def lookup_with_providers(
    conn: sqlite3.Connection,
    providers: list[Provider],
    query: str,
    summary: EnrichSummary,
) -> Enrichment | None:
    """Primer proveedor con resultado. Registra cada intento en web_calls."""
    for provider in providers:
        try:
            result = provider.lookup(query)
        except ProviderError:
            _record(conn, provider.name, query, "error")
            summary.errors += 1
            continue
        summary.by_provider[provider.name] = summary.by_provider.get(provider.name, 0) + 1
        if result is None:
            _record(conn, provider.name, query, "miss")
            continue
        _record(conn, provider.name, query, "hit")
        return result
    return None


def build_enriched_state(job: KeyJob, enrichment: Enrichment, max_chars: int) -> str:
    base = build_state(job)
    resumen = sanitize_summary(enrichment.summary, max_chars)
    tipos = ", ".join(enrichment.place_types[:4]) if enrichment.place_types else "desconocido"
    return f"{base} Informacion externa ({enrichment.provider}): {resumen}. Tipo de lugar: {tipos}."


def second_round(
    conn: sqlite3.Connection,
    job: KeyJob,
    candidate: Candidate,
    enrichment: Enrichment,
    decider: Decider,
    taxonomy: Taxonomy,
    questions: dict,
    *,
    max_summary_chars: int,
) -> tuple[bool, bool]:
    """Vuelve a preguntar con contexto. Devuelve (mejoró, cambió_categoría)."""
    state = build_enriched_state(job, enrichment, max_summary_chars)
    answers = decider.decide(state, questions)
    category = answers.get("category")
    if category is None or category.choice not in taxonomy.categories:
        return False, False
    if category.confidence <= candidate.confidence:
        return False, False
    flow = answers.get("flow_type")
    flow_type = (
        flow.choice
        if flow is not None and flow.choice in taxonomy.flow_types
        else taxonomy.default_flow_type(category.choice)
    )
    spec = taxonomy.categories[category.choice]
    subcategory = "otros" if "otros" in spec.subcategories else None
    saved = repo.upsert_merchant(
        conn,
        key=job.key,
        category=category.choice,
        subcategory=subcategory,
        flow_type=flow_type,
        source=SOURCE_WEB,
        confidence=category.confidence,
        updated_at=_now(),
    )
    if not saved:
        return False, False
    repo.propagate_to_transactions(
        conn,
        key=job.key,
        category=category.choice,
        subcategory=subcategory,
        flow_type=flow_type,
        confidence=category.confidence,
    )
    return True, category.choice != candidate.category


def run_enrichment(
    conn: sqlite3.Connection,
    settings: EnrichmentSettings,
    providers: list[Provider],
    decider: Decider | None,
    taxonomy: Taxonomy,
    questions: dict,
    *,
    candidates: list[Candidate],
    limit: int | None = None,
    on_progress: Callable[[str, str], None] | None = None,
) -> EnrichSummary:
    """Consulta proveedores para los candidatos con consulta y hace la segunda ronda."""
    summary = EnrichSummary(candidates=len(candidates))
    con_consulta = [c for c in candidates if c.query]
    summary.skipped_no_query = len(candidates) - len(con_consulta)
    tope = min(settings.max_queries_per_run, limit or settings.max_queries_per_run)
    con_consulta = con_consulta[:tope]
    jobs = {j.key: j for j in pending_jobs_for_keys(conn, [c.key for c in con_consulta])}

    for cand in con_consulta:
        assert cand.query is not None
        summary.queried += 1
        enrichment = lookup_with_providers(conn, providers, cand.query, summary)
        if enrichment is None:
            summary.misses += 1
            repo.upsert_enrichment(
                conn, key=cand.key, provider=None, query=cand.query, summary=None,
                place_types=None, url=None, helped=0, fetched_at=_now(),
            )
            summary.still_low.append(cand.key)
            if on_progress:
                on_progress(cand.key, "sin resultado")
            continue
        summary.hits += 1
        repo.upsert_enrichment(
            conn,
            key=cand.key,
            provider=enrichment.provider,
            query=cand.query,
            summary=sanitize_summary(enrichment.summary, settings.max_summary_chars),
            place_types=",".join(enrichment.place_types) or None,
            url=enrichment.url,
            helped=None,
            fetched_at=_now(),
        )
        job = jobs.get(cand.key)
        if decider is None or job is None:
            summary.still_low.append(cand.key)
            continue
        try:
            improved, changed = second_round(
                conn, job, cand, enrichment, decider, taxonomy, questions,
                max_summary_chars=settings.max_summary_chars,
            )
        except (LLMError, AnonymizeError, ValueError):
            summary.errors += 1
            summary.still_low.append(cand.key)
            continue
        repo.set_enrichment_helped(conn, cand.key, int(improved))
        if improved:
            summary.improved += 1
            if changed:
                summary.changed_category += 1
            if on_progress:
                on_progress(cand.key, enrichment.provider)
        else:
            summary.still_low.append(cand.key)
    return summary
