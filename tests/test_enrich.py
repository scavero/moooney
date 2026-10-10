"""Fase 2.5: enriquecimiento externo con proveedores y deciders falsos. Sin red ni datos reales."""

import json
import sqlite3

import httpx
import pytest
import yaml
from typer.testing import CliRunner

from moooney.categorize.pipeline import KeyJob, build_questions
from moooney.categorize.taxonomy import load_taxonomy
from moooney.db import repo
from moooney.enrich.providers import (
    DuckDuckGoProvider,
    Enrichment,
    NominatimProvider,
    ProviderError,
    RateLimiter,
    build_providers,
)
from moooney.enrich.query import build_query, sanitize_summary
from moooney.enrich.runner import (
    build_enriched_state,
    run_enrichment,
    select_candidates,
)
from moooney.enrich.settings import EnrichmentSettings, load_enrichment_settings
from moooney.llm.anonymize import assert_safe_state
from moooney.llm.client import LLMError
from moooney.llm.decide import Answer

MINI_CATEGORIES = {
    "categories": {
        "Alimentacion": {
            "flow_type": "expense",
            "description": "Compra de comida",
            "subcategories": ["supermercado", "restaurantes"],
        },
        "Otros": {
            "flow_type": "expense",
            "description": "Lo que no encaja",
            "subcategories": ["otros"],
        },
        "Ingresos": {"flow_type": "income", "description": "Dinero que entra"},
    },
    "flow_types": {
        "expense": "Gasto",
        "income": "Ingreso",
        "transfer_internal": "Traspaso propio",
    },
}

SKIP = ["^BIZUM", "^TRANSFERENCIA", "^PAYPAL EUROPE"]


@pytest.fixture()
def taxonomy(tmp_path):
    path = tmp_path / "categories.yaml"
    path.write_text(yaml.safe_dump(MINI_CATEGORIES, allow_unicode=True), encoding="utf-8")
    return load_taxonomy(path)


def _settings(**overrides) -> EnrichmentSettings:
    base = {
        "enabled": True,
        "trigger": {
            "max_confidence": 0.6,
            "also_if_category": ["Otros"],
            "skip_flow_types": ["transfer_internal", "income"],
            "skip_patterns": SKIP,
        },
        "providers": ["nominatim"],
        "region_hint": "Galicia, España",
        "rate_limit_seconds": 0.0,
        "max_queries_per_run": 50,
        "max_summary_chars": 200,
    }
    base.update(overrides)
    return EnrichmentSettings.model_validate(base)


def _db(tmp_path, rows) -> sqlite3.Connection:
    """rows: (concept_clean, amount, category, confidence, source, flow_type)."""
    conn = repo.connect(tmp_path / "test.db")
    with conn:
        for i, (key, amount, category, conf, source, flow) in enumerate(rows):
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount,"
                " account, created_at, category, flow_type, confidence)"
                " VALUES (?, '2025-01-01', 'X', ?, ?, 'principal', '2025-01-01T00:00:00', ?, ?, ?)",
                (f"f{i}", key, amount, category, flow, conf),
            )
            conn.execute(
                "INSERT OR IGNORE INTO merchants (key, category, subcategory, flow_type, source,"
                " confidence, updated_at) VALUES (?, ?, 'otros', ?, ?, ?, '2025-01-01T00:00:00')",
                (key, category, flow, source, conf),
            )
    return conn


class FakeProvider:
    name = "fake"

    def __init__(self, results: dict[str, Enrichment | None], errors: set[str] = frozenset()):
        self.results = results
        self.errors = errors
        self.queries: list[str] = []

    def lookup(self, query: str) -> Enrichment | None:
        self.queries.append(query)
        if query in self.errors:
            raise ProviderError("fake: caído")
        return self.results.get(query)


class FakeDecider:
    def __init__(self, answers_by_key: dict[str, dict[str, Answer]], boom: set[str] = frozenset()):
        self.answers_by_key = answers_by_key
        self.boom = boom
        self.states: list[str] = []

    def decide(self, state: str, questions: dict) -> dict[str, Answer]:
        self.states.append(state)
        for key in self.boom:
            if key in state:
                raise LLMError("modelo caído")
        for key, answers in self.answers_by_key.items():
            if f"Concepto de un apunte: {key}." in state:
                return answers
        return {}


# ---------- consulta ----------


