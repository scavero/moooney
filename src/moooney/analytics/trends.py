"""Tendencia, estacionalidad y previsión con banda de una serie mensual de gasto.

Todo determinista y reproducible, sobre meses completos:

- Tendencia: con 24 meses, los 12 últimos frente a los 12 anteriores (inmune a la
  estacionalidad). Con menos, 3 frente a 3 y se marca como provisional.
- Estacionalidad: meses naturales que quedan claramente por encima de la media
  (solo con dos años, que es cuando un pico se ha repetido).
- Previsión: tres métodos sencillos compiten prediciendo los últimos meses ya
  conocidos (backtest); gana el que menos se equivoca y su error da la banda.
- Escalón: los últimos 3-6 meses quedan todos por encima (o todos por debajo) de
  los 6 anteriores. Mes raro: uno de los 6 últimos muy por encima de lo normal.
- De fondo: qué parte del gasto va a comercios a los que se vuelve casi todos los
  meses (lo calcula `web/queries.core_share`; aquí solo se guarda y se cuenta).

Con dos años de datos mensuales un modelo más complejo aprendería ruido; estos
se pueden explicar en una frase y dicen cuánto fallan.
"""

from dataclasses import dataclass
from statistics import mean, median

MIN_MONTHS = 6     # con menos meses completos no se dice nada
BACKTEST = 6       # meses que se predicen para medir el error
Z80 = 1.28         # banda del 80 % si el error fuera normal
STEP_MIN_PCT, STEP_MIN_EUR = 0.20, 15.0   # un escalón más pequeño no se cuenta
ODD_MIN_PCT, ODD_MIN_EUR = 0.30, 30.0     # ni un mes raro
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
         "noviembre", "diciembre")


@dataclass
class Trend:
    name: str
    n: int                                   # meses completos usados
    level: float                             # media de los 3 últimos
    change: float | None                     # variación relativa; None si no había gasto antes
    change_eur: float                        # euros al mes de diferencia
    yearly: bool                             # True: 12 meses frente a 12; False: 3 frente a 3 (provisional)
    peaks: list[tuple[int, float]]           # (mes natural 1-12, cuánto supera la media: 0.3 = +30 %)
    model: str                               # método que ganó el backtest
    error: float | None                      # error relativo del backtest (0.15 = se va un 15 %)
    forecast: list[tuple[str, float, float, float]]   # (mes, previsto, bajo, alto)
    step: tuple[str, float, float] | None = None      # (desde qué mes, media de antes, media de ahora)
    odd: tuple[str, float] | None = None              # (mes raro más reciente, euros sobre lo normal)
    core: float | None = None                         # parte del gasto que es de fondo (0-1)


def _media3(xs: list[float], months: list[str], target: str) -> float:
    return mean(xs[-3:])


def _suavizado(xs: list[float], months: list[str], target: str, alpha: float = 0.3) -> float:
    level = xs[0]
    for x in xs[1:]:
        level = alpha * x + (1 - alpha) * level
    return level


def _estacional(xs: list[float], months: list[str], target: str) -> float | None:
    """Nivel del último año por el índice del mes natural, encogido hacia 1 cuanto menos se ha visto."""
    same = [x for x, m in zip(xs, months, strict=True) if m[5:7] == target[5:7]]
    base = mean(xs)
    if len(xs) < 13 or not same or base <= 0:
        return None
    k = len(same)
    return mean(xs[-12:]) * (1 + (mean(same) / base - 1) * k / (k + 1))


MODELS = {"media de 3 meses": _media3, "suavizado exponencial": _suavizado, "patrón del año": _estacional}


def _backtest(fn, xs: list[float], months: list[str]) -> tuple[float, float] | None:
    """(error relativo, error cuadrático medio) prediciendo cada uno de los últimos meses con los anteriores."""
    errs, real = [], 0.0
    for t in range(max(len(xs) - BACKTEST, 3), len(xs)):
        p = fn(xs[:t], months[:t], months[t])
        if p is None:
            return None
        errs.append(xs[t] - p)
        real += xs[t]
    if not errs or real <= 0:
        return None
    return sum(abs(e) for e in errs) / real, mean(e * e for e in errs) ** 0.5


def level_shift(xs: list[float], months: list[str]) -> tuple[str, float, float] | None:
    """Cambio de escalón sostenido: los últimos k meses (el tramo más largo de 6 a 3) quedan todos por
    encima, o todos por debajo, de cada uno de los 6 anteriores. Que no se pisen es lo que lo distingue
    de un par de meses caros."""
    for k in range(6, 2, -1):
        before, after = xs[-k - 6:-k], xs[-k:]
        if len(before) < 3:
            continue
        a, b = mean(after), mean(before)
        if (min(after) > max(before) or max(after) < min(before)) and abs(a - b) >= max(STEP_MIN_PCT * b, STEP_MIN_EUR):
            return months[-k], b, a
    return None


def odd_month(xs: list[float], months: list[str], skip: set[int]) -> tuple[str, float] | None:
    """El mes más reciente de los 6 últimos muy por encima de lo normal: mediana + 3 σ robusta (1,4826·MAD),
    y al menos un 30 % y 30 €. `skip` son los meses naturales con pico de temporada: esos ya se esperan."""
    med = median(xs)
    sigma = 1.4826 * median(abs(x - med) for x in xs)
    umbral = med + max(3 * sigma, ODD_MIN_PCT * med, ODD_MIN_EUR)
    for x, m in list(zip(xs, months, strict=True))[:-7:-1]:
        if x > umbral and int(m[5:7]) not in skip:
            return m, x - med
    return None


