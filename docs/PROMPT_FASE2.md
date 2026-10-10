# Prompt de arranque — Fase 2: categorización

Copia desde la línea siguiente hasta el final y pégalo en OpenCode **solo cuando la fase 1 esté cerrada** (transacciones cargadas, tests verdes, push hecho).

---

Lee `AGENTS.md` y `docs/DESIGN.md` (sección 4, "Categorización", y "Modelos y reparto de tareas"). Vas a implementar la **fase 2: categorización en cascada**. La fase 1 ya cargó las transacciones en `data/moooney.db`. Puedes consultar la base de datos y el Excel real para diseñar la normalización y las reglas, pero nunca copies conceptos reales que contengan nombres de personas a código, tests, fixtures ni documentación.

## Contexto del usuario

- Una sola cuenta del Santander. Por ella pasan: liquidaciones de tarjeta de crédito, cargos de telepeaje **Vía-T del Santander** y aportaciones a **inversión dentro del propio Santander** (fondos, planes de pensiones, valores).
- Inversión: suscripciones y aportaciones son `flow_type=investment` (ahorro, no gasto). Reembolsos, dividendos y cupones son `income` con subcategoría "rendimientos de inversión". Conceptos esperables: SUSCRIPCION, FONDO, APORTACION, PLAN DE PENSIONES, COMPRA/VENTA VALORES, DIVIDENDO, REEMBOLSO. Verifica contra los datos reales cuáles aparecen de verdad.
- Telepeaje: VIA-T, VIA T, PEAJE, AUTOPISTA, AUDASA → Transporte / peajes.
- Región: Galicia. Supermercados habituales además de los nacionales: GADIS, FROIZ, EROSKI.

## Entregables

### 1. Taxonomía (`config/categories.yaml`)

Categorías y subcategorías cerradas con una **descripción de una frase por cada una**, porque esas descripciones son literalmente los `criteria` que se envían a Clef-Flash. Partir de la lista de `docs/DESIGN.md`. Incluir `flow_type` por defecto para cada categoría (p. ej. Ahorro e inversión → `investment`, Tarjeta de crédito → `card_settlement`, Transferencias internas → `transfer_internal`).

### 2. Normalización (`src/moooney/categorize/normalize.py`)

`clean_concept(concept: str) -> str` devuelve la **clave de comercio**: mayúsculas sin acentos, sin números de tarjeta ni secuencias de 4+ dígitos, sin fechas incrustadas, sin referencias, sin prefijos de operación ("PAGO MOVIL EN", "COMPRA", "TARJETA", "RECIBO DE", "ADEUDO") salvo los que aportan significado ("BIZUM ENVIADO", "TRANSFERENCIA RECIBIDA"), sin nombres de personas en Bizum y transferencias (quedarse en "BIZUM ENVIADO" / "TRANSFERENCIA A PARTICULAR"), espacios colapsados. Debe ser estable: la misma tienda con distinto número de terminal o ciudad da la misma clave.

Método de trabajo: antes de escribir la función, consulta `SELECT concept, COUNT(*) FROM transactions GROUP BY concept ORDER BY 2 DESC` y diseña las reglas sobre lo que veas. Objetivo: que las ~N transacciones colapsen en el menor número de claves sin mezclar comercios distintos. Añade un comando `uv run moooney merchants` que liste claves con número de transacciones e importe total, para que el usuario vea el resultado.

Rellena `concept_clean` en `transactions` para todas las filas.

### 3. Reglas (`config/rules.yaml` + `src/moooney/categorize/rules.py`)

Lista ordenada de `{pattern: regex, category, subcategory, flow_type}` aplicada sobre la clave de comercio. La primera que casa gana. Sembrar con, como mínimo: nómina, Bizum enviado/recibido, transferencias emitidas/recibidas, traspasos internos, inversión Santander (ver contexto), Vía-T, liquidación de tarjeta, comisiones y cuotas, retirada de efectivo, recibos de suministros (IBERDROLA, NATURGY, ENDESA, EDP, VIESGO, agua, gas), telefonía (MOVISTAR, VODAFONE, ORANGE, DIGI, R CABLE), supermercados (MERCADONA, GADIS, FROIZ, EROSKI, CARREFOUR, DIA, LIDL, ALDI, ALCAMPO), gasolineras (REPSOL, CEPSA, GALP, BP, SHELL), suscripciones (NETFLIX, SPOTIFY, AMAZON PRIME, HBO, MAX, DISNEY, APPLE, GOOGLE, MICROSOFT, OPENAI, ANTHROPIC), compras online (AMAZON, ALIEXPRESS), seguros (SANTANDER SEGUROS, MAPFRE, MUTUA, LINEA DIRECTA), impuestos (AEAT, HACIENDA, IBI, AYUNTAMIENTO, CONCELLO, DGT), hipoteca/préstamo/alquiler. Después amplía con las claves más frecuentes de los datos reales que no casen.

### 4. Memoria de comercios

Tabla `merchants` ya existe. Cada clave categorizada se guarda con `source` ∈ {`rule`, `user`, `embedding`, `clef`, `llm`} y `confidence`. Orden de precedencia al recategorizar: `user` > `rule` > `embedding` > `clef` > `llm`. Una clave con `source=user` nunca se vuelve a consultar.

