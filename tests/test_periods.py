"""Periodos (mes, trimestre, año, rango), su resumen y las siete secciones. Movimientos inventados."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from moooney.analytics import periods
from moooney.analytics.period_summary import period_summary
from moooney.chat import agent, tools
from moooney.db import repo
from moooney.demo import build_demo
from moooney.web.app import SECTIONS, create_app, section_of
from tests.test_analytics import _tx

REPO_CONFIG = Path(__file__).resolve().parent.parent / "config"


def test_claves_de_periodo():
    q = periods.parse_period("2026-Q1")
    assert (q.kind, q.start, q.end, q.months) == ("quarter", "2026-01-01", "2026-03-31", ["2026-01", "2026-02", "2026-03"])
    assert periods.shift_period(q, -1).key == "2025-Q4" and periods.shift_period(q, 4).key == "2027-Q1"
    assert periods.parse_period("2024").end == "2024-12-31" and periods.shift_period(periods.parse_period("2024"), 1).key == "2025"
    assert periods.parse_period("2026-02").end == "2026-02-28"
    r = periods.parse_period("2026-03-10..2026-01-15")  # en cualquier orden
    assert (r.kind, r.start, r.end, r.days) == ("range", "2026-01-15", "2026-03-10", 55)
    antes = periods.shift_period(r, -1)
    assert antes.end == "2026-01-14" and antes.days == 55
    assert periods.period_containing("quarter", "2026-11-12").key == "2026-Q4"
    for basura in ("", None, "2026-13", "2026-Q5", "ayer", "2026-02-30..2026-03-01"):
        assert periods.parse_period(basura) is None


@pytest.fixture()
def db(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    rows = []
    for m in range(1, 9):  # enero a agosto de 2025; agosto, a medias (hasta el día 10)
        rows += [(f"2025-{m:02d}-01", "NOMINA", 2000.0, "Ingresos", "income"),
                 (f"2025-{m:02d}-02", "ALQUILER FALSO", -600.0, "Vivienda", "expense")]
        if m < 8:
            rows.append((f"2025-{m:02d}-20", "CINE FALSO", -10.0 * m, "Ocio", "expense"))
    rows.append(("2025-08-10", "VIAJE FALSO", -300.0, "Viajes", "expense"))
    _tx(conn, rows)
    return conn


def test_trimestre_cerrado_frente_al_anterior(db):
    ps = period_summary(db, periods.parse_period("2025-Q2"))
    assert not ps.partial and ps.cutoff == "2025-06-30" and ps.n_tx == 9
    assert ps.totals.income == 6000 and ps.totals.expenses == pytest.approx(1800 + 40 + 50 + 60)
    assert ps.prev.key == "2025-Q1" and ps.prev_totals.expenses == pytest.approx(1800 + 10 + 20 + 30)
    cats = {c.category: c for c in ps.categories}
    assert cats["Ocio"].delta == pytest.approx(90) and cats["Vivienda"].delta == 0
    assert [m.month for m in ps.months] == ["2025-04", "2025-05", "2025-06"] and all(m.full for m in ps.months)
    assert ps.costliest.month == "2025-06" and ps.cheapest.month == "2025-04"
    assert ps.merchants[0]["key"] == "ALQUILER FALSO"


def test_trimestre_en_curso_corta_el_anterior_a_la_misma_altura(db):
    ps = period_summary(db, periods.parse_period("2025-Q3"))
    assert ps.partial and ps.cutoff == "2025-08-10"
    # 41 días de trimestre: el anterior cuenta hasta el 11 de mayo (alquiler de abril y mayo, cine de abril)
    assert ps.prev_cutoff == "2025-05-11" and ps.prev_totals.expenses == pytest.approx(1200 + 40)
    assert not ps.months[-1].full and ps.months[-1].month == "2025-08"


def test_sin_periodo_anterior_completo_no_compara(db):
    ps = period_summary(db, periods.parse_period("2025-Q1"))
    assert ps.prev is None and ps.prev_totals is None and all(c.prev is None for c in ps.categories)
    assert period_summary(db, periods.parse_period("2024-Q1")) is None  # sin datos dentro
    libre = period_summary(db, periods.parse_period("2025-02-15..2025-03-15"))
    assert libre.totals.expenses == pytest.approx(20 + 600) and not any(m.full for m in libre.months)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=14)
    with TestClient(create_app(settings_path)) as c:
        yield c


def _ultimo_mes(client) -> str:
    import re

    return max(re.findall(r'<option value="/\?month=(\d{4}-\d{2})"', client.get("/").text))


def test_resumen_por_trimestre_anio_y_rango(client):
    mes = _ultimo_mes(client)
    q = periods.period_containing("quarter", f"{mes}-01")
    r = client.get(f"/?period={q.key}")
    assert r.status_code == 200 and f"T{q.key[-1]} {q.key[:4]}" in r.text
    assert "Por categoría" in r.text and "Mes a mes" in r.text and "Dónde más se gasta" in r.text
    assert "es el último periodo con datos" in r.text and 'aria-label="periodo anterior"' in r.text
    assert f"/transactions?month={q.key}&amp;category=" in r.text or f"/transactions?month={q.key}&category=" in r.text
    # fuera de los datos vuelve dentro; basura, al último mes
    assert "es el último periodo con datos" in client.get("/?period=2099-Q1").text
    assert "no hay datos anteriores" in client.get("/?period=1999").text
    assert "Diagnóstico" in client.get("/?period=basura").text
    anio = client.get(f"/?period={mes[:4]}")
    assert anio.status_code == 200 and f"<h1>{mes[:4]}</h1>" in anio.text
    libre = client.get(f"/?start={mes}-01&end={mes}-05")
    assert libre.status_code == 200 and 'name="start"' in libre.text and "Por categoría" in libre.text
    # la dirección de la antigua página «Año» sigue funcionando
    viejo = client.get("/year", follow_redirects=False)
    assert viejo.status_code == 307 and viejo.headers["location"] == f"/?period={mes[:4]}"


def test_movimientos_por_periodo(client):
    mes = _ultimo_mes(client)
    q = periods.period_containing("quarter", f"{mes}-01")

    def n(params: str) -> int:
        import re

        return int(re.search(r"(\d+) movimientos ·", client.get("/transactions?" + params).text).group(1))

    por_meses = sum(n(f"month={m}") for m in q.months)
    assert n(f"month={q.key}") == por_meses > 0 and n(f"period={q.key}") == por_meses
    assert n(f"start={q.start}&end={q.end}") == por_meses
    assert 0 < n(f"start={mes}-01&end={mes}-03") < n(f"month={mes}")
    page = client.get(f"/transactions?month={q.key}").text
    assert f'<option value="{q.key}" selected' in page and f"T{q.key[-1]} {q.key[:4]} ×" in page
    csv = client.get(f"/transactions/export.csv?month={q.key}")
    assert csv.status_code == 200 and len(csv.text.strip().splitlines()) == por_meses + 1
    assert q.key in csv.headers["content-disposition"]


def test_siete_secciones_con_pestanas(client):
    assert [s[0] for s in SECTIONS] == ["Resumen", "Tendencias", "Movimientos", "Pregunta", "Plan", "Avisos", "Datos"]
    assert section_of("/")[0] == "Resumen" and section_of("/merchants/rules/preview")[0] == "Datos"
    home = client.get("/").text
    nav = home[home.index('<nav id="side-nav">'):home.index("</nav>")]
    assert nav.count("<a href=") == 7 and 'class="section-tabs"' not in home
    assert 'class="section-tabs"' not in client.get("/transactions").text
    assert 'class="section-tabs"' not in client.get("/budgets").text and section_of("/goals")[0] == "Plan"
    avisos = client.get("/alerts").text
    assert 'class="section-tabs"' not in avisos and 'id="recurrentes"' in avisos and "Calendario de cargos" in avisos
    viejo = client.get("/recurring?all=1", follow_redirects=False)
    assert viejo.status_code == 303 and viejo.headers["location"] == "/alerts?inactive=1#recurrentes"
    assert section_of("/recurring")[0] == "Avisos" and "Todos los recurrentes" in client.get("/alerts?inactive=1").text
    for path, seccion, pestana in [("/report", "Pregunta", "Informe mensual"),
                                   ("/merchants", "Datos", "Comercios"), ("/settings", "Datos", "Ajustes")]:
        t = client.get(path).text
        tabs = t[t.index('<nav class="section-tabs"'):]
        tabs = tabs[:tabs.index("</nav>")]
        assert f'aria-label="{seccion}"' in tabs and tabs.count('class="on"') == 1, path
        activa = tabs[tabs.index('class="on"'):]
        assert pestana in activa[:activa.index("</a>")], path


def test_el_asistente_consulta_periodos(tmp_path, monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings_path = build_demo(tmp_path / "demo", source_config=REPO_CONFIG, months=14)
    from moooney.config import load_settings

    conn = repo.connect(load_settings(settings_path).paths.db_path)
    ctx = tools.Context(conn)
    as_of = periods.as_of_date(conn)
    out = json.loads(agent.run_tool(ctx, "resumen_periodo", json.dumps({"periodo": as_of[:4]})))
    assert out["periodo"] == as_of[:4] and out["en_curso"] and out["totales"]["gastos"] > 0 and out["comercios"]
    libre = json.loads(agent.run_tool(ctx, "resumen_periodo", json.dumps({"desde": as_of[:8] + "01", "hasta": as_of})))
    assert libre["desde"] == as_of[:8] + "01" and libre["hasta"] == as_of
    assert "AAAA" in json.loads(agent.run_tool(ctx, "resumen_periodo", '{"periodo": "verano"}'))["error"]
    assert "no hay datos" in json.loads(agent.run_tool(ctx, "resumen_periodo", '{"periodo": "1999"}'))["error"]
    conn.close()


def test_explorador_del_resumen(client):
    import re

    def datos(url: str) -> dict:
        t = client.get(url).text
        assert 'id="chart-donut"' in t and "data-cat-group" in t and "cat-pick" in t
        return json.loads(re.search(r'<script id="explorer-data" type="application/json">(.*?)</script>', t, re.S).group(1))

    mes = _ultimo_mes(client)
    d = datos(f"/?month={mes}")
    assert d["kind"] == "month" and d["cats"] and d["prev"] and len(d["prev"][next(iter(d["prev"]))]) == d["prev_days"]
    # lo que suma el día a día es lo que enseña cada categoría
    for c in d["cats"]:
        assert sum(d["daily"][c]) == pytest.approx(d["amounts"][c], abs=0.01)
        assert len(d["daily"][c]) == d["days"] and not any(d["daily"][c][d["cutoff"]:])
    q = periods.period_containing("quarter", f"{mes}-01")
    p = datos(f"/?period={q.key}")
    assert p["kind"] == "period" and p["months"] == [m for m in q.months if m <= mes] and len(p["income"]) == len(p["months"])
    for c in p["cats"]:
        assert sum(p["by_month"][c]) == pytest.approx(p["amounts"][c], abs=0.01)
