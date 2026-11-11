"""Aplicación web local de moooney: FastAPI + Jinja + HTMX.

Toda la funcionalidad de la CLI, desde el navegador y en local (127.0.0.1).
Las páginas leen la misma base SQLite; las acciones largas corren como trabajos
en segundo plano (ver jobs.py) y las ediciones (categoría, presupuesto, alerta
vista) responden con fragmentos HTML que HTMX intercambia en la página.
"""

import html
import os
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from moooney import envfile
from moooney.analytics import alerts as alerts_mod
from moooney.analytics import budgets as budgets_mod
from moooney.analytics import forecast as forecast_mod
from moooney.analytics import goals as goals_mod
from moooney.analytics import insights as insights_mod
from moooney.analytics import periods, yearly
from moooney.analytics import shared as shared_mod
from moooney.analytics.metrics import full_months_before_safe
from moooney.analytics.price_changes import detect_price_changes
from moooney.analytics.recurring import load_recurring
from moooney.categorize import web_rules
from moooney.categorize.rules import Rule
from moooney.chat import agent as chat_agent
from moooney.config import Settings, load_settings
from moooney.db import repo
from moooney.db.backup import list_backups
from moooney.export import export_filename, transactions_csv
from moooney.ingest.reset import CONFIRM_WORD
from moooney.ingest.upload import MAX_BYTES, save_upload
from moooney.llm.client import offline_enabled
from moooney.report import advice
from moooney.web import queries, service
from moooney.web.jobs import JobRegistry, stage
from moooney.web.service import Paths

HERE = Path(__file__).resolve().parent

# Etiquetas humanas para los identificadores internos. La interfaz está en castellano:
# nunca se enseña `card_settlement`, `clef_web` ni `anomalous_amount` al usuario.
FLOW_LABEL = {"expense": "gasto", "income": "ingreso", "transfer_internal": "traspaso",
              "investment": "inversión", "card_settlement": "tarjeta",
              "card_settlement_covered": "tarjeta (compras ya contadas)"}
SOURCE_LABEL = {"user": "corregido por ti", "rule": "regla", "clef": "modelo", "clef_web": "modelo + web",
                "embedding": "parecido", "llm": "segunda opinión"}
SEVERITY_LABEL = {"critical": "crítica", "warning": "aviso", "info": "dato"}
ALERT_TYPE_LABEL = {
    "budget_80": "presupuesto al 80 %", "budget_100": "presupuesto superado", "anomalous_amount": "importe anómalo",
    "duplicate_charge": "cargo duplicado", "new_recurring": "recurrente nuevo", "price_increase": "subida de precio",
    "bill_rising": "recibo al alza",
    "missing_income": "ingreso que no llega", "negative_balance": "saldo en negativo", "bank_fee": "comisión bancaria",
}
ALERT_EXPLAIN = {
    "budget_80": "Una categoría ha consumido el 80 % de su límite mensual antes de acabar el mes.",
    "budget_100": "Una categoría ha superado su límite mensual.",
    "anomalous_amount": "Un cargo muy por encima del histórico de ese comercio (mediana + 3 desviaciones robustas y más de 20 €).",
    "duplicate_charge": "Mismo comercio e importe en menos de 48 h: puede ser un cobro repetido.",
    "price_increase": "Una cuota fija que ha pasado a cobrarse más cara (al menos un 3 % y 50 céntimos), con lo que supone al año.",
    "bill_rising": "Un recibo variable (luz, agua…) cuyos tres últimos cargos promedian más de un 15 % y 5 € por encima de los anteriores.",
    "new_recurring": "Un cargo que empezó a repetirse hace menos de 90 días.",
    "missing_income": "Un ingreso recurrente que no llegó cuando tocaba.",
    "negative_balance": "Con los recurrentes previstos, el saldo quedaría en negativo antes del próximo ingreso.",
    "bank_fee": "Comisiones cobradas por el banco en los últimos 30 días.",
}
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre"]


@dataclass
class AppState:
    settings: Settings
    paths: Paths
    jobs: JobRegistry
    force_demo: bool = False
    env_sources: dict[str, str] | None = None  # de dónde salió cada clave (entorno o fichero)


@dataclass
class Active:
    """Lo que la petición actual usa de verdad: los datos del usuario o la demo."""

    settings: Settings
    paths: Paths
    demo: bool


def _real_is_empty(settings: Settings) -> bool:
    conn = repo.connect(settings.paths.db_path)
    try:
        return conn.execute("SELECT COUNT(*) AS n FROM transactions").fetchone()["n"] == 0
    finally:
        conn.close()


def demo_settings_path(settings: Settings, config_dir: Path) -> Path:
    """Ruta del settings.yaml de la demo, construyéndola si aún no existe."""
    from moooney.demo import build_demo

    target = settings.paths.db_path.parent / "demo"
    settings_path = target / "config" / "settings.yaml"
    if not (target / "moooney.db").exists() or not settings_path.exists():
        build_demo(target, source_config=config_dir)
    return settings_path