### 5. Embeddings (`src/moooney/categorize/embed.py`)

Cliente de `bge-m3` vía proxy (`POST /v1/embeddings`). Tabla `merchant_embeddings(key, vector BLOB)`. Para una clave nueva, si la similitud coseno con una clave ya clasificada por `user` o `rule` supera 0,92, hereda su categoría con `source=embedding`. Sin dependencias pesadas: `numpy` basta.

### 6. Capa LLM (`src/moooney/llm/`)

- `client.py`: `httpx`, base URL y clave desde `config/llm.yaml` y `LITELLM_API_KEY`. Reintentos con backoff. Cada llamada se registra en `llm_calls` con `model`, `purpose`, tokens y `payload_hash` (sha256 del cuerpo). Modo `--offline` (variable `MOOONEY_OFFLINE=1`) que hace fallar cualquier llamada de red.
- `anonymize.py`: función que valida que un `state` no contiene IBAN, secuencias de 8+ dígitos, ni importes con más de dos decimales de saldo; lanza excepción si los encuentra. Se llama siempre antes de enviar.
- `decide.py`: interfaz `Decider.decide(state: str, questions: dict) -> dict[str, Answer]` con `Answer(choice, confidence, probabilities)`. Dos implementaciones:
  - `ClefChatDecider`: chat completion a `clef-flash` con un único mensaje de usuario cuyo `content` es `json.dumps({"state", "questions"})`. Parsea `choices[0].message.content` como JSON. **Ejemplo que funciona en `docs/clef_test.json`; replica exactamente ese formato.**
  - `ChatJsonDecider`: `qwen3.8-flash-next-fp8` con `response_format` JSON Schema, confianza autoinformada 0-1. Reserva si Clef falla.
- Selección por `config/llm.yaml`.

### 7. Comando `categorize`

`uv run moooney categorize [--limit N] [--dry-run]`: para cada clave de comercio sin entrada en `merchants`, aplica la cascada: reglas → embeddings → Clef. El `state` enviado a Clef incluye la clave, el signo e importe medio redondeado a la decena, el número de ocurrencias y el intervalo medio entre ocurrencias en días. Las preguntas: `category` (choice con la taxonomía), `flow_type` (choice) e `is_recurring` (noul). Paraleliza hasta 8 peticiones. Después propaga la categoría a `transactions`. Imprime resumen por `source` y por categoría.

### 8. Comando `review`

`uv run moooney review [--threshold 0.7]`: lista claves con `confidence` bajo el umbral o categoría "Otros", ordenadas por importe total, y permite asignar categoría de forma interactiva (número de opción). Guarda con `source=user` y repropaga.

### 9. Evaluación del prompt de Clef

Antes de dar por buena la cascada:
1. Construye `data/eval/merchants_labeled.csv` (ignorado por git) con 40-60 claves reales etiquetadas a mano por ti con la taxonomía, cubriendo todas las categorías presentes.
2. Script `uv run moooney eval-clef` que mide acierto de `category` y `flow_type` y calibración (acierto medio por tramo de confianza).
3. Itera la redacción del `state` y las descripciones de `categories.yaml` hasta superar **85 % de acierto en categoría y 95 % en flow_type**. Documenta en `docs/DESIGN.md` qué redacción funcionó y las cifras finales.

### 10. Tests

Sintéticos en `tmp_path`: normalización (variantes de un mismo comercio dan la misma clave; nombres de Bizum eliminados), reglas (orden y precedencia), cascada con un `Decider` falso inyectado, `anonymize` rechazando IBAN y referencias, parseo de la respuesta de Clef a partir de un JSON de ejemplo inventado, modo offline bloqueando red. `uv run pytest` y `uv run ruff check .` en verde.

## Flujo de trabajo

1. Taxonomía + normalización + comando `merchants`. Revisa el resultado sobre datos reales. Commit.
2. Reglas + memoria de comercios. Commit.
3. Capa LLM + `anonymize` + deciders + tests. Commit.
4. Embeddings. Commit.
5. `categorize` + `review`. Ejecuta `categorize` sobre los datos reales. Commit.
6. Evaluación de Clef e iteración del prompt hasta los umbrales. Commit.
7. Actualiza `docs/DESIGN.md` (decisiones, cifras de evaluación) y `README.md`. Push a `origin/main`.

## Criterios de aceptación

- Más del 90 % de las transacciones reales tienen categoría y `flow_type`, y menos del 10 % del importe total de gasto cae en "Otros".
- La mayoría de las transacciones se categorizan por regla o memoria, no por Clef: imprime el desglose por `source`.
- `eval-clef` supera 85 % / 95 %.
- `llm_calls` registra todas las llamadas y ninguna contiene IBAN ni referencias (verificable con una consulta).
- Tests y lint en verde. Nada de `data/` en git.

No empieces la fase 3. Al terminar, resume en cinco líneas: desglose por `source`, porcentaje de "Otros", cifras de `eval-clef` y qué revisar en `review`.