def test_build_query_limpia_y_anade_region():
    q = build_query("COMPRA CAFETERIA GATO AZUL 0042 SL", region_hint="Galicia, España")
    assert q == "CAFETERIA GATO AZUL Galicia, España"


def test_build_query_conserva_ciudad_conocida_sin_region():
    q = build_query("BAR PEPE VIGO", region_hint="Galicia, España")
    assert q == "BAR PEPE VIGO"


def test_build_query_descarta_vetadas_y_cortas():
    assert build_query("BIZUM ENVIADO", region_hint="G", skip_patterns=SKIP) is None
    assert build_query("PAYPAL EUROPE", region_hint="G", skip_patterns=SKIP) is None
    assert build_query("ZARA", region_hint="G") is None  # un solo token
    assert build_query("COMPRA EN", region_hint="G") is None  # solo genericos


def test_sanitize_summary_quita_telefonos_y_referencias():
    texto = "Cafetería Gato Azul, tel. 986 123 456, ref 123456789, abierta hasta las 22"
    limpio = sanitize_summary(texto, max_chars=500)
    assert "986" not in limpio and "123456789" not in limpio
    assert "Cafetería Gato Azul" in limpio and "22" in limpio
    assert len(sanitize_summary("x" * 1000, max_chars=10)) == 10


def test_estado_enriquecido_pasa_anonymize_aunque_el_resumen_traiga_telefono():
    job = KeyJob(key="CAFETERIA GATO AZUL", n=3, avg_amount=-4.2, gap_days=10)
    enr = Enrichment("nominatim", "q", "Cafetería Gato Azul tel 986123456 cafe", ["amenity/cafe"])
    state = build_enriched_state(job, enr, max_chars=200)
    assert_safe_state(state)
    assert "amenity/cafe" in state and "986123456" not in state


# ---------- seleccion de candidatas ----------


def test_select_candidates_respeta_disparador_y_cache(tmp_path):
    conn = _db(
        tmp_path,
        [
            ("CAFETERIA GATO AZUL", -4.0, "Otros", 0.4, "clef", "expense"),  # dudosa
            ("MERCADONA FALSA", -40.0, "Alimentacion", 1.0, "rule", "expense"),  # segura
            ("BIZUM ENVIADO", -20.0, "Otros", 0.3, "clef", "expense"),  # vetada (sin consulta)
            ("NOMINA", 2000.0, "Ingresos", 0.3, "clef", "income"),  # flow_type excluido
            ("TIENDA RARA", -9.0, "Otros", 0.2, "user", "expense"),  # user: nunca
        ],
    )
    cands = select_candidates(conn, _settings())
    by_key = {c.key: c for c in cands}
    assert set(by_key) == {"CAFETERIA GATO AZUL", "BIZUM ENVIADO"}
    assert by_key["CAFETERIA GATO AZUL"].query == "CAFETERIA GATO AZUL Galicia, España"
    assert by_key["BIZUM ENVIADO"].query is None

    repo.upsert_enrichment(
        conn, key="CAFETERIA GATO AZUL", provider="fake", query="q", summary="s",
        place_types=None, url=None, helped=1, fetched_at="2025-01-01T00:00:00",
    )
    assert [c.key for c in select_candidates(conn, _settings())] == ["BIZUM ENVIADO"]
    assert len(select_candidates(conn, _settings(), force=True)) == 2
    assert [c.key for c in select_candidates(conn, _settings(), only_key="BIZUM ENVIADO")] == [
        "BIZUM ENVIADO"
    ]


# ---------- ejecucion ----------


def _answers(cat: str, conf: float) -> dict[str, Answer]:
    return {
        "category": Answer(choice=cat, confidence=conf),
        "flow_type": Answer(choice="expense", confidence=0.9),
    }


