"""Gastos compartidos (decisión 2026-11-11). Determinista, sin modelos.

Pagas una cena de 80 € y tres amigos te devuelven 20 € cada uno por Bizum. Sin esto, el mes
enseña 80 € de gasto en restaurantes y 60 € de ingresos; lo real es 20 € de gasto. Al
emparejar un ingreso con su gasto, el ingreso pasa a ser un gasto en positivo de la misma
categoría (`flow_type = expense`, importe > 0): las métricas, que suman los gastos, lo restan
solas y deja de contar como ingreso. El vínculo vive en `reimbursements` y se reaplica cada
vez que la memoria de comercios vuelve a escribir los movimientos (`repo.apply_reimbursements`).

Las propuestas son solo propuestas: ingresos de Transferencias (bizum recibido, de particular)
con gastos de los 21 días anteriores mayores que el ingreso (nunca otra transferencia, como un
bizum enviado). Para no llenar la lista de ruido, un gasto solo se propone si cuadra como
reparto a partes iguales (80 € = 4 × 20 €) o es de la última semana.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from moooney.db import repo

WINDOW_DAYS = 21
NEAR_DAYS = 7  # sin reparto exacto, solo se proponen los gastos de la última semana
MAX_PARTS = 8
SPLIT_TOLERANCE = 0.03  # 3 % (o 50 céntimos) para dar por bueno un reparto a partes iguales
INCOME_CATEGORY = "Transferencias"
INCOME_FLOWS = ("income", "transfer_internal")


@dataclass
class Candidate:
    expense: sqlite3.Row
    parts: int | None  # entre cuántos sale a partes iguales, si cuadra
    days: int
    pending: float  # lo que queda del gasto sin devolver, en positivo


@dataclass
class Suggestion:
    income: sqlite3.Row
    candidates: list[Candidate] = field(default_factory=list)


def _split(total: float, part: float) -> int | None:
    if part <= 0:
        return None
    n = round(total / part)
    if 2 <= n <= MAX_PARTS and abs(total - n * part) <= max(0.5, SPLIT_TOLERANCE * total):
        return n
    return None


def _returned(conn: sqlite3.Connection, expense_id: int) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(t.amount), 0) FROM reimbursements r JOIN transactions t ON t.id = r.income_id"
        " WHERE r.expense_id = ?",
        (expense_id,),
    ).fetchone()
    return float(row[0])


def suggestions(conn: sqlite3.Connection, *, limit: int = 30, per_income: int = 3) -> list[Suggestion]:
    """Ingresos sin emparejar que podrían ser devoluciones, con sus gastos candidatos."""
    marks = ",".join("?" for _ in INCOME_FLOWS)
    incomes = conn.execute(
        f"""SELECT * FROM transactions t
            WHERE t.amount > 0 AND t.category = ? AND t.flow_type IN ({marks})
              AND t.id NOT IN (SELECT income_id FROM reimbursements)
              AND t.id NOT IN (SELECT income_id FROM reimbursement_dismissed)
            ORDER BY t.date DESC, t.id DESC""",
        (INCOME_CATEGORY, *INCOME_FLOWS),
    ).fetchall()
    out: list[Suggestion] = []
    for inc in incomes:
        d = date.fromisoformat(inc["date"])
        amount = float(inc["amount"])
        rows = conn.execute(
            "SELECT * FROM transactions WHERE flow_type = 'expense' AND amount < 0 AND date BETWEEN ? AND ?"
            " AND COALESCE(category, '') != ? ORDER BY date DESC",
            ((d - timedelta(days=WINDOW_DAYS)).isoformat(), d.isoformat(), INCOME_CATEGORY),
        ).fetchall()
        cands: list[Candidate] = []
        for e in rows:
            total = -float(e["amount"])
            pending = total - _returned(conn, e["id"])
            if pending < amount * 1.05:  # tiene que quedar más por devolver que lo que llega
                continue
            parts, days = _split(total, amount), (d - date.fromisoformat(e["date"])).days
            if parts is None and days > NEAR_DAYS:
                continue
            cands.append(Candidate(e, parts, days, pending))
        if not cands:
            continue
        cands.sort(key=lambda c: (c.parts is None, c.days, -float(c.expense["amount"])))
        out.append(Suggestion(inc, cands[:per_income]))
        if len(out) >= limit:
            break
    return out


def link(conn: sqlite3.Connection, income_id: int, expense_id: int) -> None:
    inc = conn.execute("SELECT amount FROM transactions WHERE id = ?", (income_id,)).fetchone()
    exp = conn.execute("SELECT amount FROM transactions WHERE id = ?", (expense_id,)).fetchone()
    if inc is None or exp is None:
        raise ValueError("movimiento no encontrado")
    if float(inc["amount"]) <= 0 or float(exp["amount"]) >= 0:
        raise ValueError("hay que emparejar un ingreso con un gasto")
    with conn:
        conn.execute("DELETE FROM reimbursement_dismissed WHERE income_id = ?", (income_id,))
        conn.execute(
            "INSERT OR REPLACE INTO reimbursements (income_id, expense_id, created_at) VALUES (?, ?, ?)",
            (income_id, expense_id, datetime.now().isoformat(timespec="seconds")),
        )
    repo.apply_reimbursements(conn)


def unlink(conn: sqlite3.Connection, income_id: int) -> None:
    """Deshace el vínculo: el ingreso vuelve a la categoría de su comercio."""
    with conn:
        conn.execute("DELETE FROM reimbursements WHERE income_id = ?", (income_id,))
        conn.execute(
            """UPDATE transactions SET category = m.category, subcategory = m.subcategory,
                   flow_type = m.flow_type, confidence = m.confidence
               FROM merchants m WHERE m.key = transactions.concept_clean AND transactions.id = ?""",
            (income_id,),
        )


def dismiss(conn: sqlite3.Connection, income_id: int) -> None:
    with conn:
        conn.execute(
            "INSERT OR IGNORE INTO reimbursement_dismissed (income_id, created_at) VALUES (?, ?)",
            (income_id, datetime.now().isoformat(timespec="seconds")),
        )


def linked(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT i.id AS income_id, i.date AS income_date, i.concept_clean AS income_key, i.amount AS income_amount,
                  e.id AS expense_id, e.date AS expense_date, e.concept_clean AS expense_key,
                  e.amount AS expense_amount, e.category AS category
           FROM reimbursements r JOIN transactions i ON i.id = r.income_id JOIN transactions e ON e.id = r.expense_id
           ORDER BY e.date DESC, i.date"""
    ).fetchall()
