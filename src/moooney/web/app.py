"""Aplicación web local de moooney: FastAPI + Jinja + HTMX.

Toda la funcionalidad de la CLI, desde el navegador y en local (127.0.0.1).
Las páginas leen la misma base SQLite; las acciones largas corren como trabajos
en segundo plano (ver jobs.py) y las ediciones (categoría, presupuesto, alerta
vista) responden con fragmentos HTML que HTMX intercambia en la página.
"""

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from moooney.analytics import alerts as alerts_mod
from moooney.analytics import budgets as budgets_mod
from moooney.analytics import forecast as forecast_mod
from moooney.analytics import insights as insights_mod
from moooney.analytics import periods
from moooney.analytics.metrics import full_months_before_safe
from moooney.analytics.recurring import load_recurring
from moooney.config import Settings, load_settings
from moooney.db import repo
from moooney.llm.client import offline_enabled
from moooney.web import queries, service
from moooney.web.jobs import JobRegistry
from moooney.web.service import Paths

HERE = Path(__file__).resolve().parent


@dataclass
class AppState:
    settings: Settings
    paths: Paths
    jobs: JobRegistry


def eur(value: float | None, decimals: int = 0) -> str:
    if value is None:
        return "—"
    s = f"{value:,.{decimals}f}"
    # formato es-ES: punto de miles, coma decimal
    s = s.replace(",", "\u0000").replace(".", ",").replace("\u0000", ".")
    return f"{s} €"


def pct(value: float | None, decimals: int = 0) -> str:
    return "—" if value is None else f"{value * 100:.{decimals}f} %"