def test_run_enrichment_mejora_cambia_registra_y_cachea(tmp_path, taxonomy):
    conn = _db(
        tmp_path,
        [
            ("CAFETERIA GATO AZUL", -4.0, "Otros", 0.4, "clef", "expense"),
            ("TIENDA SIN RESULTADO", -9.0, "Otros", 0.3, "clef", "expense"),
            ("TIENDA QUE PETA", -9.0, "Otros", 0.3, "clef", "expense"),
        ],
    )
    q_cafe = "CAFETERIA GATO AZUL Galicia, España"
    q_peta = "TIENDA QUE PETA Galicia, España"
    provider = FakeProvider(
        {q_cafe: Enrichment("fake", q_cafe, "Cafetería Gato Azul", ["amenity/cafe"])},
        errors={q_peta},
    )
    decider = FakeDecider({"CAFETERIA GATO AZUL": _answers("Alimentacion", 0.9)})
    settings = _settings()
    summary = run_enrichment(
        conn, settings, [provider], decider, taxonomy, build_questions(taxonomy),
        candidates=select_candidates(conn, settings),
    )
    assert summary.queried == 3 and summary.hits == 1
    assert summary.misses == 2 and summary.errors == 1  # sin resultado incluye los errores
    assert summary.improved == 1 and summary.changed_category == 1
    assert set(summary.still_low) == {"TIENDA SIN RESULTADO", "TIENDA QUE PETA"}

    m = repo.get_merchant(conn, "CAFETERIA GATO AZUL")
    assert m["category"] == "Alimentacion" and m["source"] == "clef_web"
    assert m["confidence"] == pytest.approx(0.9)
    tx = conn.execute(
        "SELECT category FROM transactions WHERE concept_clean = 'CAFETERIA GATO AZUL'"
    ).fetchone()
    assert tx["category"] == "Alimentacion"
    assert "Informacion externa (fake)" in decider.states[0]

    calls = conn.execute("SELECT provider, status FROM web_calls ORDER BY id").fetchall()
    assert sorted((c["provider"], c["status"]) for c in calls) == [
        ("fake", "error"), ("fake", "hit"), ("fake", "miss"),
    ]
    assert repo.get_enrichment(conn, "CAFETERIA GATO AZUL")["helped"] == 1
    assert repo.get_enrichment(conn, "TIENDA SIN RESULTADO")["helped"] == 0

    # Segunda ejecucion: todo en cache, el proveedor no se vuelve a llamar.
    provider.queries.clear()
    summary2 = run_enrichment(
        conn, settings, [provider], decider, taxonomy, build_questions(taxonomy),
        candidates=select_candidates(conn, settings),
    )
    assert summary2.candidates == 0 and provider.queries == []


def test_segunda_ronda_no_pisa_si_no_mejora(tmp_path, taxonomy):
    conn = _db(tmp_path, [("CAFETERIA GATO AZUL", -4.0, "Otros", 0.5, "clef", "expense")])
    q = "CAFETERIA GATO AZUL Galicia, España"
    provider = FakeProvider({q: Enrichment("fake", q, "algo", [])})
    decider = FakeDecider({"CAFETERIA GATO AZUL": _answers("Alimentacion", 0.45)})
    settings = _settings()
    summary = run_enrichment(
        conn, settings, [provider], decider, taxonomy, build_questions(taxonomy),
        candidates=select_candidates(conn, settings),
    )
    assert summary.improved == 0 and summary.still_low == ["CAFETERIA GATO AZUL"]
    m = repo.get_merchant(conn, "CAFETERIA GATO AZUL")
    assert m["category"] == "Otros" and m["source"] == "clef"
    assert repo.get_enrichment(conn, "CAFETERIA GATO AZUL")["helped"] == 0


def test_clef_web_nunca_pisa_user(tmp_path):
    conn = repo.connect(tmp_path / "t.db")
    repo.upsert_merchant(
        conn, key="K", category="Otros", subcategory="otros", flow_type="expense",
        source="user", confidence=1.0, updated_at="t",
    )
    assert not repo.upsert_merchant(
        conn, key="K", category="Alimentacion", subcategory=None, flow_type="expense",
        source="clef_web", confidence=0.99, updated_at="t",
    )
    assert repo.upsert_merchant(
        conn, key="K2", category="Otros", subcategory=None, flow_type="expense",
        source="clef", confidence=0.4, updated_at="t",
    )
    assert repo.upsert_merchant(
        conn, key="K2", category="Alimentacion", subcategory=None, flow_type="expense",
        source="clef_web", confidence=0.9, updated_at="t",
    )
    assert not repo.upsert_merchant(  # clef normal no pisa a clef_web
        conn, key="K2", category="Otros", subcategory=None, flow_type="expense",
        source="clef", confidence=0.95, updated_at="t",
    )


