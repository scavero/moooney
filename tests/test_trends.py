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
                                       "metodo", "resumen"}
