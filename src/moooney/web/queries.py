"""Consultas de solo lectura para la web. Todo SQL plano sobre la misma base que la CLI."""

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from moooney.analytics import periods
from moooney.analytics.metrics import EXPENSE_FLOWS
from moooney.analytics.period_summary import top_merchants_between
from moooney.db import repo
from moooney.ingest.detect import list_excel_files, sha256_file

PER_PAGE = 50


def months_available(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT substr(date, 1, 7) AS m FROM transactions ORDER BY m DESC"
    ).fetchall()
    return [r["m"] for r in rows]


@dataclass
class Page:
    rows: list[sqlite3.Row]
    total: int
    page: int
    per_page: int = PER_PAGE

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.per_page))

    @property
    def has_prev(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.pages


def transactions_page(
    conn: sqlite3.Connection,
    *,
    month: str | None = None,
    category: str | None = None,
    flow: str | None = None,
    key: str | None = None,
    q: str | None = None,
    account: str | None = None,
    tag: str | None = None,
    dates: tuple[str, str] | None = None,
    only_ids: list[int] | None = None,
    page: int = 1,
    per_page: int = PER_PAGE,
) -> Page:
    sql_where, params = _transactions_where(month, category, flow, key, q, account, tag, dates, only_ids)
    total = conn.execute(
        f"SELECT COUNT(*) AS n FROM transactions t {sql_where}", params
    ).fetchone()["n"]
    page = max(1, min(page, max(1, -(-total // per_page))))
    rows = conn.execute(
        f"""
        SELECT t.id, t.date, t.concept, t.concept_clean, t.amount, t.balance, t.category,
               t.subcategory, t.flow_type, t.confidence, t.account, m.source
        FROM transactions t LEFT JOIN merchants m ON m.key = t.concept_clean
        {sql_where}
        ORDER BY t.date DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return Page(rows=rows, total=int(total), page=page, per_page=per_page)


def top_merchants(conn: sqlite3.Connection, month: str, *, limit: int = 12) -> list[sqlite3.Row]:
    first, last = periods.month_bounds(month)
    return top_merchants_between(conn, first, last, limit=limit)


def category_series(conn: sqlite3.Connection, months: list[str]) -> dict[str, list[float]]:
    """{categoría: [gasto por mes]} para los meses indicados (en el mismo orden)."""
    if not months:
        return {}
    first, _ = periods.month_bounds(months[0])
    _, last = periods.month_bounds(months[-1])
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    rows = conn.execute(
        f"""
        SELECT substr(date, 1, 7) AS m, COALESCE(category, '(sin categoría)') AS c, -SUM(amount) AS g
        FROM transactions
        WHERE date BETWEEN ? AND ? AND flow_type IN ({ph})
        GROUP BY m, c
        """,
        (first, last, *EXPENSE_FLOWS),
    ).fetchall()
    idx = {m: i for i, m in enumerate(months)}
    out: dict[str, list[float]] = {}
    for r in rows:
        if r["m"] not in idx:
            continue
        out.setdefault(r["c"], [0.0] * len(months))[idx[r["m"]]] = max(float(r["g"]), 0.0)
    totals = {c: sum(v) for c, v in out.items()}
    return dict(sorted(out.items(), key=lambda kv: -totals[kv[0]]))


def merchants_page(
    conn: sqlite3.Connection,
    *,
    mode: str = "all",
    q: str | None = None,
    category: str | None = None,
    threshold: float = 0.7,
    page: int = 1,
    per_page: int = PER_PAGE,
) -> Page:
    where: list[str] = []
    params: list = []
    if mode == "dudosas":
        where.append("(m.confidence < ? OR m.category = 'Otros') AND m.source != 'user'")
        params.append(threshold)
    elif mode == "otros":
        where.append("m.category = 'Otros'")
    elif mode == "user":
        where.append("m.source = 'user'")
    if category:
        where.append("m.category = ?")
        params.append(category)
    if q:
        where.append("m.key LIKE ?")
        params.append(f"%{q.upper()}%")
    sql_where = ("WHERE " + " AND ".join(where)) if where else ""
    base = f"""
        FROM merchants m LEFT JOIN transactions t ON t.concept_clean = m.key
        {sql_where}
        GROUP BY m.key
    """
    total = conn.execute(f"SELECT COUNT(*) AS n FROM (SELECT m.key {base})", params).fetchone()["n"]
    page = max(1, min(page, max(1, -(-total // per_page))))
    rows = conn.execute(
        f"""
        SELECT m.key, m.category, m.subcategory, m.flow_type, m.source, m.confidence,
               COUNT(t.id) AS n, COALESCE(SUM(t.amount), 0) AS total, MAX(t.date) AS last_date,
               e.summary AS web_summary, e.provider AS web_provider
        {base.replace("FROM merchants m", "FROM merchants m LEFT JOIN merchant_enrichment e ON e.key = m.key")}
        ORDER BY ABS(COALESCE(SUM(t.amount), 0)) DESC
        LIMIT ? OFFSET ?
        """,
        [*params, per_page, (page - 1) * per_page],
    ).fetchall()
    return Page(rows=rows, total=int(total), page=page, per_page=per_page)


QUEUE_MIN_TX = 2
QUEUE_MIN_EUR = 30.0


def split_queue(rows: list[sqlite3.Row]) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
    """(las que importan, cola larga): una clave importa si tiene ≥ 2 movimientos o ≥ 30 €."""
    importantes = [r for r in rows if r["n"] >= QUEUE_MIN_TX or abs(float(r["total"])) >= QUEUE_MIN_EUR]
    resto = [r for r in rows if r["n"] < QUEUE_MIN_TX and abs(float(r["total"])) < QUEUE_MIN_EUR]
    return importantes, resto


def recent_for_key(conn: sqlite3.Connection, key: str, *, limit: int = 3) -> list[sqlite3.Row]:
    """Últimos movimientos de una clave (evidencia en la cola de revisión)."""
    return conn.execute(
        "SELECT date, concept, amount FROM transactions WHERE concept_clean = ? ORDER BY date DESC, id DESC LIMIT ?",
        (key, limit),
    ).fetchall()


def merchant_row(conn: sqlite3.Connection, key: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT m.key, m.category, m.subcategory, m.flow_type, m.source, m.confidence,
               COUNT(t.id) AS n, COALESCE(SUM(t.amount), 0) AS total, MAX(t.date) AS last_date,
               e.summary AS web_summary, e.provider AS web_provider
        FROM merchants m
        LEFT JOIN transactions t ON t.concept_clean = m.key
        LEFT JOIN merchant_enrichment e ON e.key = m.key
        WHERE m.key = ?
        GROUP BY m.key
        """,
        (key,),
    ).fetchone()


def merchants_summary(conn: sqlite3.Connection, threshold: float = 0.7) -> dict:
    row = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COALESCE(SUM(CASE WHEN (confidence < ? OR category = 'Otros') AND source != 'user' THEN 1 ELSE 0 END), 0) AS dudosas,
               COALESCE(SUM(CASE WHEN category = 'Otros' THEN 1 ELSE 0 END), 0) AS otros,
               COALESCE(SUM(CASE WHEN source = 'user' THEN 1 ELSE 0 END), 0) AS user
        FROM merchants
        """,
        (threshold,),
    ).fetchone()
    by_source = conn.execute(
        "SELECT source, COUNT(*) AS n FROM merchants GROUP BY source ORDER BY n DESC"
    ).fetchall()
    return {**dict(row), "by_source": [(r["source"], r["n"]) for r in by_source]}


def frequent_categories(conn: sqlite3.Connection, *, limit: int = 6) -> list[sqlite3.Row]:
    """Categorías más usadas en la memoria de comercios (para sugerir en la cola de revisión)."""
    return conn.execute(
        "SELECT category, COUNT(*) AS n FROM merchants WHERE category IS NOT NULL AND category != 'Otros'"
        " GROUP BY category ORDER BY n DESC LIMIT ?",
        (limit,),
    ).fetchall()


def egress_stats(conn: sqlite3.Connection) -> dict:
    llm = conn.execute(
        "SELECT model, purpose, COUNT(*) AS n, COALESCE(SUM(input_tokens), 0) AS tokens,"
        " MAX(ts) AS last FROM llm_calls GROUP BY model, purpose ORDER BY n DESC"
    ).fetchall()
    web = conn.execute(
        "SELECT provider, status, COUNT(*) AS n, MAX(ts) AS last FROM web_calls"
        " GROUP BY provider, status ORDER BY provider, status"
    ).fetchall()
    return {"llm": [dict(r) for r in llm], "web": [dict(r) for r in web]}


@dataclass
class ExcelFile:
    name: str
    size_kb: int
    imported: bool
    imported_at: str | None = None
    rows_total: int | None = None
    rows_new: int | None = None
    date_min: str | None = None
    date_max: str | None = None


def excel_files(conn: sqlite3.Connection, excel_dir: Path) -> list[ExcelFile]:
    out: list[ExcelFile] = []
    if not excel_dir.exists():
        return out
    for path in list_excel_files(excel_dir):
        imp = repo.get_import_by_hash(conn, sha256_file(path))
        out.append(
            ExcelFile(
                name=path.name,
                size_kb=max(1, path.stat().st_size // 1024),
                imported=imp is not None,
                imported_at=imp["imported_at"] if imp else None,
                rows_total=imp["rows_total"] if imp else None,
                rows_new=imp["rows_new"] if imp else None,
                date_min=imp["date_min"] if imp else None,
                date_max=imp["date_max"] if imp else None,
            )
        )
    return out


def imports_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM imports ORDER BY imported_at DESC").fetchall()


def alerts_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT severity, COUNT(*) AS n FROM alerts WHERE acknowledged = 0 GROUP BY severity"
    ).fetchall()
    out = {"critical": 0, "warning": 0, "info": 0}
    for r in rows:
        out[r["severity"]] = int(r["n"])
    out["total"] = sum(out.values())
    return out


def _transactions_where(month, category, flow, key, q, account=None, tag=None, dates=None, only_ids=None):
    """`dates` es un tramo (desde, hasta) de un trimestre, año o rango; `month`, un mes suelto."""
    where: list[str] = []
    params: list = []
    if only_ids is not None:  # p. ej. los movimientos de gastos compartidos; lista vacía = ninguno
        where.append(f"t.id IN ({','.join('?' for _ in only_ids)})" if only_ids else "0")
        params += list(only_ids)
    if account:
        where.append("t.account = ?")
        params.append(account)
    if tag:
        where.append("t.id IN (SELECT transaction_id FROM transaction_tags WHERE tag = ?)")
        params.append(repo.normalize_tag(tag))
    if dates:
        where.append("t.date BETWEEN ? AND ?")
        params += list(dates)
    elif month:
        first, last = periods.month_bounds(month)
        where.append("t.date BETWEEN ? AND ?")
        params += [first, last]
    if category:
        if category == "(sin categoría)":
            where.append("t.category IS NULL")
        else:
            where.append("t.category = ?")
            params.append(category)
    if flow:
        where.append("t.flow_type = ?")
        params.append(flow)
    if key:
        where.append("t.concept_clean = ?")
        params.append(key)
    if q:
        where.append("(t.concept LIKE ? OR t.concept_clean LIKE ?)")
        params += [f"%{q.upper()}%", f"%{q.upper()}%"]
    return (("WHERE " + " AND ".join(where)) if where else ""), params


def transactions_all(conn: sqlite3.Connection, *, month=None, category=None, flow=None, key=None,
                     q=None, account=None, tag=None, dates=None, only_ids=None) -> list[sqlite3.Row]:
    """Todas las filas del filtro, en orden cronológico (para exportar)."""
    sql_where, params = _transactions_where(month, category, flow, key, q, account, tag, dates, only_ids)
    return conn.execute(
        f"""
        SELECT t.id, t.date, t.concept, t.concept_clean, t.amount, t.balance, t.category, t.subcategory,
               t.flow_type, t.confidence, t.account, m.source
        FROM transactions t LEFT JOIN merchants m ON m.key = t.concept_clean
        {sql_where}
        ORDER BY t.date, t.id
        """,
        params,
    ).fetchall()


def transactions_totals(conn: sqlite3.Connection, *, month=None, category=None, flow=None, key=None, q=None,
                        account=None, tag=None, dates=None, only_ids=None) -> dict:
    """Ingresos, gastos y neto de TODO el filtro (no solo de la página visible)."""
    sql_where, params = _transactions_where(month, category, flow, key, q, account, tag, dates, only_ids)
    ph = ",".join("?" for _ in EXPENSE_FLOWS)
    row = conn.execute(
        f"""
        SELECT COALESCE(SUM(CASE WHEN t.flow_type = 'income' THEN t.amount END), 0) AS income,
               COALESCE(-SUM(CASE WHEN t.flow_type IN ({ph}) THEN t.amount END), 0) AS expenses,
               COUNT(*) AS n
        FROM transactions t {sql_where}
        """,
        [*EXPENSE_FLOWS, *params],
    ).fetchone()
    return {"income": float(row["income"]), "expenses": float(row["expenses"]),
            "net": float(row["income"]) - float(row["expenses"]), "n": int(row["n"])}

