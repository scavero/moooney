"""Datos de demostración: una base sintética para probar moooney sin datos reales.

`moooney demo` crea `data/demo/` con una configuración propia y ~14 meses de
movimientos inventados (nómina, alquiler, suministros con variación,
suscripciones, supermercado, restaurantes, combustible, bizums, inversión,
liquidación de tarjeta, alguna anomalía y una subida de precio). Sirve para
ver la web con algo dentro, para enseñar el proyecto y para los tests.
"""

import random
import shutil
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import yaml

from moooney.analytics.recurring import detect_recurring, store_recurring
from moooney.categorize.normalize import clean_concept
from moooney.db import repo

SEED = 20261011

# (concepto base, categoría, subcategoría, flujo, importe base, variación, veces/mes, fuente, confianza)
MERCHANTS = [
    ("NOMINA EMPRESA FICTICIA SL", "Ingresos", "nomina", "income", 2350.0, 0.0, 1, "rule", 1.0),
    ("RECIBO ALQUILER PISO CENTRO", "Vivienda", "alquiler", "expense", -780.0, 0.0, 1, "rule", 1.0),
    ("RECIBO IBERDROLA CLIENTES", "Vivienda", "suministros", "expense", -62.0, 0.35, 1, "rule", 1.0),
    ("RECIBO AGUAS DE LA VILLA", "Vivienda", "suministros", "expense", -28.0, 0.2, 1, "rule", 1.0),
    ("RECIBO MOVISTAR FIBRA", "Vivienda", "telefonia e internet", "expense", -44.9, 0.0, 1, "rule", 1.0),
    ("NETFLIX", "Suscripciones", "streaming", "expense", -12.99, 0.0, 1, "rule", 1.0),
    ("SPOTIFY", "Suscripciones", "streaming", "expense", -10.99, 0.0, 1, "rule", 1.0),
    ("GIMNASIO FORMA FICTICIA", "Ocio", "deporte", "expense", -34.0, 0.0, 1, "clef", 0.82),
    ("SUSCRIPCION FONDO INDEXADO GLOBAL", "Ahorro e inversion", "fondos", "investment", -250.0, 0.0, 1, "rule", 1.0),
    ("LIQUIDACION DE TARJETAS DE CREDITO", "Tarjeta de credito", "liquidacion", "card_settlement", -180.0, 0.5, 1, "rule", 1.0),
    ("PAGO MOVIL EN MERCADONA", "Alimentacion", "supermercado", "expense", -48.0, 0.6, 6, "rule", 1.0),
    ("PAGO MOVIL EN GADIS", "Alimentacion", "supermercado", "expense", -32.0, 0.6, 3, "rule", 1.0),
    ("PAGO MOVIL EN FROIZ", "Alimentacion", "supermercado", "expense", -21.0, 0.6, 2, "rule", 1.0),
    ("PAGO MOVIL EN CAFETERIA GATO AZUL", "Alimentacion", "restaurantes", "expense", -4.2, 0.4, 5, "clef", 0.71),
    ("PAGO MOVIL EN TASCA DO RIO", "Alimentacion", "restaurantes", "expense", -27.0, 0.5, 2, "clef_web", 0.88),
    ("PAGO MOVIL EN PIZZERIA LUNA", "Alimentacion", "restaurantes", "expense", -31.0, 0.3, 1, "clef", 0.64),
    ("PAGO MOVIL EN REPSOL", "Transporte", "combustible", "expense", -58.0, 0.3, 2, "rule", 1.0),
    ("PEAJE VIA-T AUTOPISTA", "Transporte", "peajes", "expense", -6.4, 0.2, 4, "rule", 1.0),
    ("PAGO MOVIL EN FARMACIA CENTRAL", "Salud", "farmacia", "expense", -14.0, 0.8, 1, "rule", 1.0),
    ("COMPRA EN AMAZON", "Compras", "online", "expense", -36.0, 0.9, 2, "rule", 1.0),
    ("PAGO MOVIL EN ZARA", "Compras", "ropa y calzado", "expense", -45.0, 0.7, 1, "rule", 1.0),
    ("BIZUM A FAVOR DE AMIGO", "Transferencias", "bizum enviado", "expense", -20.0, 0.8, 2, "rule", 1.0),
    ("BIZUM DE AMIGO", "Transferencias", "bizum recibido", "income", 15.0, 0.8, 1, "rule", 1.0),
    ("TRASPASO A HUCHA VACACIONES", "Transferencias", "traspaso interno", "transfer_internal", -100.0, 0.0, 1, "rule", 1.0),
    ("PAGO MOVIL EN TIENDA RARA XYZ", "Otros", "otros", "expense", -19.0, 0.5, 1, "clef", 0.31),
    ("PAGO MOVIL EN SERVICIOS OPACOS", "Otros", "otros", "expense", -42.0, 0.3, 1, "clef", 0.44),
    ("COMISION MANTENIMIENTO CUENTA", "Comisiones bancarias", "comisiones", "expense", -8.0, 0.0, 1, "rule", 1.0),
]


