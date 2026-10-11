# moooney

Finanzas personales con IA, en tu máquina. Exportas tus movimientos del banco a Excel, moooney los
limpia, los categoriza con un modelo de decisión, detecta recurrentes, propone presupuestos, lanza
alertas, te dice **a dónde se va el dinero cada mes** y qué harías para mejorar, y lo muestra todo en
una web local. Ningún dato bancario sale de tu ordenador ni de la infraestructura que tú elijas.

Está pensado para que cualquiera lo clone y vea lo mismo con sus datos.

## En 5 minutos (sin tus datos)

```powershell
git clone https://github.com/scavero/moooney.git
cd moooney
uv run moooney demo --serve
```

Abre http://127.0.0.1:8765. Son 14 meses inventados, deterministas: suficiente para ver el resumen, las
tendencias, los comercios, los presupuestos, las alertas y el informe.

Requisitos: [uv](https://docs.astral.sh/uv/) (instala Python 3.12 solo). O Docker, más abajo.

## Con tus datos

1. **Exporta** los movimientos de tu cuenta a Excel desde la web del banco (Santander: Cuentas → Movimientos
   → filtro de fechas amplio → Descargar → Excel). Déjalos en `data/excel/`.
2. **Clave del modelo**: copia `.env.example` a `.env` y pon `LITELLM_API_KEY`. Vale cualquier servidor
   compatible con la API de OpenAI (LiteLLM, vLLM, Ollama…); los modelos se configuran en `config/llm.yaml`.
   Sin clave también funciona: categoriza por reglas y memoria, y el informe sale sin modelo.
3. **Arranca** la web y usa la página *Pipeline*:

```powershell
uv run moooney web
```

   Importar Excel → Categorizar pendientes → (revisar dudosas en *Comercios*) → Presupuestos → Alertas → Informe.
   Cada paso tiene su botón, su registro en pantalla y su equivalente en la CLI.

4. **Cada mes**: exporta el mes nuevo, déjalo en `data/excel/` y pulsa *Importar*. Nada se duplica. Si prefieres
   olvidarte, `uv run moooney watch` vigila la carpeta y corre la pipeline sola.

## Qué verás

- **Resumen**: ingresos, gastos fijos y variables, ahorro neto y tasa, inversión; **diagnóstico** del mes con
  hallazgos ordenados por euros al mes (categorías que se disparan, comercio dominante, comer fuera,
  comisiones, suscripciones, cargos pequeños que suman, tasa de ahorro, meses en rojo, colchón, previsión
  a fin de mes); gasto por categoría de 12 meses con clic para bajar a los movimientos; reparto del mes;
  comercios donde más se gasta; presupuesto y alertas.
- **Tendencias**: previsión a fin de mes y de los próximos tres, colchón en meses, saldo proyectado a un
  año, dónde se ha ido el dinero en 12 meses, evolución de cada categoría con media móvil y tabla mes a mes.
- **Movimientos**: filtros por mes, categoría, flujo y texto; cada fila enlaza al comercio.
- **Comercios**: cada clave de comercio con su fuente y confianza; corrige la categoría en línea y se aplica
  a todos sus movimientos para siempre.
- **Recurrentes**, **Presupuestos** (propuesta con la mediana de 6 meses, ajuste en línea, seguimiento con
  proyección), **Alertas** (presupuesto al 80 / 100 %, importe anómalo, cargo duplicado, recurrente nuevo,
  subida de precio, ingreso que no llega, saldo proyectado negativo, comisiones).
- **Informe**: el modelo de texto recibe solo agregados y devuelve un informe breve con acciones
  priorizadas por impacto. Sin modelo, el informe sale de los hallazgos deterministas.
- **Pipeline**: todos los pasos con un botón, vista previa y activación del enriquecimiento, auditoría de
  lo que ha salido a modelos e Internet, historial de importaciones y trabajos.

## Cómo funciona

```
Excel ──► ingesta ──► SQLite ──► categorización ──► (enriquecimiento web opcional) ──► analítica ──► web / informe
          sin IA                  reglas → memoria → embeddings → Clef-Flash         sin IA        FastAPI + HTMX
```

- **Determinista antes que IA.** Parseo, deduplicación, métricas, recurrentes, presupuestos, alertas,
  previsión y diagnóstico son código normal y testeable. El modelo solo interpreta nombres de comercio y
  redacta el informe.
- **Categorización en cascada por clave de comercio**, no por movimiento: reglas (`config/rules.yaml`),
  memoria (`merchants`), embeddings para variantes de un comercio conocido y, solo para lo nuevo,
  [Clef-Flash](https://huggingface.co/Cloudflare/clef-flash), un modelo de decisión que devuelve
  probabilidades en lugar de texto. Tus correcciones mandan sobre todo lo demás.
- **Enriquecimiento externo (opcional, desactivado por defecto)**: para comercios que siguen dudosos
  consulta Nominatim, un SearXNG propio, DuckDuckGo o Google Places con la clave de comercio limpia, nunca
  importes ni fechas, y vuelve a preguntar a Clef. Es el único punto donde algo sale de tu infraestructura;
  antes de activarlo puedes ver la lista exacta de consultas, y cada una queda en `web_calls`.

Diagramas en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md); diseño y decisiones en [docs/DESIGN.md](docs/DESIGN.md).

## Privacidad

- `data/`, `reports/`, `config/budgets.yaml` y `.env` están ignorados por git y fuera de la imagen Docker.
- A los modelos van claves de comercio limpias (sin IBAN, saldos, nombres de personas ni referencias) o
  agregados mensuales. Una barrera (`llm/anonymize.py`) rechaza cualquier texto con forma de IBAN, tarjeta o
  referencia larga antes de enviarlo. Todo queda en `llm_calls` y `web_calls`, visibles en *Pipeline*.
- La web solo escucha en `127.0.0.1` y no tiene cuentas: es para tu máquina.
- `MOOONEY_OFFLINE=1` bloquea toda llamada a modelos y a Internet; todo lo demás sigue funcionando.
- Las librerías de la web (htmx, Chart.js) se sirven desde el propio repo, sin CDN. Las fuentes (Fraunces y
  Work Sans) se cargan de Google Fonts: es la única petición externa de la web y no lleva ningún dato.

## Docker

```powershell
cp .env.example .env     # y pon tu LITELLM_API_KEY
docker compose up -d     # http://127.0.0.1:8765
```

`./data`, `./config` y `./reports` se montan como volúmenes: la imagen no contiene datos. Para que la pipeline
corra sola cuando dejes un Excel nuevo en `data/excel/`:

```powershell
docker compose --profile watch up -d
```

## CLI

Todo lo de la web, desde la terminal. `uv run moooney --help` lista los comandos:

| Comando | Qué hace |
|---|---|
| `inspect FICHERO` | Comprueba que el parser entiende un Excel (solo estructura, nunca valores) |
| `ingest` | Importa los Excel nuevos de `data/excel/` (idempotente) |
| `status` | Estado de la base |
| `merchants` | Claves de comercio con transacciones e importe |
| `categorize` | Cascada de categorización; `--refresh-rules`, `--rebuild-keys`, `--enrich`, `--no-llm`, `--dry-run` |
| `enrich` | Enriquecimiento externo; `--dry-run --out fichero` lista las consultas sin enviar nada |
| `review` | Corregir a mano dudosas y Otros |
| `eval-clef` | Mide al modelo contra claves etiquetadas a mano |
| `metrics`, `recurring`, `budget init\|set\|show`, `alerts` | Analítica |
| `report` | Informe mensual con consejos (`--no-model` para la versión determinista) |
| `watch` | Vigila `data/excel/` y corre la pipeline; `--once` para tareas programadas |
| `web` | Interfaz web local |
| `demo` | Base sintética para probar sin datos reales |

## Configuración

| Fichero | Para qué |
|---|---|
| `config/settings.yaml` | Rutas y mapeo de columnas del Excel (alias por columna, sin tocar código) |
| `config/categories.yaml` | Taxonomía cerrada; las descripciones son lo que se le explica al modelo |
| `config/rules.yaml` | Reglas regex → categoría, la primera que casa gana |
| `config/llm.yaml` | Proxy, clave (nombre de la variable de entorno) y qué modelo hace cada cosa |
| `config/enrichment.yaml` | Enriquecimiento: activado, disparador, vetos, proveedores, tope de consultas |
| `config/budgets.yaml` | Tus límites mensuales (ignorado por git; `budget init` lo crea) |

Para otro banco basta con ajustar `settings.yaml` (fila de cabecera y alias de columnas) y, si hace falta,
los prefijos de operación en `categorize/normalize.py`.

## Desarrollo

```powershell
uv run pytest        # 136 tests, todos con datos sintéticos
uv run ruff check .
uv run moooney web --reload
```

Contexto para agentes de IA que trabajen en el repo: [AGENTS.md](AGENTS.md).