def test_modelo_caido_en_segunda_ronda_cuenta_como_error(tmp_path, taxonomy):
    conn = _db(tmp_path, [("CAFETERIA GATO AZUL", -4.0, "Otros", 0.5, "clef", "expense")])
    q = "CAFETERIA GATO AZUL Galicia, España"
    provider = FakeProvider({q: Enrichment("fake", q, "algo", [])})
    decider = FakeDecider({}, boom={"CAFETERIA GATO AZUL"})
    settings = _settings()
    summary = run_enrichment(
        conn, settings, [provider], decider, taxonomy, build_questions(taxonomy),
        candidates=select_candidates(conn, settings),
    )
    assert summary.errors == 1 and summary.improved == 0
    assert repo.get_enrichment(conn, "CAFETERIA GATO AZUL") is not None  # el resultado web se guarda


def test_limite_de_consultas_por_ejecucion(tmp_path, taxonomy):
    rows = [(f"TIENDA NUMERO {n}", -1.0, "Otros", 0.1, "clef", "expense") for n in "ABCDE"]
    conn = _db(tmp_path, rows)
    provider = FakeProvider({})
    settings = _settings(max_queries_per_run=2)
    summary = run_enrichment(
        conn, settings, [provider], None, taxonomy, build_questions(taxonomy),
        candidates=select_candidates(conn, settings),
    )
    assert summary.queried == 2 and len(provider.queries) == 2


# ---------- proveedores ----------


def test_rate_limiter_espera_lo_que_falta():
    esperas: list[float] = []
    reloj = iter([0.0, 0.0, 0.5, 1.5, 3.0, 3.0])
    limiter = RateLimiter(1.5, sleeper=esperas.append, clock=lambda: next(reloj))
    limiter.wait()  # primera: no espera
    limiter.wait()  # han pasado 0.5 → espera 1.0
    limiter.wait()  # han pasado 1.5 → no espera
    assert esperas == [pytest.approx(1.0)]


def test_offline_bloquea_proveedores(monkeypatch):
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    settings = _settings()
    limiter = RateLimiter(0)
    with pytest.raises(ProviderError):
        NominatimProvider(settings, limiter).lookup("BAR PEPE VIGO")
    with pytest.raises(ProviderError):
        DuckDuckGoProvider(settings, limiter, search_fn=lambda q: [{"title": "x"}]).lookup("q")