def _month_iter(start: date, months: int):
    y, m = start.year, start.month
    for _ in range(months):
        yield y, m
        m += 1
        if m == 13:
            y, m = y + 1, 1


def generate_transactions(months: int = 14, end: date | None = None) -> list[dict]:
    """Movimientos sintéticos ordenados por fecha, con saldo acumulado."""
    rng = random.Random(SEED)
    end = end or date(2026, 10, 10)
    start_year, start_month = end.year, end.month - (months - 1)
    while start_month <= 0:
        start_month += 12
        start_year -= 1
    rows: list[dict] = []
    for y, m in _month_iter(date(start_year, start_month, 1), months):
        for concept, cat, sub, flow, base, var, per_month, source, conf in MERCHANTS:
            if flow in ("income", "investment", "transfer_internal", "card_settlement") or per_month == 1:
                days = [min(28, {"income": 1, "investment": 3, "transfer_internal": 5, "card_settlement": 25}.get(flow, rng.randint(2, 27)))]
                if concept.startswith("RECIBO"):
                    days = [rng.randint(3, 9)]
                if concept in ("NETFLIX", "SPOTIFY", "GIMNASIO FORMA FICTICIA", "COMISION MANTENIMIENTO CUENTA"):
                    days = [{"NETFLIX": 5, "SPOTIFY": 7, "GIMNASIO FORMA FICTICIA": 9, "COMISION MANTENIMIENTO CUENTA": 15}[concept]]
            else:
                days = sorted(rng.randint(1, 28) for _ in range(per_month))
            for d in days:
                amount = base * (1 + rng.uniform(-var, var)) if var else base
                # el gimnasio sube de precio en los dos últimos meses
                if concept == "GIMNASIO FORMA FICTICIA" and (y, m) >= (end.year, end.month - 1):
                    amount = -39.0
                day = date(y, m, d)
                if day > end:
                    continue
                rows.append({
                    "date": day.isoformat(), "concept": concept, "amount": round(amount, 2),
                    "category": cat, "subcategory": sub, "flow_type": flow,
                    "source": source, "confidence": conf,
                })
    # una compra anómala reciente y un cargo duplicado
    rows.append({"date": (end - timedelta(days=4)).isoformat(), "concept": "PAGO MOVIL EN MERCADONA",
                 "amount": -215.4, "category": "Alimentacion", "subcategory": "supermercado",
                 "flow_type": "expense", "source": "rule", "confidence": 1.0})
    rows.append({"date": (end - timedelta(days=2)).isoformat(), "concept": "PAGO MOVIL EN PIZZERIA LUNA",
                 "amount": -31.0, "category": "Alimentacion", "subcategory": "restaurantes",
                 "flow_type": "expense", "source": "clef", "confidence": 0.64})
    rows.append({"date": (end - timedelta(days=2)).isoformat(), "concept": "PAGO MOVIL EN PIZZERIA LUNA",
                 "amount": -31.0, "category": "Alimentacion", "subcategory": "restaurantes",
                 "flow_type": "expense", "source": "clef", "confidence": 0.64})
    rows.sort(key=lambda r: r["date"])
    balance = 1800.0
    for r in rows:
        balance = round(balance + r["amount"], 2)
        r["balance"] = balance
    return rows


