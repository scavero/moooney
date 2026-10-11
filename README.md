# moooney

Pipeline local de finanzas personales. Analiza los extractos exportados del Banco Santander
sin enviar ningún dato bancario fuera de la máquina o de la infraestructura propia.

Diagramas de la arquitectura (flujo de datos, cascada de categorización, fronteras de
privacidad y módulos): [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md).

## Requisitos

- [uv](https://docs.astral.sh/uv/) (Python 3.12 se instala solo)

## Uso (fase 1: ingesta)

Los extractos `.xlsx` se colocan a mano en `data/excel/`. Desde la raíz del proyecto:

```powershell
# 1. Comprobar que el parser entiende un Excel nuevo (solo muestra estructura:
#    hoja, fila de cabecera, mapeo de columnas, nº de filas y rango de fechas)
uv run moooney inspect data/excel/mi_extracto.xlsx

# 2. Importar los Excel que no estén ya en la base de datos (idempotente)
uv run moooney ingest            # o --dir otra_carpeta

# 3. Ver el estado de la base de datos (solo agregados)
uv run moooney status
```

Los CSV normalizados quedan en `data/clean/` y la base de datos en `data/moooney.db`.
El mapeo de columnas del parser se ajusta en `config/settings.yaml` sin tocar código.

## Uso (fase 2: categorización)

```powershell
# Categorizar lo que no lo esté (idempotente). Cascada: reglas -> memoria de
# comercios -> embeddings -> Clef-Flash. Requiere LITELLM_API_KEY; con
# MOOONEY_OFFLINE=1 (o --no-llm) solo usa reglas y memoria (nunca toca la red).
uv run moooney categorize        # --dry-run | --no-llm | --limit N

# Corregir a mano (tus correcciones mandan para siempre) los dudosos:
# confianza baja o categoría Otros
uv run moooney review            # o review --threshold 0.6

# Ver la tabla de comercios (fuente y confianza de cada clave)
uv run moooney merchants

# Medir al decider contra un set etiquetado a mano (>85 % cat, >95 % flow)
uv run moooney eval-clef         # --labels otra_carpena.csv
```

Las etiquetas de comercio viven en la tabla `merchants` (source: user > rule >
embedding > clef_web > clef > llm) y todo lo que hace el modelo queda registrado en `llm_calls`.

Si corriges o amplías `config/rules.yaml`, reaplica las reglas a las claves ya categorizadas
(nunca toca tus correcciones manuales):

```powershell
uv run moooney categorize --refresh-rules --no-llm
```

Si cambias la normalización de conceptos (`normalize.py`), reconstruye las claves; la memoria
de comercios se migra a las claves nuevas y tus correcciones se conservan:

```powershell
uv run moooney categorize --rebuild-keys
```

## Uso (fase 2.5: enriquecimiento externo, opcional)

Para los comercios que la cascada deja dudosos (confianza baja o categoría Otros), la
pipeline puede preguntar a un servicio externo qué es ese comercio y volver a consultar a
Clef con la respuesta. **Es el único punto donde algo sale de tu máquina y de ClusterTIC**:
solo la clave de comercio limpia más una pista regional, nunca importes, fechas ni saldos.
Por eso viene desactivado.

```powershell
# 1. Ver EXACTAMENTE qué consultas se enviarían y a qué proveedor, sin enviar nada
uv run moooney enrich --dry-run                      # a pantalla
uv run moooney enrich --dry-run --out reports/enrich_dry_run.txt

# 2. Si estás de acuerdo, pon enabled: true en config/enrichment.yaml y ejecuta
uv run moooney enrich                                # --limit N | --key CLAVE | --force

# o encadénalo a la categorización
uv run moooney categorize --enrich
```

Proveedores en orden (se para en el primero útil): Nominatim de OpenStreetMap (sin clave,
devuelve el tipo de lugar), un SearXNG propio si configuras `searxng_url`, DuckDuckGo, y
Google Places solo si existe `GOOGLE_PLACES_API_KEY`. Cada consulta real queda registrada
en la tabla `web_calls`; cada clave se consulta una sola vez (caché en `merchant_enrichment`).
La segunda ronda con Clef solo sobreescribe si mejora la confianza y nunca pisa una
corrección tuya. `MOOONEY_OFFLINE=1` bloquea también estos proveedores.

## Uso (fase 3: analítica)

Todo determinista, sin modelos. La fecha de referencia es siempre la del último movimiento cargado.

```powershell
# Métricas del mes: ingresos, gastos (fijos/variables), ahorro, inversión y gasto por
# categoría con variación frente al mes anterior y a la media de meses completos
uv run moooney metrics                 # --month 2026-09 | --months 12

# Cargos e ingresos recurrentes (periodo, importe, próxima fecha); --all incluye inactivos
uv run moooney recurring

# Presupuestos: propuesta inicial (mediana de 6 meses completos, -10 % en variables),
# ajuste manual y seguimiento con proyección a fin de mes
uv run moooney budget init             # --months 6 --cut 0.10 --force
uv run moooney budget set Ocio 120
uv run moooney budget show             # --month 2026-09

# Alertas: presupuesto al 80/100 %, importe anómalo, cargo duplicado, recurrente nuevo,
# subida de precio, ingreso ausente, saldo proyectado negativo, comisiones
uv run moooney alerts                  # --ack ID | --ack-all
```

Los límites viven en `config/budgets.yaml` (ignorado por git; ejemplo en
`config/budgets.example.yaml`) y se materializan por mes en la tabla `budgets`.

## Privacidad

`data/`, `reports/` y los `.env` están ignorados por git. Ningún comando imprime conceptos,
importes individuales, saldos ni el IBAN completo. Los tests usan únicamente datos sintéticos.
Egreso auditable: `llm_calls` (modelos, dentro de ClusterTIC) y `web_calls` (fase 2.5, fuera).

## Desarrollo

```powershell
uv run pytest        # tests
uv run ruff check .  # lint
```