def analyze(name: str, xs: list[float], months: list[str], ahead: list[str]) -> Trend | None:
    """`xs` es el gasto de los meses completos `months` (en orden); `ahead`, los meses a prever."""
    n = len(xs)
    if n < MIN_MONTHS or sum(xs) <= 0:
        return None
    yearly = n >= 24
    a, b = (mean(xs[-12:]), mean(xs[-24:-12])) if yearly else (mean(xs[-3:]), mean(xs[-6:-3]))
    peaks: list[tuple[int, float]] = []
    if yearly:
        base = mean(xs)
        for cal in range(1, 13):
            same = [x for x, m in zip(xs, months, strict=True) if int(m[5:7]) == cal]
            if len(same) >= 2 and mean(same) >= 1.25 * base and mean(same) - base >= 20:
                peaks.append((cal, mean(same) / base - 1))
        peaks = sorted(peaks, key=lambda p: -p[1])[:2]
    scores = {k: s for k, fn in MODELS.items() if (s := _backtest(fn, xs, months)) is not None}
    model = min(scores, key=lambda k: scores[k][0]) if scores else "media de 3 meses"
    error, rmse = scores.get(model, (None, 0.0))
    forecast = []
    for m in ahead:
        p = max(MODELS[model](xs, months, m) or 0.0, 0.0)
        # shortcut: la misma banda para los tres meses; ensancharla con el horizonte si se prevé más lejos
        forecast.append((m, p, max(p - Z80 * rmse, 0.0), p + Z80 * rmse))
    step = level_shift(xs, months)
    return Trend(name=name, n=n, level=mean(xs[-3:]), change=(a - b) / b if b > 0 else None, change_eur=a - b,
                 yearly=yearly, peaks=peaks, model=model, error=error, forecast=forecast, step=step,
                 # tras un escalón los meses nuevos son lo normal de ahora, no meses raros
                 odd=None if step else odd_month(xs, months, {c for c, _ in peaks}))


def _mes(m: str) -> str:
    return f"{MESES[int(m[5:7]) - 1]} de {m[:4]}"


def describe(t: Trend) -> str:
    """Una frase con lo que le pasa a la serie, para la página y para el modelo."""
    parts = []
    if t.change is None:
        parts.append("gasto nuevo, sin periodo anterior con el que comparar")
    elif abs(t.change) < 0.10 or abs(t.change_eur) < 5:
        parts.append("estable")
    else:
        base = "en un año" if t.yearly else "en 3 meses (provisional)"
        parts.append(f"{'sube' if t.change > 0 else 'baja'} un {abs(t.change) * 100:.0f} % {base}, "
                     f"{t.change_eur:+.0f} € al mes")
    if t.peaks:
        parts.append("pico en " + " y ".join(f"{MESES[c - 1]} (+{p * 100:.0f} %)" for c, p in t.peaks))
    if t.step:
        parts.append(f"cambió de escalón en {_mes(t.step[0])}: de {t.step[1]:.0f} € a {t.step[2]:.0f} € al mes")
    if t.odd:
        parts.append(f"{_mes(t.odd[0])} fue un mes raro: {t.odd[1]:.0f} € por encima de lo normal")
    return "; ".join(parts)


def payload(total: Trend | None, trends: list[Trend], as_of: str) -> dict:
    """Lo que recibe el modelo de redacción: solo agregados mensuales por categoría."""
    def one(t: Trend) -> dict:
        return {
            "nombre": t.name, "meses_de_datos": t.n, "media_mensual_reciente": round(t.level),
            "cambio_pct": round(t.change * 100) if t.change is not None else None,
            "cambio_eur_mes": round(t.change_eur), "base": "12 meses frente a 12" if t.yearly else "3 frente a 3, provisional",
            "picos": [{"mes": MESES[c - 1], "sobre_la_media_pct": round(p * 100)} for c, p in t.peaks],
            "prevision": [{"mes": m, "previsto": round(v), "entre": [round(lo), round(hi)]} for m, v, lo, hi in t.forecast],
            "error_habitual_pct": round(t.error * 100) if t.error is not None else None, "metodo": t.model,
            "escalon": {"desde": t.step[0], "antes": round(t.step[1]), "ahora": round(t.step[2])} if t.step else None,
            "mes_raro": {"mes": t.odd[0], "sobre_lo_normal": round(t.odd[1])} if t.odd else None,
            "pct_de_fondo": round(t.core * 100) if t.core is not None else None,
            "resumen": describe(t),
        }
    return {"datos_hasta": as_of, "gasto_total": one(total) if total else None, "categorias": [one(t) for t in trends[:10]]}


SYSTEM_PROMPT = """Eres un analista de finanzas personales prudente y directo. Recibes, de una sola persona,
la tendencia, la estacionalidad y la previsión de su gasto por categoría, ya calculadas (nunca movimientos).
Tu trabajo es interpretar, no calcular. Escribe en español, en Markdown, sin saludos ni relleno:

## Lo que está cambiando
2-4 viñetas con los cambios que más pesan en euros al mes y qué pueden significar.

## Lo que viene
2-3 viñetas sobre los próximos meses: picos estacionales que se acercan y la previsión del gasto total con su rango.

## Dónde mirar
2-3 cosas concretas que revisar, ordenadas por euros.

Reglas: usa solo cifras que estén en el JSON y cítalas tal cual. Si una base es «provisional» o el error
habitual pasa del 30 %, dilo y no saques conclusiones firmes. Si el resumen de una serie dice «estable»,
no la presentes como un cambio. Un «escalón» es un cambio sostenido de nivel y pesa más que un «mes raro»,
que es un mes suelto. «pct_de_fondo» es la parte del gasto en comercios a los que vuelve casi todos los meses:
lo que no es de fondo son compras puntuales, más fáciles de recortar. No des consejos de inversión. Máximo 250 palabras."""