def test_nominatim_parsea_tipo_de_lugar():
    payload = [
        {
            "name": "Cafetería Gato Azul",
            "display_name": "Cafetería Gato Azul, Rúa Falsa, Vigo, Galicia",
            "category": "amenity",
            "type": "cafe",
            "osm_type": "node",
            "osm_id": 1,
        }
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "nominatim.openstreetmap.org"
        assert request.url.params["q"] == "CAFETERIA GATO AZUL VIGO"
        assert "moooney" in request.headers["user-agent"]
        return httpx.Response(200, json=payload)

    provider = NominatimProvider(_settings(), RateLimiter(0), transport=httpx.MockTransport(handler))
    enr = provider.lookup("CAFETERIA GATO AZUL VIGO")
    assert enr is not None
    assert enr.place_types == ["amenity/cafe"]
    assert "Cafetería Gato Azul" in enr.summary
    assert enr.url == "https://www.openstreetmap.org/node/1"

    vacio = NominatimProvider(
        _settings(), RateLimiter(0), transport=httpx.MockTransport(lambda r: httpx.Response(200, json=[]))
    )
    assert vacio.lookup("NADA DE NADA") is None

    roto = NominatimProvider(
        _settings(), RateLimiter(0), transport=httpx.MockTransport(lambda r: httpx.Response(503))
    )
    with pytest.raises(ProviderError):
        roto.lookup("X Y")


def test_duckduckgo_resume_resultados():
    provider = DuckDuckGoProvider(
        _settings(), RateLimiter(0),
        search_fn=lambda q: [
            {"title": "Gato Azul", "body": "Cafetería en Vigo", "href": "https://x"},
            {"title": "Otro", "body": "", "href": "https://y"},
        ],
    )
    enr = provider.lookup("q")
    assert enr is not None and enr.summary == "Gato Azul: Cafetería en Vigo | Otro"
    assert enr.url == "https://x" and enr.place_types == []


def test_build_providers_omite_los_que_no_tienen_credenciales(monkeypatch):
    monkeypatch.delenv("GOOGLE_PLACES_API_KEY", raising=False)
    settings = _settings(providers=["nominatim", "searxng", "duckduckgo", "google_places"])
    nombres = [p.name for p in build_providers(settings)]
    assert nombres == ["nominatim", "duckduckgo"]
    monkeypatch.setenv("GOOGLE_PLACES_API_KEY", "clave-falsa")
    settings = _settings(
        providers=["searxng", "google_places"], searxng_url="https://searx.local"
    )
    assert [p.name for p in build_providers(settings)] == ["searxng", "google_places"]


# ---------- configuracion y CLI ----------


def test_load_enrichment_settings_valida(tmp_path):
    path = tmp_path / "enrichment.yaml"
    path.write_text("enabled: false\nproviders: [nominatim]\n", encoding="utf-8")
    assert load_enrichment_settings(path).enabled is False
    path.write_text("providers: [bing]\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_enrichment_settings(path)


def _cli_project(tmp_path, monkeypatch, *, enabled: bool) -> sqlite3.Connection:
    """Proyecto mínimo en tmp_path para ejercitar la CLI sin red."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "settings.yaml").write_text(
        yaml.safe_dump(
            {
                "paths": {"db_path": "data/t.db"},
                "parser": {"columns": {c: [c] for c in
                                       ["date", "value_date", "concept", "amount", "balance"]}},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "config" / "categories.yaml").write_text(
        yaml.safe_dump(MINI_CATEGORIES, allow_unicode=True), encoding="utf-8"
    )
    (tmp_path / "config" / "enrichment.yaml").write_text(
        yaml.safe_dump({"enabled": enabled, "providers": ["nominatim"],
                        "trigger": {"skip_patterns": SKIP}}),
        encoding="utf-8",
    )
    conn = repo.connect(tmp_path / "data" / "t.db")
    with conn:
        conn.execute(
            "INSERT INTO transactions (fingerprint, date, concept, concept_clean, amount, account,"
            " created_at, category, flow_type, confidence) VALUES ('f1', '2025-01-01', 'X',"
            " 'CAFETERIA GATO AZUL', -4.0, 'principal', 't', 'Otros', 'expense', 0.4)"
        )
        conn.execute(
            "INSERT INTO merchants (key, category, subcategory, flow_type, source, confidence,"
            " updated_at) VALUES ('CAFETERIA GATO AZUL', 'Otros', 'otros', 'expense', 'clef', 0.4, 't')"
        )
    return conn


def test_cli_dry_run_lista_sin_enviar(tmp_path, monkeypatch):
    from moooney.cli import app

    conn = _cli_project(tmp_path, monkeypatch, enabled=False)
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")  # si algo intentara red, fallaría
    result = CliRunner().invoke(app, ["enrich", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "CAFETERIA GATO AZUL Galicia" in result.output
    assert "nominatim.openstreetmap.org" in result.output
    assert conn.execute("SELECT COUNT(*) AS n FROM web_calls").fetchone()["n"] == 0

    out = tmp_path / "reports" / "dry.txt"
    result = CliRunner().invoke(app, ["enrich", "--dry-run", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "CAFETERIA GATO AZUL" not in result.output  # a pantalla solo el conteo
    assert "CAFETERIA GATO AZUL Galicia" in out.read_text(encoding="utf-8")


def test_cli_desactivado_no_envia_nada(tmp_path, monkeypatch):
    from moooney.cli import app

    conn = _cli_project(tmp_path, monkeypatch, enabled=False)
    result = CliRunner().invoke(app, ["enrich"])
    assert result.exit_code == 1
    assert "desactivado" in result.output
    assert conn.execute("SELECT COUNT(*) AS n FROM web_calls").fetchone()["n"] == 0


def test_cli_activado_pero_offline_no_envia_nada(tmp_path, monkeypatch):
    from moooney.cli import app

    conn = _cli_project(tmp_path, monkeypatch, enabled=True)
    monkeypatch.setenv("MOOONEY_OFFLINE", "1")
    result = CliRunner().invoke(app, ["enrich"])
    assert result.exit_code == 1
    assert "OFFLINE" in result.output
    assert conn.execute("SELECT COUNT(*) AS n FROM web_calls").fetchone()["n"] == 0


def test_parse_clef_respuesta_enriquecida_sigue_siendo_json_normal():
    # La segunda ronda usa el mismo decider: el formato de respuesta no cambia.
    from moooney.llm.decide import parse_clef_response

    content = json.dumps({"answers": {"category": {"choice": "Alimentacion", "confidence": 0.8}}})
    assert parse_clef_response(content)["category"].choice == "Alimentacion"
