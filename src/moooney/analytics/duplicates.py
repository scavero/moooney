"""Posibles cargos duplicados: el código busca los candidatos y el modelo de decisión opina.

Candidato = dos o más movimientos de la misma cuenta, el mismo comercio y el mismo importe en
pocos días. La mayoría son repeticiones normales (el café de cada mañana), así que cada grupo
se le pregunta al decider con lo mínimo: clave de comercio, tipo de operación, importe, días
de separación y lo habitual que es ese comercio. Nunca van el concepto original, nombres,
saldos ni la cuenta (ampliación de la regla 3 decidida por el usuario el 2026-11-15).
El veredicto se guarda en `duplicate_reviews`; el usuario puede descartar un grupo a mano.
Nada se borra solo: apartar un movimiento es siempre un clic del usuario.
"""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date

from moooney.categorize.normalize import operation_type
from moooney.llm.anonymize import AnonymizeError
from moooney.llm.client import LLMError
from moooney.llm.decide import Decider

WINDOW_DAYS = 3
VERDICTS = ("duplicado", "normal", "dudoso")
QUESTION = {
    "veredicto": {
        "type": "choice",
        "instructions": "Estos cargos iguales y cercanos, ¿son el mismo cobro repetido por error o pagos distintos?",
        "criteria": {
            "duplicado": "El mismo cobro repetido por error: un recibo, suscripcion o compra poco habitual"
                         " cobrado dos veces con el mismo importe",
            "normal": "Pagos distintos y legitimos: comercio habitual donde el mismo importe se repite"
                      " (cafe, transporte, peajes, aparcamiento, comedor) o envios periodicos de dinero",
            "dudoso": "No hay datos suficientes para decidir",
        },
    }
}


@dataclass
class Group:
    ids: list[int]
    key: str
    amount: float
    rows: list[sqlite3.Row]
    span_days: int
    verdict: str | None = None      # duplicado | normal | dudoso | None (sin revisar)
    confidence: float | None = None
    source: str | None = None       # clef | user
    state: str = field(default="", repr=False)

    @property
    def gid(self) -> str:
        return ",".join(str(i) for i in self.ids)


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _state(conn: sqlite3.Connection, g: Group) -> str:
    """Lo único que ve el modelo de un grupo. Sin concepto original, nombres, saldos ni cuenta."""
    tot = conn.execute(
        "SELECT COUNT(*) AS n, MIN(date) AS a, MAX(date) AS b, SUM(ROUND(amount, 2) = ROUND(?, 2)) AS iguales"
        " FROM transactions WHERE concept_clean = ?", (g.amount, g.key)).fetchone()
    meses = max(1.0, _days(tot["a"], tot["b"]) / 30.4)
    rec = conn.execute("SELECT period_days FROM recurring WHERE key = ? AND active = 1", (g.key,)).fetchone()
    op = operation_type(g.rows[0]["concept"])
    cuando = "el mismo dia" if g.span_days == 0 else f"en {g.span_days} dias"
    return (
        f"Posible cargo duplicado. Comercio: {g.key}."
        + (f" Tipo de operacion: {op}." if op else "")
        + f" {len(g.ids)} {'cargos' if g.amount < 0 else 'abonos'} de {abs(g.amount):.2f} EUR {cuando}, en la misma cuenta."
        + f" Este comercio aparece {tot['n']} veces en total, unas {tot['n'] / meses:.1f} al mes;"
        + f" {tot['iguales']} de ellas con este mismo importe."
        + (f" Es un pago recurrente cada {rec['period_days']} dias." if rec else " No es un pago recurrente detectado.")
    )


def candidates(conn: sqlite3.Connection) -> list[Group]:
    """Grupos de movimientos iguales (cuenta, comercio, importe) separados por WINDOW_DAYS o menos,
    con el veredicto guardado si lo hay. Los más recientes primero."""
    rows = conn.execute(
        "SELECT id, date, concept, concept_clean, amount, account FROM transactions"
        " WHERE amount != 0 AND concept_clean IS NOT NULL"
        " ORDER BY account, concept_clean, ROUND(amount, 2), date, id"
    ).fetchall()
    groups: list[Group] = []
    run: list[sqlite3.Row] = []

    def close() -> None:
        if len(run) > 1:
            groups.append(Group(ids=[int(r["id"]) for r in run], key=run[0]["concept_clean"],
                                amount=float(run[0]["amount"]), rows=list(run),
                                span_days=_days(run[0]["date"], run[-1]["date"])))

    for r in rows:
        same = run and (r["account"], r["concept_clean"], round(r["amount"], 2)) == (
            run[-1]["account"], run[-1]["concept_clean"], round(run[-1]["amount"], 2))
        if same and _days(run[-1]["date"], r["date"]) <= WINDOW_DAYS:
            run.append(r)
        else:
            close()
            run = [r]
    close()
    reviews = {r["ids"]: r for r in conn.execute("SELECT * FROM duplicate_reviews")}
    for g in groups:
        rv = reviews.get(g.gid)
        if rv is not None:
            g.verdict, g.confidence, g.source = rv["verdict"], rv["confidence"], rv["source"]
    groups.sort(key=lambda g: (g.rows[-1]["date"], g.ids[-1]), reverse=True)
    return groups


def _save(conn: sqlite3.Connection, gid: str, verdict: str, confidence: float | None, source: str, now: str) -> None:
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO duplicate_reviews (ids, verdict, confidence, source, created_at)"
            " VALUES (?, ?, ?, ?, ?)", (gid, verdict, confidence, source, now))


def dismiss(conn: sqlite3.Connection, gid: str, *, now: str) -> bool:
    """El usuario dice que ese grupo no es un duplicado. False si el grupo ya no existe."""
    if gid not in {g.gid for g in candidates(conn)}:
        return False
    _save(conn, gid, "normal", 1.0, "user", now)
    return True


def review(conn: sqlite3.Connection, decider: Decider, *, now: str, max_workers: int = 8,
           on_progress: Callable[[str], None] | None = None) -> tuple[int, int, int]:
    """Pregunta al decider por los grupos sin veredicto. Devuelve (preguntados, duplicados, fallidos)."""
    pend = [g for g in candidates(conn) if g.verdict is None]
    for g in pend:
        g.state = _state(conn, g)
    if not pend:
        return 0, 0, 0
    dup = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(pend)))) as pool:
        futures = {pool.submit(decider.decide, g.state, QUESTION): g for g in pend}
        for future, g in futures.items():
            try:
                a = future.result().get("veredicto")
            except (LLMError, AnonymizeError, ValueError):
                a = None
            if a is None or a.choice not in VERDICTS:
                failed += 1
                continue
            _save(conn, g.gid, a.choice, a.confidence, "clef", now)
            dup += a.choice == "duplicado"
            if on_progress:
                on_progress(f"{g.key}: {a.choice}")
    return len(pend), dup, failed
