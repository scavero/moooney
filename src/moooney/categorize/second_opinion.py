"""Segunda opinión sobre las claves dudosas con el modelo de texto.

Clef-Flash decide bien cuando reconoce el nombre; cuando no, devuelve Otros con poca
confianza. Para esas claves, un modelo de texto más capaz (Qwen) recibe por lotes la
misma información que Clef (clave limpia, tipo de operación, importe redondeado,
apariciones, intervalo) más el resumen web si lo hay, y responde con categoría,
confianza y un porqué de una línea. Nunca pisa claves corregidas por el usuario, por
regla ni por parecido: solo las que vienen de un modelo.
"""

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from moooney.categorize.pipeline import KeyJob, dudosa_jobs
from moooney.categorize.taxonomy import Taxonomy
from moooney.db import repo
from moooney.enrich.query import sanitize_summary
from moooney.llm.anonymize import assert_safe_text
from moooney.llm.client import LLMClient, LLMError, LLMSettings

SOURCE = "llm"
BATCH = 15
MIN_CONFIDENCE = 0.7  # por debajo, la segunda opinión no sustituye a la primera

SYSTEM = (
    "Eres un clasificador de movimientos bancarios de una cuenta personal en España. "
    "Recibes claves de comercio ya normalizadas (sin importes exactos ni datos personales) "
    "y debes asignar a cada una la categoría más probable de una lista cerrada. "
    "Usa tu conocimiento de marcas, cadenas, aseguradoras, administraciones y servicios. "
    "Pistas: un 'recibo domiciliado' suele ser vivienda, seguros, suscripciones, impuestos o "
    "educación; una 'compra con tarjeta' suele ser alimentación, compras, ocio, transporte o salud. "
    "Si de verdad no puedes deducirlo, responde Otros con confianza baja. "
    "Responde solo JSON con la forma {\"answers\": [{\"key\": ..., \"category\": ..., "
    "\"confidence\": 0-1, \"why\": \"una línea\"}]}."
)


@dataclass
class Opinion:
    key: str
    category: str
    confidence: float
    why: str


@dataclass
class SecondOpinionSummary:
    examined: int = 0
    changed: int = 0
    confirmed: int = 0
    kept: int = 0
    failed: int = 0


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _web_summaries(conn: sqlite3.Connection, keys: list[str], max_chars: int = 160) -> dict[str, str]:
    if not keys:
        return {}
    ph = ",".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT key, summary FROM merchant_enrichment WHERE key IN ({ph}) AND summary IS NOT NULL",
        keys,
    ).fetchall()
    return {r["key"]: sanitize_summary(r["summary"], max_chars) for r in rows if r["summary"]}


def build_batch_prompt(jobs: list[KeyJob], taxonomy: Taxonomy, web: dict[str, str]) -> str:
    """Contenido de usuario: criterios de la taxonomía y las claves del lote, en JSON."""
    items = []
    for j in jobs:
        item: dict = {
            "key": j.key,
            "tipo_operacion": j.op_type or "desconocido",
            "importe_medio_eur": round(j.avg_amount / 10) * 10,
            "apariciones": j.n,
        }
        if j.gap_days:
            item["intervalo_medio_dias"] = round(j.gap_days)
        if j.key in web:
            item["info_externa"] = web[j.key]
        items.append(item)
    payload = {
        "categorias": {name: spec.description for name, spec in taxonomy.categories.items()},
        "claves": items,
    }
    return json.dumps(payload, ensure_ascii=False)


def parse_opinions(raw: str, taxonomy: Taxonomy) -> list[Opinion]:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMError("la segunda opinión no es JSON válido") from exc
    answers = data.get("answers") if isinstance(data, dict) else data
    if not isinstance(answers, list):
        raise LLMError("la segunda opinión no trae una lista de respuestas")
    out: list[Opinion] = []
    for a in answers:
        if not isinstance(a, dict):
            continue
        cat = str(a.get("category") or "")
        if cat not in taxonomy.categories:
            continue
        try:
            conf = max(0.0, min(1.0, float(a.get("confidence") or 0.0)))
        except (TypeError, ValueError):
            conf = 0.0
        out.append(Opinion(key=str(a.get("key") or ""), category=cat, confidence=conf,
                           why=str(a.get("why") or "")[:200]))
    return out


def ask_batch(client: LLMClient, llm: LLMSettings, jobs: list[KeyJob], taxonomy: Taxonomy,
              web: dict[str, str]) -> list[Opinion]:
    content = assert_safe_text(build_batch_prompt(jobs, taxonomy, web), max_chars=12000)
    raw = client.chat(
        model=llm.models["redaccion"],
        messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": content}],
        purpose="second_opinion",
        response_format={"type": "json_object"},
        max_tokens=200 + 90 * len(jobs),
        extra=llm.chat_extra,
    )
    return parse_opinions(raw, taxonomy)


def apply_opinion(conn: sqlite3.Connection, job: KeyJob, op: Opinion, taxonomy: Taxonomy,
                  *, min_confidence: float = MIN_CONFIDENCE) -> str:
    """Guarda la opinión si es útil. Devuelve 'changed', 'confirmed' o 'kept'."""
    existing = repo.get_merchant(conn, job.key)
    if existing is None or existing["source"] not in ("clef", "clef_web", SOURCE):
        return "kept"
    if op.confidence < min_confidence or (op.category == "Otros" and existing["category"] == "Otros"):
        return "kept"
    spec = taxonomy.categories[op.category]
    subcategory = "otros" if "otros" in spec.subcategories else None
    repo.upsert_merchant(
        conn, key=job.key, category=op.category, subcategory=subcategory,
        flow_type=taxonomy.default_flow_type(op.category), source=SOURCE,
        confidence=op.confidence, updated_at=_now(), force=True,
    )
    repo.propagate_to_transactions(
        conn, key=job.key, category=op.category, subcategory=subcategory,
        flow_type=taxonomy.default_flow_type(op.category), confidence=op.confidence,
    )
    return "changed" if op.category != existing["category"] else "confirmed"


def run_second_opinion(
    conn: sqlite3.Connection, client: LLMClient, llm: LLMSettings, taxonomy: Taxonomy, *,
    threshold: float = 0.7, limit: int | None = None, batch: int = BATCH,
    on_progress: Callable[[str, str], None] | None = None,
) -> SecondOpinionSummary:
    """Segunda opinión por lotes sobre las claves dudosas que vienen de un modelo."""
    jobs = dudosa_jobs(conn, threshold=threshold, sources=("clef", "clef_web"))
    if limit:
        jobs = jobs[:limit]
    summary = SecondOpinionSummary(examined=len(jobs))
    web = _web_summaries(conn, [j.key for j in jobs])
    by_key = {j.key: j for j in jobs}
    for i in range(0, len(jobs), batch):
        lote = jobs[i:i + batch]
        try:
            opinions = ask_batch(client, llm, lote, taxonomy, web)
        except LLMError as e:
            summary.failed += len(lote)
            if on_progress:
                on_progress("lote", f"falló ({e})")
            continue
        seen: set[str] = set()
        for op in opinions:
            job = by_key.get(op.key)
            if job is None or op.key in seen:
                continue
            seen.add(op.key)
            result = apply_opinion(conn, job, op, taxonomy, min_confidence=threshold)
            setattr(summary, result, getattr(summary, result) + 1)
            if on_progress and result != "kept":
                on_progress(op.key, f"{op.category} ({op.confidence:.2f}) · {op.why}")
        summary.kept += len(lote) - len(seen)  # claves que el modelo no devolvió
    return summary
