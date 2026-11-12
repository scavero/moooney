"""Exportación de movimientos categorizados a CSV.

Formato pensado para abrirse en Excel en español: separador `;`, decimales con coma,
UTF-8 con BOM. Misma selección de filtros que la página de movimientos.
"""

import csv
import io
import sqlite3
from pathlib import Path

from moooney.db import repo
from moooney.web.queries import transactions_all

COLUMNAS = [
    "fecha", "concepto", "comercio", "categoria", "subcategoria", "flujo",
    "importe", "saldo", "cuenta", "etiquetas", "fuente", "confianza",
]


def _num(v: float | None) -> str:
    return "" if v is None else f"{v:.2f}".replace(".", ",")


def transactions_csv(
    conn: sqlite3.Connection, *, month: str | None = None, category: str | None = None,
    flow: str | None = None, key: str | None = None, q: str | None = None,
    account: str | None = None, tag: str | None = None, dates: tuple[str, str] | None = None,
) -> tuple[str, int]:
    """Devuelve (texto CSV, número de filas) de los movimientos que cumplen el filtro."""
    rows = transactions_all(conn, month=month, category=category, flow=flow, key=key, q=q,
                            account=account, tag=tag, dates=dates)
    etiquetas = repo.tags_for(conn, [int(t["id"]) for t in rows])
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\n")
    w.writerow(COLUMNAS)
    for t in rows:
        w.writerow([
            t["date"], t["concept"], t["concept_clean"] or "", t["category"] or "",
            t["subcategory"] or "", t["flow_type"] or "", _num(t["amount"]), _num(t["balance"]),
            t["account"] or "", "|".join(etiquetas.get(int(t["id"]), [])),
            t["source"] or "", _num(t["confidence"]),
        ])
    return buf.getvalue(), len(rows)


def export_filename(*, month: str | None = None, category: str | None = None) -> str:
    partes = ["moooney-movimientos"]
    if month:
        partes.append(month)
    if category:
        partes.append(category.lower().replace(" ", "-"))
    return "-".join(partes) + ".csv"


def write_csv(conn: sqlite3.Connection, path: Path, **filters) -> int:
    texto, n = transactions_csv(conn, **filters)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(texto, encoding="utf-8-sig")
    return n
