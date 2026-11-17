"""Tendencia, estacionalidad y previsión con banda: series inventadas."""

from moooney.analytics import trends


def _months(n: int, year: int = 2024) -> list[str]:
    return [f"{year + i // 12}-{i % 12 + 1:02d}" for i in range(n)]


AHEAD = ["2026-01", "2026-02", "2026-03"]


def test_con_pocos_meses_no_se_dice_nada():
    assert trends.analyze("X", [100.0] * 5, _months(5), AHEAD) is None
    assert trends.analyze("X", [0.0] * 12, _months(12), AHEAD) is None


def test_serie_plana_es_estable_y_la_banda_se_cierra():
    t = trends.analyze("Vivienda", [800.0] * 24, _months(24), AHEAD)
    assert t.yearly and t.change == 0 and trends.describe(t) == "estable"
    assert t.error == 0 and t.forecast[0] == ("2026-01", 800.0, 800.0, 800.0)


def test_subida_anual_y_pico_de_diciembre():
    xs = [300.0] * 24
    xs[11], xs[23] = 600.0, 660.0                      # dos diciembres altos
    xs = [x * (1.2 if i >= 12 else 1) for i, x in enumerate(xs)]
    t = trends.analyze("Compras", xs, _months(24), AHEAD)
    assert t.yearly and 0.15 < t.change < 0.30 and t.change_eur > 50
    assert [c for c, _ in t.peaks] == [12]
    texto = trends.describe(t)
    assert "sube un" in texto and "en un año" in texto and "pico en diciembre" in texto
    lo, hi = t.forecast[0][2], t.forecast[0][3]
    assert 0 <= lo <= t.forecast[0][1] <= hi


def test_el_patron_del_año_gana_cuando_hay_estacionalidad():
    xs = [100.0 + (400.0 if i % 12 in (6, 7) else 0.0) for i in range(36)]   # verano caro, tres años
    t = trends.analyze("Viajes", xs, _months(36, 2023), ["2026-01", "2026-07"])
    assert t.model == "patrón del año"
    assert t.forecast[1][1] > 2 * t.forecast[0][1]     # julio previsto muy por encima de enero


def test_con_un_año_la_tendencia_es_provisional():
    t = trends.analyze("Ocio", [50.0] * 9 + [90.0] * 3, _months(12), AHEAD)
    assert not t.yearly and "provisional" in trends.describe(t) and not t.peaks


def test_el_payload_solo_lleva_agregados():
    t = trends.analyze("Ocio", [50.0] * 9 + [90.0] * 3, _months(12), AHEAD)
    p = trends.payload(t, [t], "2025-01-10")
    assert p["categorias"][0]["nombre"] == "Ocio" and p["gasto_total"]["prevision"][0]["mes"] == "2026-01"
    assert set(p["categorias"][0]) == {"nombre", "meses_de_datos", "media_mensual_reciente", "cambio_pct",
                                       "cambio_eur_mes", "base", "picos", "prevision", "error_habitual_pct",
                                       "metodo", "resumen", "escalon", "mes_raro", "pct_de_fondo"}


def test_escalon_sostenido_y_no_dos_meses_caros():
    t = trends.analyze("Suministros", [100.0, 110.0, 95.0, 105.0, 100.0, 108.0, 150.0, 160.0, 155.0, 158.0], _months(10), AHEAD)
    assert t.step == ("2024-07", 103.0, 155.75) and t.odd is None
    assert "cambió de escalón en julio de 2024: de 103 € a 156 € al mes" in trends.describe(t)
    assert trends.payload(t, [t], "x")["categorias"][0]["escalon"] == {"desde": "2024-07", "antes": 103, "ahora": 156}
    # dos meses altos y vuelta a lo de siempre no es un escalón
    assert trends.analyze("X", [100.0] * 6 + [160.0, 160.0, 100.0, 100.0], _months(10), AHEAD).step is None
    # ni una subida pequeña
    assert trends.analyze("X", [100.0] * 6 + [110.0] * 4, _months(10), AHEAD).step is None


def test_mes_raro_y_el_pico_de_temporada_no_lo_es():
    xs = [200.0, 210.0, 190.0, 205.0, 195.0, 200.0, 208.0, 600.0, 198.0, 202.0]
    t = trends.analyze("Hogar", xs, _months(10), AHEAD)
    assert t.step is None and t.odd == ("2024-08", 399.0)
    assert "agosto de 2024 fue un mes raro: 399 € por encima de lo normal" in trends.describe(t)
    assert trends.odd_month(xs, _months(10), {8}) is None            # si agosto es su pico de cada año, se espera
    assert trends.odd_month([200.0] * 3 + [600.0] + [200.0] * 8, _months(12), set()) is None   # hace más de 6 meses


def test_de_fondo_frente_a_puntual(tmp_path):
    from moooney.db import repo
    from moooney.web.queries import core_share, trends_analysis
    from tests.test_analytics import _tx

    conn = repo.connect(tmp_path / "t.db")
    filas = [(f"2025-{m:02d}-05", "SUPER INVENTADO", -100.0, "Alimentacion", "expense") for m in range(1, 9)]
    filas += [("2025-03-10", "MUEBLES FICTICIOS", -400.0, "Hogar", "expense"),
              ("2025-04-10", "BAR DE PASO", -200.0, "Alimentacion", "expense"),
              ("2025-08-31", "NOMINA", 2000.0, "Ingresos", "income")]
    _tx(conn, filas)
    meses = [f"2025-{m:02d}" for m in range(1, 9)]
    share = core_share(conn, meses)
    assert share["Alimentacion"] == 0.8 and share["Hogar"] == 0.0 and share[""] == 800 / 1400
    tr = trends_analysis(conn, "2025-09-10", "2025-01-01")
    assert tr["total"].core == share[""] and {t.name: t.core for t in tr["trends"]}["Alimentacion"] == 0.8
