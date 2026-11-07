# moooney

[![ci](https://github.com/scavero/moooney/actions/workflows/ci.yml/badge.svg)](https://github.com/scavero/moooney/actions/workflows/ci.yml)
[![licencia MIT](https://img.shields.io/badge/licencia-MIT-0e3b2e)](LICENSE)

Finanzas personales con IA, en tu máquina. Exportas tus movimientos del banco a Excel (Santander o BBVA,
otros por perfil), moooney los limpia, los categoriza con un modelo de decisión, detecta recurrentes, propone
presupuestos, lanza alertas, te dice **a dónde se va el dinero cada mes** en una frase y qué harías para
mejorar, y lo muestra todo en una web local. Ningún dato bancario sale de tu ordenador ni de la
infraestructura que tú elijas.

Está pensado para que cualquiera lo clone y vea lo mismo con sus datos. La interfaz, la taxonomía y los
perfiles de banco están en castellano y pensados para cuentas españolas.

## En 5 minutos (sin tus datos)

Necesitas [git](https://git-scm.com/) y [uv](https://docs.astral.sh/uv/), que instala Python 3.12 por su
cuenta. Funciona en Windows, Linux y macOS; los comandos de este README valen igual en PowerShell y en bash.

```powershell
git clone https://github.com/scavero/moooney.git
cd moooney
uv run moooney web
```

Abre http://127.0.0.1:8765. Mientras no haya datos tuyos, la app muestra una **demo** con 14 meses
inventados y deterministas, marcada como tal en cada página: suficiente para ver el resumen, las
tendencias, los comercios, los presupuestos, las alertas y el informe. En cuanto importas tu primer
Excel, la demo desaparece sola. (`uv run moooney web --demo` la fuerza aunque tengas datos.)

No hace falta ningún modelo para probarla. Si prefieres Docker, mira la sección del mismo nombre.

## Con tus datos

1. **Exporta** los movimientos de tu cuenta a Excel desde la web del banco y déjalos en `data/excel/`:
   - **Santander**: Cuentas → Movimientos → filtro de fechas amplio → Descargar → Excel.
   - **BBVA**: Cuentas → Movimientos → Exportar → Excel («Últimos movimientos»).
   Se detecta el banco por la cabecera del fichero; puedes mezclar bancos en la misma carpeta.
2. **Modelos (opcional)**: apunta moooney a tu servidor de modelos como se explica en
   [Modelos](#modelos). Sin modelos también funciona: categoriza por reglas y memoria, y el informe sale de
   los cálculos deterministas.
3. **Reglas propias (opcional)**: copia `config/rules.local.example.yaml` a `config/rules.local.yaml` y añade
   los comercios de tu zona o los servicios que pagas. Ese fichero no entra en git y manda sobre las reglas
   generales.
4. **Arranca** la web y usa la página *Pipeline*:

```powershell
uv run moooney web
```

   Importar Excel → Categorizar pendientes → Reintentar dudosas / Segunda opinión → (revisar lo que quede en
   *Comercios*) → Presupuestos → Alertas → Informe. Cada paso tiene su botón, su registro en pantalla y su
   equivalente en la CLI; la página muestra de un vistazo qué paso pide acción.

5. **Cada mes**: exporta el mes nuevo, déjalo en `data/excel/` y pulsa *Importar*. Nada se duplica. Si prefieres
   olvidarte, `uv run moooney watch` vigila la carpeta y corre la pipeline sola (o una tarea programada, más abajo).

## Modelos

moooney habla con cualquier servidor compatible con la API de OpenAI: LiteLLM, vLLM, Ollama u otro. Hay
tres papeles, y cada uno se asigna en `config/llm.yaml`:

| Papel | Para qué | Por defecto |
|---|---|---|
| `clef` | Decidir la categoría de cada comercio nuevo | [Clef-Flash](https://huggingface.co/Cloudflare/clef-flash) |
| `redaccion` | Informe mensual, segunda opinión y reserva para categorizar | `qwen3.8-flash-next-fp8` |
| `embeddings` | Heredar la categoría entre variantes de un mismo comercio | `bge-m3` |

Los nombres por defecto son los del servidor del autor; cámbialos por los que sirva el tuyo. Si no tienes
Clef-Flash, pon `decider: qwen` y el modelo de redacción categoriza con salida JSON Schema.

La URL y la clave nunca van en el YAML del repo, que solo lleva un marcador. Se ponen de una de estas formas,
de mayor a menor prioridad:

1. Variables de entorno del sistema `LITELLM_BASE_URL` y `LITELLM_API_KEY`.
2. La página *Ajustes* de la web, que las guarda en `data/.env` y tiene un botón para probar la conexión.
3. Un `.env` en la raíz, copiado de `.env.example`.

Las variables se llaman `LITELLM_*` por el proxy original, pero valen para cualquier servidor. Con Ollama en
local, por ejemplo, la URL es `http://localhost:11434/v1` y la clave puede ser cualquier texto. Si tu
servidor no es vLLM, quita el bloque `chat_extra` de `config/llm.yaml`.

## Qué verás

- **Resumen**: abre con la cifra de ahorro neto del mes y un **veredicto en una frase**, calculado sin modelo
  («Has ahorrado el 30 % de lo que entró; alimentación se dispara»); debajo, ingresos, gastos fijos y
  variables, ahorro neto, saldo (sumando todas las cuentas) y colchón en meses; **diagnóstico** con los euros
  al mes en primera columna (categorías que se disparan, comercio dominante, comer fuera, comisiones,
  suscripciones, cargos pequeños que suman, tasa de ahorro, meses en rojo, colchón, previsión a fin de mes);
  gasto por categoría de 12 meses con clic para bajar a los movimientos; reparto del mes; comercios donde más
  se gasta; presupuesto con la marca del día de hoy y alertas pendientes.
- **Tendencias**: previsión a fin de mes y de los próximos tres, colchón en meses, saldo proyectado a un
  año, dónde se ha ido el dinero en 12 meses, evolución de cada categoría con media móvil y tabla mes a mes.
- **Movimientos**: filtros por mes, categoría, flujo, cuenta, etiqueta y texto, totales del filtro; la
  categoría se corrige en la propia fila; **etiquetas libres** en movimientos sueltos (viaje, reforma…) para
  agrupar gastos de distintas categorías y ver lo que suman; **Exportar CSV** descarga el filtro completo listo
  para Excel. Con varios bancos, el saldo del resumen es la suma del último saldo de cada cuenta.
- **Comercios**: la **cola de revisión** es la página. Cada clave dudosa (las que importan primero: dos o más
  movimientos o 30 € o más) aparece como titular con su evidencia (los últimos movimientos, la categoría actual
  con su confianza y lo que dice la web), tres sugerencias con tecla (1-3), Enter para confirmar y S para
  saltar. Una corrección se aplica a todos los movimientos del comercio para siempre. La tabla completa de
  claves, con fuente, confianza y filtros, queda plegada debajo. Antes de revisar a mano, *Pipeline* ofrece
  **Reintentar dudosas** (el decider vuelve a preguntar con el tipo de operación y el resumen web) y
  **Segunda opinión** (el modelo de texto revisa por lotes y solo sustituye si está seguro).
- **Recurrentes**, **Presupuestos** (propuesta con la mediana de 6 meses, ajuste en línea, seguimiento con
  proyección), **Alertas** (presupuesto al 80 / 100 %, importe anómalo, cargo duplicado, recurrente nuevo,
  subida de precio, ingreso que no llega, saldo proyectado negativo, comisiones).
- **Informe**: el modelo de texto recibe solo agregados y devuelve un informe breve con acciones
  priorizadas por impacto. Sin modelo, el informe sale de los hallazgos deterministas.
- **Pipeline**: todos los pasos con un botón, vista previa y activación del enriquecimiento, auditoría de
  lo que ha salido a modelos e Internet, historial de importaciones y trabajos.
- **Ajustes**: la clave del proxy (y la de Google Places, opcional) sin tocar ficheros, con prueba de
  conexión, y el interruptor de modo offline. Las claves nunca se muestran enteras.

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
  probabilidades en lugar de texto. El modelo ve la clave limpia, el tipo de operación (recibo domiciliado,
  compra con tarjeta, bizum…), el importe redondeado y la frecuencia. Lo que queda dudoso pasa por una
  **segunda opinión** del modelo de texto, con los criterios de la taxonomía y el resumen web, que solo
  sustituye con confianza alta. Tus correcciones mandan sobre todo lo demás.
- **Varios bancos y cuentas**: cada banco es un perfil en `settings.yaml` (alias de columnas y, si hace falta,
  un constructor que reescribe su concepto a la forma «COMPRA EN …», «RECIBO …» con la que está calibrado
  todo lo demás). Las cuentas conviven en la misma base y el saldo es la suma del último saldo de cada una.
- **Enriquecimiento externo (opcional, desactivado por defecto)**: para comercios que siguen dudosos
  consulta Nominatim, un SearXNG propio, DuckDuckGo o Google Places con la clave de comercio limpia, nunca
  importes ni fechas, y vuelve a preguntar a Clef. Es el único punto donde algo sale de tu infraestructura;
  antes de activarlo puedes ver la lista exacta de consultas, y cada una queda en `web_calls`.

Diagramas en [docs/ARQUITECTURA.md](docs/ARQUITECTURA.md); diseño y decisiones en [docs/DESIGN.md](docs/DESIGN.md).

## Privacidad

- `data/`, `reports/`, `config/budgets.yaml` y `.env` están ignorados por git y fuera de la imagen Docker.
  La clave guardada desde *Ajustes* vive en `data/.env`, con permisos solo del usuario en Linux.
- A los modelos van claves de comercio limpias (sin IBAN, saldos, nombres de personas ni referencias) o
  agregados mensuales. Una barrera (`llm/anonymize.py`) rechaza cualquier texto con forma de IBAN, tarjeta o
  referencia larga antes de enviarlo. Todo queda en `llm_calls` y `web_calls`, visibles en *Pipeline*.
- La web solo escucha en `127.0.0.1` y no tiene cuentas: es para tu máquina.
- `MOOONEY_OFFLINE=1` bloquea toda llamada a modelos y a Internet; todo lo demás sigue funcionando.
- Las librerías y las fuentes de la web (htmx, Chart.js, Fraunces, Work Sans) se sirven desde el propio repo:
  la web no hace ninguna petición externa.

## Docker

```powershell
docker compose up -d
```

La primera vez construye la imagen. Después abre http://127.0.0.1:8765 y configura los modelos en *Ajustes*, o
copia `.env.example` a `.env` antes de arrancar. `./data`, `./config` y `./reports` se montan como volúmenes:
la imagen no contiene datos. Para que la pipeline corra sola cuando dejes un Excel nuevo en `data/excel/`:

```powershell
docker compose --profile watch up -d
```

El puerto solo se publica en `127.0.0.1` y la web no tiene usuarios ni contraseñas. Si la sirves a otros
equipos, ponla detrás de un proxy inverso con autenticación: cualquiera que llegue a ella ve tus movimientos.

## Automatizar en Windows

En lugar de dejar `watch` corriendo, una tarea programada que ejecute `moooney watch --once` cada día a las 9
importa lo que haya nuevo en `data/excel/`, categoriza, enriquece (si está activado) y recalcula alertas.
Desde PowerShell, en la carpeta del repo:

```powershell
$uv = (Get-Command uv).Source
schtasks /Create /TN "moooney" /SC DAILY /ST 09:00 /F /TR "`"$uv`" run --directory `"$PWD`" moooney watch --once"
```

`schtasks /Run /TN moooney` la lanza a mano y `schtasks /Delete /TN moooney /F` la quita. En Linux o macOS,
la misma línea en `cron`: `0 9 * * * cd /ruta/moooney && uv run moooney watch --once`.

## CLI

Todo lo de la web, desde la terminal. `uv run moooney --help` lista los comandos:

| Comando | Qué hace |
|---|---|
| `inspect FICHERO` | Comprueba que el parser entiende un Excel (solo estructura, nunca valores) |
| `ingest` | Importa los Excel nuevos de `data/excel/` (idempotente) |
| `status` | Estado de la base |
| `merchants` | Claves de comercio con transacciones e importe |
| `categorize` | Cascada de categorización; `--refresh-rules`, `--rebuild-keys`, `--enrich`, `--retry-dudosas`, `--second-opinion`, `--no-llm`, `--dry-run` |
| `enrich` | Enriquecimiento externo; `--dry-run --out fichero` lista las consultas sin enviar nada |
| `review` | Corregir a mano dudosas y Otros |
| `eval-clef` | Mide al modelo contra claves etiquetadas a mano |
| `metrics`, `recurring`, `budget init\|set\|show`, `alerts` | Analítica |
| `report` | Informe mensual con consejos (`--no-model` para la versión determinista) |
| `watch` | Vigila `data/excel/` y corre la pipeline; `--once` para tareas programadas |
| `export` | Movimientos categorizados a CSV; `--month`, `--category`, `--out` |
| `tag` | Etiqueta un movimiento (`tag ID "viaje china"`, `--remove`); sin argumentos lista las etiquetas |
| `web` | Interfaz web local |
| `demo` | Base sintética para probar sin datos reales |

## Configuración

| Fichero | Para qué |
|---|---|
| `config/settings.yaml` | Rutas, perfil del Santander y perfiles de otros bancos (`banks:`), alias de columnas |
| `config/categories.yaml` | Taxonomía cerrada; las descripciones son lo que se le explica al modelo |
| `config/rules.yaml` | Reglas regex → categoría, la primera que casa gana |
| `config/rules.local.yaml` | Reglas propias (comercios de tu zona, servicios que usas); ignorado por git y con prioridad sobre `rules.yaml` |
| `config/llm.yaml` | Proxy, clave (nombre de la variable de entorno) y qué modelo hace cada cosa |
| `config/enrichment.yaml` | Enriquecimiento: activado, disparador, vetos, proveedores, tope de consultas |
| `config/budgets.yaml` | Tus límites mensuales (ignorado por git; `budget init` lo crea) |
| `data/.env` | Claves guardadas desde *Ajustes* (ignorado; manda sobre `./.env`, no sobre el entorno) |

Para otro banco se añade un perfil en `settings.yaml` bajo `banks:` (alias de columnas, etiqueta de cuenta)
y, si su concepto no lleva el tipo de operación delante como el del Santander, un constructor de concepto en
`ingest/banks.py` que lo reescriba a esa forma (el del BBVA es el ejemplo).

## Desarrollo

```powershell
uv run pytest        # 170 tests, todos con datos sintéticos
uv run ruff check .
uv run moooney web --reload
```

La integración continua (`.github/workflows/ci.yml`) corre ruff y los tests en cada push y comprueba que la imagen
Docker se construye y sirve la demo. Cambios por versión en [CHANGELOG.md](CHANGELOG.md). El sistema visual
(Fraunces + Work Sans variables, pesos 400/500/700, contraste mínimo 4,5:1, un dominante y un acento, ritmo de 8 px) está en `static/app.css` y documentado en
`docs/DESIGN.md`; las dos pantallas principales se rediseñaron con [Superdesign](https://superdesign.dev) a
partir de las plantillas, nunca de datos.

Contexto para agentes de IA que trabajen en el repo: [AGENTS.md](AGENTS.md). Sus reglas sobre la
infraestructura de modelos son las del autor; las de privacidad valen para cualquiera.

## Limitaciones

- Solo hay perfiles probados para el Santander y el BBVA. Otro banco necesita su perfil en `settings.yaml`
  y, a veces, un constructor de concepto.
- La categorización está calibrada con conceptos en castellano. Con extractos en otro idioma, las reglas y
  la normalización de comercios acertarán menos.
- Es una herramienta personal de una sola persona y una sola máquina. No hay cuentas, permisos ni
  sincronización.

## Contribuir

Los issues y pull requests son bienvenidos. Nunca adjuntes extractos, capturas ni filas reales: para
reproducir un fallo usa `uv run moooney demo` o un Excel inventado. Los tests generan sus datos al vuelo y
así debe seguir siendo. Antes de abrir un pull request, `uv run ruff check .` y `uv run pytest` tienen que
pasar.

## Licencia

[MIT](LICENSE). Úsalo, modifícalo y compártelo; solo conserva el aviso de copyright.