def eur(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return "—"
    s = f"{value:,.{decimals}f}"
    # formato es-ES: punto de miles, coma decimal
    s = s.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    return f"{s} €"


def pct(value: float | None, decimals: int = 0) -> str:
    return "—" if value is None else f"{value * 100:.{decimals}f} %"


def fecha(value: str | None) -> str:
    """'2026-10-06' → '6 oct'. La fecha ISO completa va en el title del elemento."""
    if not value or len(value) < 10:
        return value or "—"
    return f"{int(value[8:10])} {MESES[int(value[5:7]) - 1][:3]}"


DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


def fecha_larga(value: str | None) -> str:
    """'2026-10-10' → 'viernes 10 de octubre'. Cabecera de día en la tabla de movimientos."""
    if not value or len(value) < 10:
        return value or "—"
    d = date.fromisoformat(value[:10])
    return f"{DIAS[d.weekday()]} {d.day} de {MESES[d.month - 1]}"


def group_by_day(rows: list) -> list[dict]:
    """Agrupa filas ya ordenadas por fecha en bloques {date, rows, total} para la cabecera de día."""
    groups: list[dict] = []
    for t in rows:
        if not groups or groups[-1]["date"] != t["date"]:
            groups.append({"date": t["date"], "rows": [], "total": 0.0})
        groups[-1]["rows"].append(t)
        groups[-1]["total"] += float(t["amount"] or 0)
    return groups


def mes(value: str | None, short: bool = False) -> str:
    """'2026-10' → 'octubre 2026' (o 'oct 26')."""
    if not value or len(value) < 7:
        return value or "—"
    name = MESES[int(value[5:7]) - 1]
    return f"{name[:3]} {value[2:4]}" if short else f"{name} {value[:4]}"


def build_verdict(summary, insights, *, day: int | None = None) -> str:
    """Una frase en castellano que responde a la pregunta del mes. Determinista."""
    prefix = f"A día {day}, " if day and not summary.full_month else ""
    if summary.income > 0 and summary.net >= 0:
        base = f"has ahorrado el {summary.savings_rate * 100:.0f} % de lo que entró"
    elif summary.income > 0:
        base = f"has gastado {eur(-summary.net)} más de lo que entró"
    else:
        base = f"sin ingresos, has gastado {eur(summary.expenses)}"
    frase = (prefix + base) if prefix else base[0].upper() + base[1:]
    aviso = next((i for i in insights if i.severity in ("warn", "bad") and i.impact > 0), None)
    if aviso is not None:
        titulo = aviso.title.split(":")[0].rstrip(".")  # sin las cifras del detalle: frase corta
        frase += f"; {titulo[0].lower() + titulo[1:]}"
    return frase + "."


def create_app(config_path: Path | None = None, *, force_demo: bool = False, snapshot: bool = False) -> FastAPI:
    """App web. `snapshot` marca las páginas para la copia estática de la demo (ver web/snapshot.py)."""
    settings = load_settings(config_path)
    config_dir = config_path.parent if config_path else Path("config")
    # Claves: variable de entorno > data/.env (lo que se guarda en Ajustes) > ./.env
    env_path = envfile.data_env_path(settings.paths.db_path.parent)
    env_sources = envfile.load_env_files(settings.paths.db_path.parent)
    state = AppState(settings=settings, paths=Paths(config_dir), jobs=JobRegistry(), force_demo=force_demo,
                     env_sources=env_sources)

    app = FastAPI(title="moooney", docs_url=None, redoc_url=None)
    app.state.moooney = state
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["eur"] = eur
    templates.env.filters["pct"] = pct
    templates.env.filters["fecha"] = fecha
    templates.env.filters["fecha_larga"] = fecha_larga
    templates.env.filters["mes"] = mes
    templates.env.globals.update(
        FLOW_LABEL=FLOW_LABEL, SOURCE_LABEL=SOURCE_LABEL, SEVERITY_LABEL=SEVERITY_LABEL,
        ALERT_TYPE_LABEL=ALERT_TYPE_LABEL, ALERT_EXPLAIN=ALERT_EXPLAIN, snapshot=snapshot,
    )

    _demo_cache: dict[str, Active] = {}

    def active() -> Active:
        """Datos del usuario si existen; si la base real está vacía, la demo (marcada como tal)."""
        if not state.force_demo and not _real_is_empty(state.settings):
            return Active(state.settings, state.paths, demo=False)
        if "demo" not in _demo_cache:
            sp = demo_settings_path(state.settings, state.paths.config_dir)
            _demo_cache["demo"] = Active(load_settings(sp), Paths(sp.parent), demo=True)
        return _demo_cache["demo"]

    def get_db() -> Iterator[sqlite3.Connection]:
        conn = repo.connect(active().settings.paths.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    DB = Annotated[sqlite3.Connection, Depends(get_db)]

    def render(request: Request, conn: sqlite3.Connection, template: str, **ctx) -> HTMLResponse:
        base = {
            "request": request,
            "path": request.url.path,
            "months": queries.months_available(conn),
            "alerts_counts": queries.alerts_counts(conn),
            "merchants_counts": queries.merchants_summary(conn),
            "as_of": periods.as_of_date(conn),
            "job_running": state.jobs.current(),
            "job_latest": state.jobs.latest(),
            "offline": offline_enabled(),
            "demo_mode": active().demo,
            "excel_dir": state.settings.paths.excel_dir,
        }
        return templates.TemplateResponse(request, template, {**base, **ctx})

    def taxonomy():
        return service.taxonomy_for(active().paths)

    def resolve_month(conn: sqlite3.Connection, month: str | None) -> str | None:
        as_of = periods.as_of_date(conn)
        if as_of is None:
            return None
        return month or periods.month_of(as_of)

    def today_pct(conn: sqlite3.Connection, month: str) -> float:
        """Porcentaje del mes transcurrido (100 si el mes ya está cerrado)."""
        as_of = periods.as_of_date(conn)
        if not as_of or periods.month_of(as_of) != month:
            return 100.0
        return int(as_of[8:10]) / periods.days_in_month(month) * 100.0

    # ---------- resumen ----------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request, conn: DB, month: str | None = None):
        m = resolve_month(conn, month)
        if m is None:
            return render(request, conn, "empty.html")
        service.refresh_recurring(conn)
        insights, report = insights_mod.build_insights(conn, m)
        series_months = [s.month for s in report.series]
        cat_series = queries.category_series(conn, series_months)
        top_cats = list(cat_series.items())[:8]
        resto = [sum(v[i] for _, v in list(cat_series.items())[8:]) for i in range(len(series_months))]
        limits_cfg = budgets_mod.load_budgets_config(active().paths.budgets)
        budget_rows = []
        if limits_cfg:
            limits = budgets_mod.limits_for_month(conn, m, limits_cfg)
            budget_rows = budgets_mod.budget_status(conn, m, limits, as_of=report.as_of)
        first = periods.first_date(conn)
        fme = forecast_mod.forecast_month_end(conn, report.as_of, first=first) if m == periods.month_of(report.as_of) else None
        return render(
            request, conn, "dashboard.html",
            month=m, prev_month=periods.prev_month(m), next_month=periods.next_month(m),
            report=report, top=queries.top_merchants(conn, m, limit=8),
            chart_months=series_months,
            chart_series=[{"label": c, "data": v} for c, v in top_cats]
            + ([{"label": "Resto", "data": resto}] if any(resto) else []),
            chart_flows={
                "ingresos": [s.income for s in report.series],
                "gastos": [s.expenses for s in report.series],
                "ahorro": [s.net for s in report.series],
            },
            budget_rows=budget_rows[:6], today_pct=today_pct(conn, m),
            pending=alerts_mod.pending_alerts(conn)[:5],
            insights=insights[:7],
            potential=sum(i.impact for i in insights if i.impact > 0),
            fme=fme, balance=forecast_mod.last_balance(conn),
            runway=forecast_mod.runway_months(conn, report.as_of, first=first),
            accounts=repo.accounts(conn),
            verdict=build_verdict(report.summary, insights, day=int(report.as_of[8:10])),
            day=int(report.as_of[8:10]), days_in_month=periods.days_in_month(m),
        )

    # ---------- tendencias y previsión ----------

    @app.get("/year", response_class=HTMLResponse)
    def year_page(request: Request, conn: DB, year: int | None = None):
        years = yearly.years_available(conn)
        if not years:
            return render(request, conn, "empty.html")
        y = yearly.year_summary(conn, year if year in years else years[0])
        return render(request, conn, "year.html", y=y, years=years)

    @app.get("/trends", response_class=HTMLResponse)
    def trends(request: Request, conn: DB, category: str | None = None):
        as_of = periods.as_of_date(conn)
        if as_of is None:
            return render(request, conn, "empty.html")
        service.refresh_recurring(conn)
        first = periods.first_date(conn)
        month = periods.month_of(as_of)
        months12 = [*periods.last_n_months(month, 11), month]
        series = queries.category_series(conn, months12)
        categories = list(series) or ["Otros"]
        category = category if category in series else categories[0]
        values = series.get(category, [0.0] * 12)
        avg = [sum(values[max(0, i - 2): i + 1]) / len(values[max(0, i - 2): i + 1]) for i in range(len(values))]
        full = [periods.is_full_month(mm, as_of, first) for mm in months12]
        table = []
        for c, vals in series.items():
            complete = [v for v, ok in zip(vals, full, strict=True) if ok]
            last3 = complete[-3:]
            prev3 = complete[-6:-3]
            avg3 = sum(last3) / len(last3) if last3 else 0.0
            prev = sum(prev3) / len(prev3) if prev3 else 0.0
            trend = (avg3 - prev) / prev if prev > 0 else 0.0
            table.append((c, vals, avg3, trend))
        max_cell = max((v for _, vals, _, _ in table for v in vals), default=1.0) or 1.0
        history = full_months_before_safe(conn, month, 6, as_of=as_of, first=first)
        avg_net = sum(h.net for h in history) / len(history) if history else 0.0
        return render(
            request, conn, "trends.html", as_of=as_of, category=category, categories=categories,
            months12=months12, cat_values=values, cat_avg=avg, table=table, max_cell=max_cell,
            share=insights_mod.spend_share_12m(conn, as_of),
            fme=forecast_mod.forecast_month_end(conn, as_of, first=first),
            nxt=forecast_mod.forecast_next_months(conn, as_of, n=3, first=first),
            runway=forecast_mod.runway_months(conn, as_of, first=first),
            balance=forecast_mod.last_balance(conn),
            projection=forecast_mod.savings_projection(conn, as_of, months=12, first=first),
            avg_net=avg_net,
        )

    # ---------- movimientos ----------

    @app.get("/transactions", response_class=HTMLResponse)
    def transactions(request: Request, conn: DB, month: str | None = None, category: str | None = None,
                     flow: str | None = None, key: str | None = None, q: str | None = None,
                     account: str | None = None, tag: str | None = None, page: int = 1):
        filters = {"month": month or None, "category": category or None, "flow": flow or None,
                   "key": key or None, "q": q or None, "account": account or None, "tag": tag or None}
        pg = queries.transactions_page(conn, page=page, **filters)
        return render(
            request, conn, "transactions.html", page=pg, month=month or "", category=category or "",
            flow=flow or "", key=key or "", q=q or "", account=account or "", tag=tag or "",
            totals=queries.transactions_totals(conn, **filters), taxonomy=taxonomy(),
            accounts=repo.accounts(conn), tags=repo.tags_summary(conn),
            tags_by_id=repo.tags_for(conn, [int(t["id"]) for t in pg.rows]),
            day_groups=group_by_day(pg.rows),
        )

    def _tags_cell(request: Request, conn: sqlite3.Connection, tx_id: int):
        return templates.TemplateResponse(
            request, "partials/tags_cell.html",
            {"request": request, "tx_id": tx_id, "tags": repo.tags_for(conn, [tx_id]).get(tx_id, [])},
        )

    @app.post("/transactions/{tx_id}/tags", response_class=HTMLResponse)
    def tag_add(request: Request, conn: DB, tx_id: int, tag: Annotated[str, Form()] = ""):
        """Añade una etiqueta al movimiento y devuelve la celda de etiquetas."""
        if repo.add_tag(conn, tx_id, tag, now=datetime.now().isoformat(timespec="seconds")) is None and tag.strip():
            raise HTTPException(status_code=404, detail="movimiento no encontrado")
        return _tags_cell(request, conn, tx_id)

    @app.post("/transactions/{tx_id}/tags/remove", response_class=HTMLResponse)
    def tag_remove(request: Request, conn: DB, tx_id: int, tag: Annotated[str, Form()] = ""):
        repo.remove_tag(conn, tx_id, tag)
        return _tags_cell(request, conn, tx_id)

    @app.get("/transactions/export.csv")
    def transactions_export(conn: DB, month: str | None = None, category: str | None = None,
                            flow: str | None = None, key: str | None = None, q: str | None = None,
                            account: str | None = None, tag: str | None = None):
        """CSV de los movimientos del filtro actual (separador ;, UTF-8 con BOM para Excel)."""
        texto, _n = transactions_csv(
            conn, month=month or None, category=category or None, flow=flow or None,
            key=key or None, q=q or None, account=account or None, tag=tag or None,
        )
        nombre = export_filename(month=month or None, category=category or None)
        return Response(
            content="\ufeff" + texto, media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{nombre}"'},
        )

    # ---------- comercios ----------

    @app.get("/merchants", response_class=HTMLResponse)
    def merchants(request: Request, conn: DB, mode: str = "dudosas", q: str | None = None,
                  category: str | None = None, page: int = 1, skip: str = "", queue: str = ""):
        pg = queries.merchants_page(conn, mode=mode, q=q or None, category=category or None, page=page)
        skipped = [s for s in skip.split("|") if s]
        todas = queries.merchants_page(conn, mode="dudosas", per_page=500).rows
        # La cola enseña por defecto solo las que importan: ≥ 2 movimientos o ≥ 30 €. La cola larga
        # de claves de un solo cargo pequeño se resume en euros y se puede abrir con ?queue=all.
        importantes, cola_larga = queries.split_queue(todas)
        queue_rows = todas if queue == "all" else importantes
        pending = [r for r in queue_rows if r["key"] not in skipped]
        tx = taxonomy()
        current = pending[0] if pending else None
        suggestions: list[str] = []
        if current:
            # la categoría actual (si no es Otros) y después las más frecuentes en tu memoria de comercios
            base = [current["category"]] if current["category"] != "Otros" else []
            frecuentes = [r["category"] for r in queries.frequent_categories(conn, limit=6)]
            suggestions = [c for c in [*base, *frecuentes] if c in tx.categories and c != "Otros"]
            suggestions = list(dict.fromkeys(suggestions))[:3]
        return render(
            request, conn, "merchants.html", page=pg, mode=mode, q=q or "", category=category or "",
            summary=queries.merchants_summary(conn), taxonomy=tx,
            recent=queries.recent_for_key(conn, current["key"], limit=3) if current else [],
            open_table=bool(mode != "dudosas" or q or category or page > 1 or current is None),
            queue_current=current, queue_pos=len(queue_rows) - len(pending) + 1, queue_total=len(queue_rows),
            queue_suggestions=suggestions, skip=skip, queue_mode=queue,
            tail_n=len(cola_larga), tail_total=sum(abs(float(r["total"])) for r in cola_larga),
            web_rules=[{"rule": r, "how": how, "text": text} for r in web_rules.read(web_rules.path_for(active().paths.rules))
                       for how, text in [web_rules.describe(r.pattern)]],
            rule_msg=request.query_params.get("regla_ok", ""), rule_error=request.query_params.get("regla_error", ""),
        )

    @app.get("/merchants/rules/preview", response_class=HTMLResponse)
    def rule_preview(request: Request, conn: DB, text: str = "", mode: str = "empieza"):
        ctx: dict = {"request": request, "text": text}
        try:
            ctx["pv"] = web_rules.preview(conn, web_rules.build_pattern(text, mode))
        except ValueError as e:
            ctx["error"] = str(e).capitalize() + "."
        return templates.TemplateResponse(request, "partials/rule_preview.html", ctx)

    @app.post("/merchants/rules")
    def rule_add(conn: DB, text: Annotated[str, Form()], mode: Annotated[str, Form()],
                 target: Annotated[str, Form()]):
        tx = taxonomy()
        category, _, sub = target.partition("|")
        try:
            if category not in tx.categories or sub not in tx.categories[category].subcategories:
                raise ValueError("elige una categoría de la lista")
            rule = Rule(pattern=web_rules.build_pattern(text, mode), category=category, subcategory=sub,
                        flow_type=tx.default_flow_type(category))
        except ValueError as e:
            return RedirectResponse(f"/merchants?regla_error={quote(str(e).capitalize())}#tus-reglas", status_code=303)
        p = active().paths
        web_rules.add(web_rules.path_for(p.rules), rule)
        n = service.reapply_all_rules(conn, p)
        how, txt = web_rules.describe(rule.pattern)
        msg = (f"Regla guardada: {how} {txt} → {category}. "
               f"{n} {'clave cambiada' if n == 1 else 'claves cambiadas'} ahora; las nuevas se categorizarán así al importar.")
        return RedirectResponse(f"/merchants?regla_ok={quote(msg)}#tus-reglas", status_code=303)

    @app.post("/merchants/rules/delete")
    def rule_delete(pattern: Annotated[str, Form()]):
        ok = web_rules.remove(web_rules.path_for(active().paths.rules), pattern)
        how, txt = web_rules.describe(pattern)
        msg = f"Regla quitada: {how} {txt}." if ok else "Esa regla ya no estaba."
        return RedirectResponse(f"/merchants?regla_ok={quote(msg)}#tus-reglas", status_code=303)

    @app.post("/merchants/{key:path}/category", response_class=HTMLResponse)
    def merchant_set_category(request: Request, conn: DB, key: str,
                              category: Annotated[list[str], Form()], flow_type: Annotated[str, Form()] = ""):
        # La cola de dudosas manda un hidden vacío y el botón pulsado: vale el último valor no vacío.
        chosen = next((c for c in reversed(category) if c), "")
        try:
            n = service.set_merchant_category(conn, taxonomy(), key, chosen, flow_type or None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        row = queries.merchant_row(conn, key)
        return templates.TemplateResponse(
            request, "partials/merchant_row.html",
            {"request": request, "m": row, "taxonomy": taxonomy(), "saved": n},
        )

    # ---------- asistente ----------

    def chat_html(text: str) -> str:
        """Markdown del modelo a HTML, sin dejar pasar HTML propio de la respuesta."""
        import markdown as md

        return md.markdown(text.replace("&", "&amp;").replace("<", "&lt;"))

    def chat_ctx() -> dict:
        llm = service.llm_settings_or_none(active().paths)
        return {"has_llm": llm is not None and not offline_enabled(),
                "model_name": llm.models.get("redaccion", "—") if llm else "—"}

    @app.get("/chat", response_class=HTMLResponse)
    def chat_page(request: Request, conn: DB):
        msgs = [{"role": r["role"], "html": chat_html(r["content"]) if r["role"] == "assistant" and not r["error"]
                 else "", "text": r["content"], "error": bool(r["error"]),
                 "tools": (r["tools"] or "").split(",") if r["tools"] else []}
                for r in chat_agent.history(conn)]
        return render(request, conn, "chat.html", messages=msgs, suggestions=chat_agent.SUGGESTIONS,
                      **chat_ctx())

    @app.post("/chat", response_class=HTMLResponse)
    def chat_post(request: Request, conn: DB, question: Annotated[str, Form()] = ""):
        try:
            answer = service.chat_ask(conn, active().paths, question)
        except ValueError as e:
            return templates.TemplateResponse(request, "partials/chat_turn.html", {
                "question": None, "reply": {"error": True, "text": str(e).capitalize() + ".", "tools": []}})
        reply = {"error": bool(answer.error), "text": answer.error or answer.content,
                 "html": "" if answer.error else chat_html(answer.content),
                 "tools": list(dict.fromkeys(answer.tools_used))}
        return templates.TemplateResponse(request, "partials/chat_turn.html", {
            "question": " ".join(question.split()), "reply": reply})

    @app.post("/chat/clear")
    def chat_clear(conn: DB):
        chat_agent.clear(conn)
        return RedirectResponse("/chat", status_code=303)

    # ---------- metas ----------

    @app.get("/goals", response_class=HTMLResponse)
    def goals_page(request: Request, conn: DB):
        service.refresh_recurring(conn)
        base = goals_mod.baseline(conn)
        today = date.today().isoformat()
        goals = goals_mod.list_goals(conn)
        net = base.net if base else 0.0
        planned = [(g, goals_mod.plan(g, net, today)) for g in goals]
        sim = {"net": round(net, 2), "month": today[:7],
               "goals": [{"name": g.name, "missing": round(g.missing, 2)} for g in goals]}
        cut = [c for c in (base.categories[:8] if base else []) if c.variable >= 5]
        return render(request, conn, "goals.html", base=base, base_net=net, goals=planned, sim=sim,
                      cut_categories=cut, this_month=today[:7], goal_error=request.query_params.get("error", ""))

    def _goal_error(e: ValueError) -> RedirectResponse:
        return RedirectResponse(f"/goals?error={quote(str(e).capitalize() + '.')}", status_code=303)

    @app.post("/goals")
    def goal_add(conn: DB, name: Annotated[str, Form()], target: Annotated[float, Form()],
                 saved: Annotated[float, Form()] = 0.0, deadline: Annotated[str, Form()] = ""):
        try:
            goals_mod.add_goal(conn, name, target, saved, deadline or None)
        except ValueError as e:
            return _goal_error(e)
        return RedirectResponse("/goals", status_code=303)

    @app.post("/goals/{goal_id}/saved")
    def goal_saved(conn: DB, goal_id: int, saved: Annotated[float, Form()]):
        try:
            goals_mod.set_saved(conn, goal_id, saved)
        except ValueError as e:
            return _goal_error(e)
        return RedirectResponse("/goals", status_code=303)

    @app.post("/goals/{goal_id}/delete")
    def goal_delete(conn: DB, goal_id: int):
        goals_mod.delete_goal(conn, goal_id)
        return RedirectResponse("/goals", status_code=303)

    # ---------- gastos compartidos ----------

    @app.get("/shared", response_class=HTMLResponse)
    def shared_page(request: Request, conn: DB):
        links = shared_mod.linked(conn)
        recovered = sum(float(r["income_amount"]) for r in links)
        paid = {r["expense_id"]: -float(r["expense_amount"]) for r in links}
        return render(request, conn, "shared.html", suggestions=shared_mod.suggestions(conn), links=links,
                      recovered=recovered, net=sum(paid.values()) - recovered)

    @app.post("/shared/link")
    def shared_link(conn: DB, income_id: Annotated[int, Form()], expense_id: Annotated[int, Form()]):
        try:
            shared_mod.link(conn, income_id, expense_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        return RedirectResponse("/shared", status_code=303)

    @app.post("/shared/unlink")
    def shared_unlink(conn: DB, income_id: Annotated[int, Form()]):
        shared_mod.unlink(conn, income_id)
        return RedirectResponse("/shared", status_code=303)

    @app.post("/shared/dismiss")
    def shared_dismiss(conn: DB, income_id: Annotated[int, Form()]):
        shared_mod.dismiss(conn, income_id)
        return RedirectResponse("/shared", status_code=303)

    # ---------- recurrentes ----------

    @app.get("/recurring", response_class=HTMLResponse)
    def recurring(request: Request, conn: DB, all: int = 0):
        service.refresh_recurring(conn)
        rows = load_recurring(conn, active_only=not all)
        as_of = periods.as_of_date(conn)
        horizon = (date.fromisoformat(as_of) + timedelta(days=30)).isoformat() if as_of else None
        upcoming = sorted(
            (r for r in load_recurring(conn, active_only=True)
             if as_of and as_of < r["next_expected"] <= horizon),
            key=lambda r: r["next_expected"],
        )
        balance = forecast_mod.last_balance(conn) or 0.0
        running, calendar = balance, []
        for r in upcoming:
            running += float(r["amount_avg"])
            calendar.append((r, running))
        charges_total = -sum(float(r["amount_avg"]) for r in upcoming if r["flow_type"] != "income")
        monthly_fixed = sum(
            -float(r["amount_avg"]) * (30.0 / r["period_days"])
            for r in rows if r["active"] and r["flow_type"] in ("expense", "card_settlement")
        )
        price_up = {p.key: p for p in detect_price_changes(conn)}
        next_income = min((r["next_expected"] for r in upcoming if r["flow_type"] == "income"), default=None)
        return render(request, conn, "recurring.html", rows=rows, all=all, calendar=calendar, horizon=horizon,
                      charges_total=charges_total, charges_n=sum(1 for r in upcoming if r["flow_type"] != "income"),
                      balance=balance, after=balance - charges_total, monthly_fixed=monthly_fixed, as_of=as_of,
                      price_up=price_up, next_income=next_income)

    # ---------- presupuestos ----------

    def budget_context(conn: sqlite3.Connection, month: str):
        as_of = periods.as_of_date(conn)
        limits_cfg = budgets_mod.load_budgets_config(active().paths.budgets)
        limits = budgets_mod.limits_for_month(conn, month, limits_cfg)
        if as_of and month == periods.month_of(as_of) and limits_cfg:
            limits = limits_cfg
        rows = budgets_mod.budget_status(conn, month, limits, as_of=as_of) if limits else []
        return rows, limits_cfg

    @app.get("/budgets", response_class=HTMLResponse)
    def budgets(request: Request, conn: DB, month: str | None = None):
        m = resolve_month(conn, month)
        if m is None:
            return render(request, conn, "empty.html")
        rows, limits_cfg = budget_context(conn, m)
        tx = taxonomy()
        sin_limite = [c for c, spec in tx.categories.items()
                      if spec.flow_type in ("expense", "card_settlement") and c not in limits_cfg]
        as_of = periods.as_of_date(conn)
        current = as_of is not None and periods.month_of(as_of) == m
        return render(
            request, conn, "budgets.html", month=m, prev_month=periods.prev_month(m),
            next_month=periods.next_month(m), rows=rows, has_config=bool(limits_cfg),
            sin_limite=sin_limite, total_limit=sum(r.limit for r in rows),
            total_spent=sum(r.spent for r in rows),
            total_projected=sum((r.projected if r.projected is not None else r.spent) for r in rows),
            over_count=sum(1 for r in rows if r.projected is not None and r.projected > r.limit),
            today_pct=today_pct(conn, m), is_current=current,
            day=int(as_of[8:10]) if as_of else None, days_in_month=periods.days_in_month(m),
        )

    @app.post("/budgets/propose")
    def budgets_propose(conn: DB, months: Annotated[int, Form()] = 6, cut: Annotated[float, Form()] = 10.0):
        # el formulario pide el recorte en % (10); la analítica lo quiere en tanto por uno (0.10)
        service.propose_and_save_budgets(conn, active().paths, months=months, cut=cut / 100.0)
        return RedirectResponse("/budgets", status_code=303)

    @app.post("/budgets/{category}", response_class=HTMLResponse)
    def budget_set(request: Request, conn: DB, category: str, amount: Annotated[float, Form()],
                   month: Annotated[str, Form()] = ""):
        service.set_budget(conn, active().paths, category, amount)
        m = resolve_month(conn, month or None)
        rows, _ = budget_context(conn, m)
        row = next((r for r in rows if r.category == category), None)
        if row is None:
            # límite 0: la fila desaparece
            return HTMLResponse("")
        return templates.TemplateResponse(
            request, "partials/budget_row.html",
            {"request": request, "r": row, "month": m, "saved": True, "today_pct": today_pct(conn, m)},
        )

    # ---------- alertas ----------

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts(request: Request, conn: DB, all: int = 0, severity: str = ""):
        if all:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY acknowledged, CASE severity WHEN 'critical' THEN 0"
                " WHEN 'warning' THEN 1 ELSE 2 END, date DESC"
            ).fetchall()
        else:
            rows = alerts_mod.pending_alerts(conn)
        counts = {s: sum(1 for a in rows if a["severity"] == s) for s in ("critical", "warning", "info")}
        if severity:
            rows = [a for a in rows if a["severity"] == severity]
        groups: dict[str, list] = {}
        for a in rows:
            groups.setdefault(a["type"], []).append(a)
        return render(request, conn, "alerts.html", rows=rows, groups=groups, all=all, severity=severity,
                      counts=counts, total=sum(counts.values()))

    @app.post("/alerts/refresh")
    def alerts_refresh(conn: DB):
        service.refresh_alerts(conn, active().paths)
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/ack-all")
    def alerts_ack_all(conn: DB):
        alerts_mod.acknowledge(conn)
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/ack-type/{alert_type}")
    def alerts_ack_type(conn: DB, alert_type: str):
        with conn:
            conn.execute("UPDATE alerts SET acknowledged = 1 WHERE acknowledged = 0 AND type = ?", (alert_type,))
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/{alert_id}/ack")
    def alert_ack(conn: DB, alert_id: int):
        alerts_mod.acknowledge(conn, alert_id)
        return Response(status_code=200)

    # ---------- informe ----------

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request, conn: DB, month: str | None = None):
        import json

        import markdown as md

        m = resolve_month(conn, month)
        if m is None:
            return render(request, conn, "empty.html")
        service.refresh_recurring(conn)
        row = advice.latest_report(conn, m)
        html = md.markdown(row["content"], extensions=["tables"]) if row else ""
        llm = service.llm_settings_or_none(active().paths)
        limits = budgets_mod.load_budgets_config(active().paths.budgets)
        payload = advice.build_payload(conn, m, budgets_limits=limits or None)
        return render(
            request, conn, "report.html", month=m, prev_month=periods.prev_month(m),
            next_month=periods.next_month(m), report=row, report_html=html,
            has_llm=llm is not None, model_name=(llm.models.get("redaccion") if llm else "—"),
            payload_json=json.dumps(payload, ensure_ascii=False, indent=1),
        )

    @app.post("/report/generate", response_class=HTMLResponse)
    def report_generate(request: Request, month: str | None = None):
        a = active()
        s, p = a.settings, a.paths
        try:
            job = state.jobs.start(
                f"Informe {month or 'del mes'} con el modelo",
                lambda log: service.job_report(s, p, log, month=month, use_model=True),
            )
        except RuntimeError as e:
            return HTMLResponse(f'<div class="notice warn">{html.escape(str(e))}. Espera a que termine.</div>')
        return job_panel(request, job, started=True)

    @app.post("/report/generate-offline")
    def report_generate_offline(month: str | None = None):
        a = active()
        service.job_report(a.settings, a.paths, lambda _line: None, month=month, use_model=False)
        return RedirectResponse(f"/report?month={month}" if month else "/report", status_code=303)

    # ---------- pipeline ----------

    def pipeline_ctx(conn: sqlite3.Connection) -> dict:
        a = active()
        enr = service.enrichment_settings_or_none(a.paths)
        llm = service.llm_settings_or_none(a.paths)
        status = repo.get_status(conn)
        files = queries.excel_files(conn, state.settings.paths.excel_dir)
        merchants = queries.merchants_summary(conn)
        alerts_counts = queries.alerts_counts(conn)
        pending_files = sum(1 for f in files if not f.imported)
        # Pasos del flujo con su estado: la página dice de un vistazo qué pide acción.
        steps = [
            {"n": 1, "label": "Importar", "ok": pending_files == 0,
             "status": f"{pending_files} pendientes" if pending_files else "al día",
             "hint": f"{len(files)} ficheros en la carpeta",
             **({"cta": "Importar nuevos", "action": "ingest"} if pending_files else {"cta": "Subir Excel", "href": "#upload-form"})},
            {"n": 2, "label": "Categorizar", "ok": not status["uncategorized"],
             "status": f"{status['uncategorized']} sin categoría" if status["uncategorized"] else "al día",
             "hint": f"{merchants['total']} claves en memoria", "cta": "Categorizar", "action": "categorize"},
            {"n": 3, "label": "Revisar dudosas", "ok": not merchants["dudosas"],
             "status": f"{merchants['dudosas']} pendientes" if merchants["dudosas"] else "al día",
             "hint": "confianza < 0,7 u Otros", "cta": "Revisar", "href": "/merchants?mode=dudosas"},
            {"n": 4, "label": "Alertas", "ok": not alerts_counts["critical"],
             "status": f"{alerts_counts['total']} pendientes" if alerts_counts["total"] else "al día",
             "hint": "recalculadas con los datos actuales", "cta": "Ver alertas", "href": "/alerts"},
            {"n": 5, "label": "Informe", "ok": llm is not None and not offline_enabled(),
             "status": "con modelo" if llm is not None and not offline_enabled() else "sin modelo",
             "hint": "la versión determinista siempre está", "cta": "Ver informe", "href": "/report"},
        ]
        return {
            "status": status, "files": files, "imports": queries.imports_history(conn), "merchants": merchants,
            "egress": queries.egress_stats(conn), "enrichment": enr, "llm": llm, "jobs": state.jobs.recent(),
            "excel_dir": state.settings.paths.excel_dir, "demo": a.demo, "steps": steps, "reset_word": CONFIRM_WORD,
            "backups": list_backups(state.settings.paths.backups_dir),
        }

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request, conn: DB):
        return render(request, conn, "pipeline.html", **pipeline_ctx(conn))

    JOB_NAMES = {
        "ingest": "Importar Excel nuevos",
        "categorize": "Categorizar pendientes",
        "categorize-refresh": "Reaplicar reglas",
        "categorize-rebuild": "Reconstruir claves",
        "categorize-retry": "Reintentar dudosas con el decider",
        "second-opinion": "Segunda opinión del modelo de texto",
        "enrich": "Enriquecer dudosas por web",
        "alerts": "Recalcular recurrentes y alertas",
        "repair": "Revisar extractos solapados",
        "reset": "Empezar de cero",
        "backup": "Copia de seguridad",
    }

    def job_panel(request: Request, job, *, started: bool = False) -> HTMLResponse:
        """Tarjeta del trabajo. Avisa a la página por HX-Trigger: `job-started` al lanzarlo
        (aparece el indicador de la barra lateral) y `job-finished` en cuanto termina (la página
        recarga sus cifras sola, sin pulsar F5)."""
        resp = templates.TemplateResponse(request, "partials/job.html", {"request": request, "job": job})
        if started:
            resp.headers["HX-Trigger"] = "job-started"
        elif not job.running:
            resp.headers["HX-Trigger"] = "job-finished"
        return resp

    @app.post("/pipeline/jobs/{name}", response_class=HTMLResponse)
    def job_start(request: Request, name: str):
        if name not in JOB_NAMES:
            raise HTTPException(status_code=404)
        a = active()
        s, p = a.settings, a.paths
        real = state.settings  # la ingesta siempre carga los Excel del usuario en SU base
        fns = {
            "ingest": lambda log: service.job_ingest(real, log),
            "categorize": lambda log: service.job_categorize(s, p, log),
            "categorize-refresh": lambda log: service.job_categorize(s, p, log, refresh=True),
            "categorize-rebuild": lambda log: service.job_categorize(s, p, log, rebuild=True, refresh=True),
            "categorize-retry": lambda log: service.job_categorize(s, p, log, retry=True),
            "second-opinion": lambda log: service.job_categorize(s, p, log, second_opinion=True),
            "enrich": lambda log: service.job_enrich(s, p, log),
            "alerts": lambda log: service.job_alerts(s, p, log),
            "repair": lambda log: service.job_repair(real, s, p, log),
            "reset": lambda log: service.job_reset(real, log),
            "backup": lambda log: service.job_backup(real, log),
        }
        if name == "reset" and request.headers.get("HX-Prompt", "").strip().upper() != CONFIRM_WORD:
            # Borrado sin vuelta atrás: hay que escribir la palabra, no basta con un clic.
            return HTMLResponse(f'<div class="notice warn">No se ha borrado nada: para confirmarlo hay que '
                                f'escribir {CONFIRM_WORD}.</div>')
        try:
            job = state.jobs.start(JOB_NAMES[name], fns[name])
        except RuntimeError as e:
            return HTMLResponse(f'<div class="notice warn">{html.escape(str(e))}. Espera a que termine.</div>')
        return job_panel(request, job, started=True)

    @app.post("/pipeline/upload", response_class=HTMLResponse)
    async def pipeline_upload(request: Request, files: Annotated[list[UploadFile] | None, File()] = None):
        """Excel subidos desde el navegador: van a la carpeta de entrada y se importan en el acto."""
        real = state.settings  # como la ingesta: siempre a la carpeta y la base del usuario
        results = []
        for f in files or []:
            content = await f.read(MAX_BYTES + 1)
            results.append(save_upload(real.paths.excel_dir, f.filename or "", content, real.profiles))
        if not results:
            return HTMLResponse('<div class="notice warn">No ha llegado ningún fichero.</div>')
        lines = [f"{r.filename}: {r.message}" for r in results]
        if not any(r.ok for r in results):
            items = "".join(f"<li>{html.escape(line)}</li>" for line in lines)
            return HTMLResponse(f'<div class="notice crit"><strong>No se ha guardado nada.</strong><ul>{items}</ul></div>')
        a = active()
        s, p = a.settings, a.paths

        stages = ["Subir", "Importar movimientos"] + ([] if a.demo else ["Categorizar", "Recalcular alertas"])

        def run(log):
            for line in lines:
                log(line)
            stage(log, "Importar movimientos")
            service.job_ingest(real, log)
            if not a.demo:  # en la demo se importa a tu base, pero no se categoriza la de la demo
                stage(log, "Categorizar")
                service.job_categorize(s, p, log)
                stage(log, "Recalcular alertas")
                service.job_alerts(s, p, log)

        try:
            job = state.jobs.start("Importar Excel subidos", run, stages=stages)
        except RuntimeError as e:
            return HTMLResponse(f'<div class="notice warn">Ficheros guardados en la carpeta, pero {html.escape(str(e))}. '
                                'Pulsa «Importar Excel nuevos» cuando termine.</div>')
        return job_panel(request, job, started=True)

    @app.get("/pipeline/jobs/current", response_class=HTMLResponse)
    def job_indicator(request: Request, was: str | None = None):
        """Indicador de la barra lateral: se ve en todas las páginas mientras hay un trabajo. Si
        termina mientras miras otra página, avisa con un toast que lleva a Pipeline."""
        job = state.jobs.latest()
        resp = templates.TemplateResponse(request, "partials/job_indicator.html", {"request": request, "job": job})
        on_pipeline = "/pipeline" in request.headers.get("HX-Current-URL", "")
        if was == "running" and job is not None and not job.running and not on_pipeline:
            estado = "terminado" if job.status == "done" else "con error"
            msg = f'{html.escape(job.name)}: {estado}. <a href="/pipeline">Ver resultado →</a>'
            # Las cabeceras van en latin-1; el toast se pinta como HTML, así que basta con entidades.
            resp.headers["X-Toast"] = msg.encode("ascii", "xmlcharrefreplace").decode("ascii")
            resp.headers["X-Toast-Ms"] = "8000"
        return resp

    @app.get("/pipeline/jobs/{job_id}", response_class=HTMLResponse)
    def job_status(request: Request, job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return job_panel(request, job)

    @app.get("/pipeline/enrich/preview", response_class=HTMLResponse)
    def enrich_preview(request: Request, conn: DB):
        items = service.enrich_preview(conn, active().paths)
        return templates.TemplateResponse(
            request, "partials/enrich_preview.html", {"request": request, "items": items}
        )

    @app.post("/pipeline/enrichment/toggle")
    def enrichment_toggle(enabled: Annotated[int, Form()] = 0):
        service.set_enrichment_enabled(active().paths, bool(enabled))
        return RedirectResponse("/pipeline", status_code=303)

    # ---------- ajustes (claves y modo offline) ----------

    def source_label(name: str) -> str:
        src = (state.env_sources or {}).get(name)
        if src is None:
            return "—"
        if src == envfile.SOURCE_ENV:
            return "variable de entorno (Windows o docker compose): se cambia donde está definida"
        return f"fichero {src}"

    def key_info(name: str) -> dict:
        value = os.environ.get(name)
        src = (state.env_sources or {}).get(name)
        return {"set": bool(value), "hint": envfile.hint(value), "source_label": source_label(name),
                "removable": src is not None and src != envfile.SOURCE_ENV}

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, conn: DB, notice: str | None = None, kind: str | None = None):
        return render(
            request, conn, "settings.html",
            llm=service.llm_settings_or_none(state.paths), env_path=env_path,
            keys={n: key_info(n) for n in envfile.SECRET_NAMES},
            offline_source_label=source_label("MOOONEY_OFFLINE"),
            notice=notice, notice_kind=kind,
        )

    @app.post("/settings/key")
    def settings_key_save(name: Annotated[str, Form()], value: Annotated[str, Form()] = ""):
        if name not in envfile.SECRET_NAMES:
            raise HTTPException(status_code=404)
        value = value.strip()
        if not value:
            return RedirectResponse("/settings?notice=La+clave+estaba+vacía&kind=warn", status_code=303)
        envfile.save_env_value(env_path, name, value)
        if (state.env_sources or {}).get(name) == envfile.SOURCE_ENV:
            # La variable del sistema seguirá mandando en el próximo arranque; se avisa.
            return RedirectResponse(
                "/settings?notice=Guardada+en+el+fichero,+pero+la+variable+de+entorno+sigue+mandando&kind=warn",
                status_code=303)
        os.environ[name] = value
        state.env_sources = {**(state.env_sources or {}), name: str(env_path)}
        return RedirectResponse("/settings?notice=Clave+guardada", status_code=303)

    @app.post("/settings/key/remove")
    def settings_key_remove(name: Annotated[str, Form()]):
        if name not in envfile.SECRET_NAMES:
            raise HTTPException(status_code=404)
        envfile.remove_env_value(env_path, name)
        if (state.env_sources or {}).get(name) != envfile.SOURCE_ENV:
            os.environ.pop(name, None)
            state.env_sources = {k: v for k, v in (state.env_sources or {}).items() if k != name}
        return RedirectResponse("/settings?notice=Clave+quitada", status_code=303)

    @app.post("/settings/offline")
    def settings_offline(enabled: Annotated[int, Form()] = 0):
        valor = "1" if enabled else "0"
        envfile.save_env_value(env_path, "MOOONEY_OFFLINE", valor)
        os.environ["MOOONEY_OFFLINE"] = valor
        state.env_sources = {**(state.env_sources or {}), "MOOONEY_OFFLINE": str(env_path)}
        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/test", response_class=HTMLResponse)
    def settings_test(request: Request):
        result = service.test_llm_connection(state.paths)
        return templates.TemplateResponse(request, "partials/llm_test.html", {"request": request, "result": result})

    return app


def app_from_env() -> FastAPI:
    """Fábrica para `uvicorn --factory` (modo --reload): lee MOOONEY_CONFIG si existe."""
    import os

    cfg = os.environ.get("MOOONEY_CONFIG")
    return create_app(Path(cfg) if cfg else None, force_demo=os.environ.get("MOOONEY_FORCE_DEMO") == "1")