def build_demo(target: Path, *, source_config: Path = Path("config"), months: int = 14) -> Path:
    """Crea target/config/settings.yaml, target/moooney.db y devuelve la ruta del settings."""
    target = Path(target)
    config_dir = target / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (target / "excel").mkdir(exist_ok=True)
    (target / "clean").mkdir(exist_ok=True)
    for name in ("categories.yaml", "rules.yaml", "llm.yaml", "enrichment.yaml"):
        src = source_config / name
        if src.exists():
            shutil.copy(src, config_dir / name)
    base_settings = yaml.safe_load((source_config / "settings.yaml").read_text(encoding="utf-8"))
    base_settings["paths"] = {
        "excel_dir": str(target / "excel"),
        "clean_dir": str(target / "clean"),
        "db_path": str(target / "moooney.db"),
        "reports_dir": str(target / "reports"),
        "backups_dir": str(target / "backups"),
    }
    settings_path = config_dir / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(base_settings, allow_unicode=True, sort_keys=False), encoding="utf-8")
    # el enriquecimiento de la demo siempre apagado: nada sale a Internet por probar
    enr = config_dir / "enrichment.yaml"
    if enr.exists():
        data = yaml.safe_load(enr.read_text(encoding="utf-8")) or {}
        data["enabled"] = False
        enr.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")

    db_path = target / "moooney.db"
    if db_path.exists():
        db_path.unlink()
    conn = repo.connect(db_path)
    try:
        _populate(conn, generate_transactions(months))
    finally:
        conn.close()
    return settings_path


def _populate(conn: sqlite3.Connection, rows: list[dict]) -> None:
    now = "2026-10-10T12:00:00"
    with conn:
        conn.execute(
            "INSERT INTO imports (sha256, filename, imported_at, rows_total, rows_new, date_min, date_max)"
            " VALUES ('demo', 'demo_movimientos.xlsx', ?, ?, ?, ?, ?)",
            (now, len(rows), len(rows), rows[0]["date"], rows[-1]["date"]),
        )
        merchants: dict[str, dict] = {}
        for i, r in enumerate(rows):
            key = clean_concept(r["concept"])
            conn.execute(
                "INSERT INTO transactions (fingerprint, date, value_date, concept, concept_clean, amount,"
                " balance, account, flow_type, category, subcategory, confidence, import_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '****0000', ?, ?, ?, ?, 1, ?)",
                (f"demo-{i}", r["date"], r["date"], r["concept"], key, r["amount"], r["balance"],
                 r["flow_type"], r["category"], r["subcategory"], r["confidence"], now),
            )
            merchants.setdefault(key, r)
        for key, r in merchants.items():
            conn.execute(
                "INSERT OR IGNORE INTO merchants (key, category, subcategory, flow_type, source, confidence,"
                " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (key, r["category"], r["subcategory"], r["flow_type"], r["source"], r["confidence"], now),
            )
        conn.execute(
            "INSERT INTO merchant_enrichment (key, provider, query, summary, place_types, url, helped, fetched_at)"
            " VALUES ('TASCA DO RIO', 'nominatim', 'TASCA DO RIO', 'Tasca do Río: amenity/restaurant (Rúa Inventada, Vigo)',"
            " 'amenity/restaurant', NULL, 1, ?)",
            (now,),
        )
        conn.execute(
            "INSERT INTO web_calls (ts, provider, query, status, payload_hash) VALUES (?, 'nominatim', 'TASCA DO RIO', 'hit', 'x')",
            (now,),
        )
        conn.execute(
            "INSERT INTO llm_calls (ts, model, purpose, input_tokens, output_tokens, payload_hash)"
            " VALUES (?, 'clef-flash', 'categorize', 480, 0, 'x')",
            (now,),
        )
    store_recurring(conn, detect_recurring(conn, rows[-1]["date"]), now)