def create_app(config_path: Path | None = None) -> FastAPI:
    settings = load_settings(config_path)
    config_dir = config_path.parent if config_path else Path("config")
    state = AppState(settings=settings, paths=Paths(config_dir), jobs=JobRegistry())

    app = FastAPI(title="moooney", docs_url=None, redoc_url=None)
    app.state.moooney = state
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["eur"] = eur
    templates.env.filters["pct"] = pct

    def get_db() -> Iterator[sqlite3.Connection]:
        conn = repo.connect(state.settings.paths.db_path, check_same_thread=False)
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
            "as_of": periods.as_of_date(conn),
            "job_running": state.jobs.current(),
            "offline": offline_enabled(),
        }
        return templates.TemplateResponse(request, template, {**base, **ctx})

    def taxonomy():
        return service.taxonomy_for(state.paths)

    def resolve_month(conn: sqlite3.Connection, month: str | None) -> str | None:
        as_of = periods.as_of_date(conn)
        if as_of is None:
            return None
        return month or periods.month_of(as_of)

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
        limits_cfg = budgets_mod.load_budgets_config(state.paths.budgets)
        budget_rows = []
        if limits_cfg:
            limits = budgets_mod.limits_for_month(conn, m, limits_cfg)
            budget_rows = budgets_mod.budget_status(conn, m, limits, as_of=report.as_of)
        return render(
            request, conn, "dashboard.html",
            month=m, prev_month=periods.prev_month(m), next_month=periods.next_month(m),
            report=report, top=queries.top_merchants(conn, m),
            chart_months=series_months,
            chart_series=[{"label": c, "data": v} for c, v in top_cats]
            + ([{"label": "Resto", "data": resto}] if any(resto) else []),
            chart_flows={
                "ingresos": [s.income for s in report.series],
                "gastos": [s.expenses for s in report.series],
                "ahorro": [s.net for s in report.series],
            },
            budget_rows=budget_rows[:6],
            pending=alerts_mod.pending_alerts(conn)[:5],
            insights=insights[:7],
        )

    # ---------- tendencias y previsión ----------

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
        history = full_months_before_safe(conn, month, 6, as_of=as_of, first=first)
        avg_net = sum(h.net for h in history) / len(history) if history else 0.0
        return render(
            request, conn, "trends.html", as_of=as_of, category=category, categories=categories,
            months12=months12, cat_values=values, cat_avg=avg, table=table,
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
                     flow: str | None = None, key: str | None = None, q: str | None = None, page: int = 1):
        pg = queries.transactions_page(
            conn, month=month or None, category=category or None, flow=flow or None,
            key=key or None, q=q or None, page=page,
        )
        total = sum(float(r["amount"]) for r in pg.rows)
        return render(
            request, conn, "transactions.html", page=pg, month=month or "", category=category or "",
            flow=flow or "", key=key or "", q=q or "", page_total=total, taxonomy=taxonomy(),
        )

    # ---------- comercios ----------

    @app.get("/merchants", response_class=HTMLResponse)
    def merchants(request: Request, conn: DB, mode: str = "dudosas", q: str | None = None,
                  category: str | None = None, page: int = 1):
        pg = queries.merchants_page(conn, mode=mode, q=q or None, category=category or None, page=page)
        return render(
            request, conn, "merchants.html", page=pg, mode=mode, q=q or "", category=category or "",
            summary=queries.merchants_summary(conn), taxonomy=taxonomy(),
        )

    @app.post("/merchants/{key:path}/category", response_class=HTMLResponse)
    def merchant_set_category(request: Request, conn: DB, key: str,
                              category: Annotated[str, Form()], flow_type: Annotated[str, Form()] = ""):
        try:
            n = service.set_merchant_category(conn, taxonomy(), key, category, flow_type or None)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from None
        row = queries.merchant_row(conn, key)
        return templates.TemplateResponse(
            request, "partials/merchant_row.html",
            {"request": request, "m": row, "taxonomy": taxonomy(), "saved": n},
        )

    # ---------- recurrentes ----------

    @app.get("/recurring", response_class=HTMLResponse)
    def recurring(request: Request, conn: DB, all: int = 0):
        service.refresh_recurring(conn)
        rows = load_recurring(conn, active_only=not all)
        as_of = periods.as_of_date(conn)
        upcoming = sorted(
            (r for r in rows if r["active"] and r["flow_type"] != "income"), key=lambda r: r["next_expected"]
        )[:8]
        monthly_fixed = sum(
            -float(r["amount_avg"]) * (30.0 / r["period_days"])
            for r in rows if r["active"] and r["flow_type"] in ("expense", "card_settlement")
        )
        return render(request, conn, "recurring.html", rows=rows, all=all, upcoming=upcoming,
                      monthly_fixed=monthly_fixed, as_of=as_of)

    # ---------- presupuestos ----------

    def budget_context(conn: sqlite3.Connection, month: str):
        as_of = periods.as_of_date(conn)
        limits_cfg = budgets_mod.load_budgets_config(state.paths.budgets)
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
        return render(
            request, conn, "budgets.html", month=m, prev_month=periods.prev_month(m),
            next_month=periods.next_month(m), rows=rows, has_config=bool(limits_cfg),
            sin_limite=sin_limite, total_limit=sum(r.limit for r in rows),
            total_spent=sum(r.spent for r in rows),
        )

    @app.post("/budgets/propose")
    def budgets_propose(conn: DB, months: Annotated[int, Form()] = 6, cut: Annotated[float, Form()] = 0.10):
        service.propose_and_save_budgets(conn, state.paths, months=months, cut=cut)
        return RedirectResponse("/budgets", status_code=303)

    @app.post("/budgets/{category}", response_class=HTMLResponse)
    def budget_set(request: Request, conn: DB, category: str, amount: Annotated[float, Form()],
                   month: Annotated[str, Form()] = ""):
        service.set_budget(conn, state.paths, category, amount)
        m = resolve_month(conn, month or None)
        rows, _ = budget_context(conn, m)
        row = next((r for r in rows if r.category == category), None)
        if row is None:
            return HTMLResponse("")
        return templates.TemplateResponse(
            request, "partials/budget_row.html", {"request": request, "r": row, "month": m, "saved": True}
        )

    # ---------- alertas ----------

    @app.get("/alerts", response_class=HTMLResponse)
    def alerts(request: Request, conn: DB, all: int = 0):
        if all:
            rows = conn.execute(
                "SELECT * FROM alerts ORDER BY acknowledged, CASE severity WHEN 'critical' THEN 0"
                " WHEN 'warning' THEN 1 ELSE 2 END, date DESC"
            ).fetchall()
        else:
            rows = alerts_mod.pending_alerts(conn)
        return render(request, conn, "alerts.html", rows=rows, all=all)

    @app.post("/alerts/refresh")
    def alerts_refresh(conn: DB):
        service.refresh_alerts(conn, state.paths)
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/ack-all")
    def alerts_ack_all(conn: DB):
        alerts_mod.acknowledge(conn)
        return RedirectResponse("/alerts", status_code=303)

    @app.post("/alerts/{alert_id}/ack")
    def alert_ack(conn: DB, alert_id: int):
        alerts_mod.acknowledge(conn, alert_id)
        return Response(status_code=200)

    # ---------- pipeline ----------

    def pipeline_ctx(conn: sqlite3.Connection) -> dict:
        enr = service.enrichment_settings_or_none(state.paths)
        llm = service.llm_settings_or_none(state.paths)
        return {
            "status": repo.get_status(conn),
            "files": queries.excel_files(conn, state.settings.paths.excel_dir),
            "imports": queries.imports_history(conn),
            "merchants": queries.merchants_summary(conn),
            "egress": queries.egress_stats(conn),
            "enrichment": enr,
            "llm": llm,
            "jobs": state.jobs.recent(),
            "excel_dir": state.settings.paths.excel_dir,
        }

    @app.get("/pipeline", response_class=HTMLResponse)
    def pipeline(request: Request, conn: DB):
        return render(request, conn, "pipeline.html", **pipeline_ctx(conn))

    JOB_NAMES = {
        "ingest": "Importar Excel nuevos",
        "categorize": "Categorizar pendientes",
        "categorize-refresh": "Reaplicar reglas",
        "categorize-rebuild": "Reconstruir claves",
        "enrich": "Enriquecer dudosas por web",
        "alerts": "Recalcular recurrentes y alertas",
    }

    @app.post("/pipeline/jobs/{name}", response_class=HTMLResponse)
    def job_start(request: Request, name: str):
        if name not in JOB_NAMES:
            raise HTTPException(status_code=404)
        s, p = state.settings, state.paths
        fns = {
            "ingest": lambda log: service.job_ingest(s, log),
            "categorize": lambda log: service.job_categorize(s, p, log),
            "categorize-refresh": lambda log: service.job_categorize(s, p, log, refresh=True),
            "categorize-rebuild": lambda log: service.job_categorize(s, p, log, rebuild=True, refresh=True),
            "enrich": lambda log: service.job_enrich(s, p, log),
            "alerts": lambda log: service.job_alerts(s, p, log),
        }
        try:
            job = state.jobs.start(JOB_NAMES[name], fns[name])
        except RuntimeError as e:
            return HTMLResponse(f'<div class="notice warn">{e}</div>')
        return templates.TemplateResponse(request, "partials/job.html", {"request": request, "job": job})

    @app.get("/pipeline/jobs/{job_id}", response_class=HTMLResponse)
    def job_status(request: Request, job_id: str):
        job = state.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(request, "partials/job.html", {"request": request, "job": job})

    @app.get("/pipeline/enrich/preview", response_class=HTMLResponse)
    def enrich_preview(request: Request, conn: DB):
        items = service.enrich_preview(conn, state.paths)
        return templates.TemplateResponse(
            request, "partials/enrich_preview.html", {"request": request, "items": items}
        )

    @app.post("/pipeline/enrichment/toggle")
    def enrichment_toggle(enabled: Annotated[int, Form()] = 0):
        service.set_enrichment_enabled(state.paths, bool(enabled))
        return RedirectResponse("/pipeline", status_code=303)

    return app


def app_from_env() -> FastAPI:
    """Fábrica para `uvicorn --factory` (modo --reload): lee MOOONEY_CONFIG si existe."""
    import os

    cfg = os.environ.get("MOOONEY_CONFIG")
    return create_app(Path(cfg) if cfg else None)
