# moooney — Diseño de la pipeline

Objetivo: mejorar la economía mensual del usuario analizando sus extractos bancarios, con todo el procesamiento en local o en infraestructura propia (LiteLLM de ClusterTIC). Ningún dato bancario sale a terceros.

## Principios

1. **Determinista antes que IA.** Parseo, deduplicación, métricas, presupuestos y detección de alertas son código normal y reproducible. El LLM solo entra donde hace falta interpretar texto libre (conceptos) o redactar (informes, consejos).
2. **El LLM nunca ve datos crudos.** Recibe conceptos anonimizados o agregados mensuales. Nunca IBAN, saldos, nombres de personas ni referencias.
3. **Cache y memoria.** Cada comercio se categoriza una vez. Las correcciones del usuario mandan sobre el modelo.
4. **Idempotente.** Ejecutar la pipeline dos veces sobre el mismo Excel no duplica nada.
5. **Proveedor de IA intercambiable.** LiteLLM hoy, Ollama local mañana, mismo código.

## Flujo

```
data/excel/*.xlsx  ──►  [1 detect]  ──►  [2 parse → CSV limpio]  ──►  [3 dedupe + load SQLite]
                                                                               │
          ┌────────────────────────────────────────────────────────────────────┘
          ▼
   [4 categorize] ──► [5 metrics] ──► [6 budgets] ──► [7 alerts] ──► [8 forecast] ──► [9 report/advice]
   reglas + memoria        SQL          usuario        reglas         estadística        LLM (agregados)
   + LLM solo nuevos
```

### 1. Detección de nuevos ficheros
- Comando `moooney ingest` (manual) o `moooney watch` (watchdog sobre `data/excel/`). En Windows se puede programar con el Programador de tareas.
- Cada fichero se identifica por SHA-256. Tabla `imports(hash, filename, imported_at, rows_total, rows_new)`. Si el hash ya existe, se salta.

### 2. Parseo a CSV limpio
- Salida en `data/clean/<fecha>_<hash8>.csv` con columnas fijas:
  `date, value_date, concept, amount, balance, account, source_file`
- Normalización: fechas ISO, importes como decimal con punto, conceptos en mayúsculas sin espacios dobles, cabeceras del Santander saltadas (se localiza la fila que contiene "Fecha").
- Comando `moooney inspect <fichero>` imprime **solo** nombres de columnas, número de filas y rango de fechas. Sirve para ajustar el parser sin exponer el contenido.

### 3. Deduplicación y carga
- Huella de transacción: `sha1(account | date | amount | concept | balance)`. El saldo desambigua movimientos idénticos el mismo día.
- Tabla `transactions(id, fingerprint UNIQUE, date, value_date, concept, concept_clean, amount, balance, account, import_id, created_at)`.
- Las exportaciones se solapan (2 años, luego mensuales): solo entran las huellas nuevas.

### 4. Categorización (híbrida, en cascada)
1. **Comercio normalizado**: `concept_clean` elimina números de tarjeta, referencias, fechas incrustadas y nombres propios de Bizum. Queda una clave tipo `MERCADONA`, `BIZUM ENVIADO`, `RECIBO IBERDROLA`.
2. **Memoria de comercios**: tabla `merchants(key, category, subcategory, flow_type, source, confidence)`. Si la clave existe, se aplica sin consultar nada.
3. **Reglas**: `config/rules.yaml` editable (regex → categoría). Cubre nómina, recibos, Bizum, transferencias, comisiones, liquidación de tarjeta, telepeaje, brókers.
4. **Vecinos por embedding** (opcional, `bge-m3`): si una clave nueva está muy cerca de una ya clasificada por el usuario (p. ej. `MERCADONA 1234` vs `MERCADONA`), hereda la categoría sin pasar por el modelo de decisión.
5. **Clef-Flash** para claves desconocidas. Es un modelo de decisión, no genera texto: recibe un `state` y un esquema de preguntas tipadas y devuelve probabilidades por opción. Por cada clave se envía un `state` con la clave normalizada, el signo e importe redondeado y el número de veces que aparece, y se preguntan:
   - `category` (`choice`, taxonomía cerrada con descripción por opción).
   - `flow_type` (`choice`: gasto, ingreso, transferencia interna, inversión, liquidación de tarjeta).
   - `is_recurring` (`noul`): pista para el detector de recurrentes, que luego lo confirma con fechas.
   La `confidence` que devuelve el modelo se guarda tal cual. No hay JSON que parsear ni alucinaciones posibles: la respuesta siempre es una opción válida del esquema. Latencia de decenas de milisegundos, así que las 300-500 claves de la carga inicial se resuelven en segundos.
6. **Revisión**: `moooney review` lista las de confianza < 0,7. La corrección del usuario sobreescribe `merchants` con `source=user` y nunca se vuelve a preguntar.

Taxonomía inicial (cerrada, ampliable): Ingresos (nómina, otros), Vivienda (alquiler/hipoteca, suministros, comunidad), Alimentación (supermercado, restaurantes), Transporte (combustible, transporte público, parking, peajes), Salud, Ocio, Suscripciones, Compras, Educación, Seguros, Impuestos y tasas, Transferencias (entre cuentas propias, Bizum enviado/recibido), Ahorro e inversión, Tarjeta de crédito (liquidación), Comisiones bancarias, Efectivo, Otros.

#### Tipos de flujo (`flow_type`)
La cuenta exportada es una sola, pero por ella pasan cargos de tarjetas de crédito, telepeaje y aportaciones a inversión. Cada transacción lleva un `flow_type` que decide cómo cuenta en las métricas:

| flow_type | Ejemplo | Métricas |
|---|---|---|
| `expense` | Mercadona, recibo luz, peaje | Gasto del mes |
| `income` | Nómina, devolución | Ingreso del mes |
| `transfer_internal` | Traspaso a otra cuenta propia | Se excluye de ingresos y gastos |
| `investment` | Transferencia a bróker, fondo, plan | **Ahorro**, no gasto. Acumula capital aportado |
| `card_settlement` | Liquidación mensual de tarjeta de crédito | Gasto del mes como bloque "Tarjeta de crédito" hasta que se importe el detalle |

- **Tarjetas de crédito**: la liquidación aparece como una sola línea. En fase 1 se trata como gasto opaco. En fase 2 se puede importar el Excel de la tarjeta por separado y conciliar: la suma de sus movimientos del periodo debe cuadrar con la liquidación, y entonces la liquidación se marca como `transfer_internal` para no contar dos veces.
- **Telepeaje**: cargo recurrente del operador (Bip&Drive, Vía-T). Categoría Transporte / peajes.
- **Inversión**: las transferencias a brókers o fondos cuentan como ahorro. Como el usuario invierte desde 2023 y la exportación empieza a finales de 2024, el capital aportado será parcial. `config/seed.yaml` admite un `invested_before_data_start` para no engañar a la tasa de ahorro histórica. No se valoran las posiciones (no hay datos de mercado); solo capital aportado.

### 4b. Enriquecimiento externo (fase 2.5, opcional)

Cuando la cascada termina con confianza baja o en "Otros", la pipeline puede consultar un servicio externo para averiguar qué es el comercio y volver a preguntar a Clef con esa información. Es el **único punto donde algo sale de la máquina del usuario y de ClusterTIC**, por eso:

- Desactivado por defecto (`config/enrichment.yaml: enabled`). `--dry-run` lista las consultas exactas antes de enviar nada.
- Solo sale la clave de comercio limpia más una pista regional. Nunca importes, fechas, frecuencias ni saldos. Pasa por el mismo `anonymize` que los `state` de Clef.
- Proveedores en orden: Nominatim (OpenStreetMap, sin clave, devuelve tipo de lugar), SearXNG propio si existe, DuckDuckGo, y Google Places solo con clave. Se para en el primero útil.
- Caché permanente por clave (`merchant_enrichment`) y registro de cada consulta (`web_calls`), análogo a `llm_calls`.
- La segunda ronda con Clef solo sobreescribe si mejora la confianza (`source=clef_web`) y nunca pisa `source=user`.

Implementación (`src/moooney/enrich/`): `settings.py` (config), `query.py` (construcción y
saneado de la consulta), `providers.py` (Nominatim, SearXNG, DuckDuckGo vía `ddgs`, Google
Places Text Search, limitador de ritmo), `runner.py` (selección de candidatas, caché, registro,
segunda ronda). Comando `moooney enrich` y flag `categorize --enrich`.

Detalle en `docs/PROMPT_FASE2_5.md`.

### 5. Métricas (SQL sobre SQLite, sin IA)
- Mensual: ingresos, gastos, ahorro neto, tasa de ahorro.
- Por categoría: importe, % del gasto, variación vs mes anterior y vs media 6 meses.
- Fijo vs variable: un gasto es fijo si es recurrente (mismo comercio, importe ±10 %, periodo ~30/90/365 días).
- Recurrentes detectados: tabla `recurring(merchant_key, amount_avg, period_days, last_seen, next_expected)`.
- Tendencias: media móvil 3 meses por categoría.

### 6. Presupuestos
- Tabla `budgets(category, month, limit_amount, source)`.
- `moooney budget init`: propone límites con la mediana de los últimos 6 meses por categoría, menos un recorte objetivo en las variables (p. ej. 10 %). El usuario los edita en `config/budgets.yaml` o por CLI.
- Los presupuestos se copian mes a mes salvo que el usuario los cambie. El LLM puede sugerir ajustes en el informe mensual, pero no los aplica.

### 7. Alertas inteligentes (reglas + explicación LLM)
Detección por reglas, siempre explicables:
- Presupuesto al 80 % y al 100 % de una categoría antes de fin de mes.
- Gasto anómalo: importe > 3 desviaciones típicas del histórico de ese comercio.
- Cargo duplicado: mismo comercio e importe en < 48 h.
- Nueva suscripción: comercio nuevo que se repite con periodo estable.
- Subida de precio en recurrente: importe > 5 % sobre la media.
- Ingreso esperado ausente: nómina no recibida en la ventana habitual.
- Saldo proyectado negativo antes de la próxima nómina.
- Comisiones bancarias detectadas.

Cada alerta se guarda en `alerts(id, type, severity, date, transaction_id, message, acknowledged)`. El LLM solo redacta y prioriza a partir de las alertas ya detectadas.

### 8. Previsión
- Cash flow de 1 a 3 meses: recurrentes conocidos + media de variables por categoría. Método estadístico simple, suficiente y explicable. Prophet o similar solo si más adelante hay valor claro.
- Saldo proyectado día a día hasta la próxima nómina.

### 9. Informe y consejos
- `moooney report --month YYYY-MM` genera Markdown en `reports/` (ignorado por git).
- El LLM recibe únicamente: tabla de agregados por categoría, variaciones, alertas activas, presupuestos y recurrentes. Devuelve 3-5 acciones concretas priorizadas por impacto en euros al mes.
- Se registra en `llm_calls(model, purpose, tokens, payload_hash)` qué se ha enviado, para auditar el egreso de datos.

## Modelos y reparto de tareas

Todos servidos desde el proxy LiteLLM de ClusterTIC (`https://litellm.clusterticgalicia.com`). Los `hosted_vllm/*` son vLLM propio; `openai/clef-flash` y `openai/traductor-nllb` usan el adaptador OpenAI-compatible pero apuntan a servidores propios (coste 0). Ninguno enruta a nubes externas.

| Tarea | Modelo | Por qué |
|---|---|---|
| Categorizar comercios, `flow_type`, pista de recurrencia | `clef-flash` vía proxy (reserva: `qwen3.8-flash-next-fp8` con JSON Schema) | Decisión estructurada con probabilidades, sin parseo libre. Ver "Acceso a Clef-Flash" |
| Priorizar alertas (severidad 0-3) | mismo backend `decide` | La detección sigue siendo por reglas |
| Similitud entre claves de comercio | `bge-m3` | Embeddings, coste 0, evita consultar al modelo para variantes de un comercio ya conocido |
| Informe mensual, consejos, propuesta de presupuesto | `qwen3.8-flash-next-fp8` | El más capaz del proxy para redactar; recibe solo agregados |
| Reserva barata para redacción | `gemma-4-e2b` | Si el flash-next no está disponible |

**Acceso a Clef-Flash: resuelto (verificado 2026-10-10).** Delante de Clef hay un adaptador propio de ClusterTIC que expone `/v1/chat/completions`. Se envía un chat completion a `model: clef-flash` cuyo único mensaje de usuario tiene como `content` un JSON serializado con `state` y `questions` (formato SystemOne). La respuesta llega en `choices[0].message.content` como JSON con `answers` y `usage`. Ejemplo real en `docs/clef_test.json` y `docs/clef_test.ps1`. Si el mensaje no es ese JSON, el adaptador devuelve 400 con "clef-flash no conversa". Límite de entrada 4096 tokens; una clave de comercio con tres preguntas consume ~500.

Respuesta observada para `MERCADONA -42.10 EUR x12`: `flow_type=expense` con 0,93, `category=supermercado` con solo 0,47 y `is_recurring` 0,40. Dos lecciones para la fase 2:
- La redacción del `state` y de las descripciones de cada opción pesa mucho. Hay que iterar el prompt con un conjunto de 30-50 claves etiquetadas a mano (fixtures sintéticos) y medir acierto y calibración antes de fiarse de la confianza.
- Las cadenas conocidas se resuelven antes por reglas y embeddings; Clef queda para lo genuinamente ambiguo, y su confianza decide si entra en `review`.

La capa `llm/` expone `decide(state, questions)` con dos backends que devuelven la misma estructura `{question_id: {choice, confidence, probabilities}}`: `ClefChatDecider` (por defecto, vía proxy) y `ChatJsonDecider` (reserva con `qwen3.8-flash-next-fp8` y JSON Schema, por si Clef cae). La clave del proxy se lee de la variable de entorno `LITELLM_API_KEY`; nunca se escribe en el repo.

## Privacidad y soberanía

- Capa `llm/` con interfaz única (`decide`, `embed`, `generate`) y dos backends: LiteLLM y Ollama local (donde `decide` se emula con generación JSON restringida si no hay Clef).
- Solo se usan modelos listados en `config/llm.yaml` con `self_hosted: true`. Si alguien añade un modelo externo al proxy, la app no lo usará por accidente.
- Anonimización antes de cualquier llamada: sin IBAN, sin saldo, sin nombres de personas, sin referencias numéricas largas. Los conceptos van con la clave normalizada, no el texto original.
- Modo `--offline` que bloquea toda llamada de red para auditar que el resto de la pipeline funciona sin IA.

## Stack

- Python 3.12 gestionado con `uv`.
- pandas + openpyxl (parseo), SQLite (almacenamiento, un solo fichero en `data/moooney.db`), pydantic (validación de salidas LLM), typer (CLI), watchdog (opcional, detección), pyyaml (configuración).
- Cliente LLM: SDK `openai` apuntando al proxy LiteLLM. Sin LangChain.
- UI: ninguna en la fase 1. Fase 2: Streamlit sobre el mismo SQLite.

## Estructura del repo

```
moooney/
  config/
    rules.yaml          # regex → categoría
    categories.yaml     # taxonomía cerrada
    budgets.yaml        # límites mensuales del usuario
    llm.yaml            # backend, modelo, flags de privacidad
  data/                 # ignorado por git
    excel/              # entrada manual
    clean/              # CSV normalizados
    moooney.db
  reports/              # ignorado por git
  src/moooney/
    cli.py
    ingest/      detect.py  parse_santander.py  load.py
    categorize/  normalize.py  rules.py  llm.py  review.py
    analytics/   metrics.py  recurring.py  budgets.py  alerts.py  forecast.py
    llm/         client.py  anonymize.py  prompts/
    report/      monthly.py
    db/          schema.sql  repo.py
  tests/
    fixtures/           # extractos sintéticos, nunca reales
  docs/DESIGN.md
```

## Fases

1. **Ingesta**: inspect, parse, dedupe, load. Verificable sin IA. ✔
2. **Categorización**: normalize, rules, memoria, LLM para desconocidos, review. ✔
2.5. **Enriquecimiento externo**: opcional, activado tras revisión del usuario. ✔
3. **Analítica**: métricas, recurrentes, presupuestos, alertas. ✔
4. **Web app (GUI)**: ver y controlar toda la pipeline desde el navegador, en local. ✔
4.5. **Diagnóstico, tendencias y previsión** en la web; librerías JS en local. ✔
5. **Empaquetado e informe**: docker compose, informe mensual con consejos del modelo, watch. ✔

Próximo (sin fecha): importación del Excel de la tarjeta de crédito y conciliación con la liquidación; más bancos (solo requiere alias de columnas y prefijos); etiquetas de usuario en movimientos sueltos.

## Preguntas abiertas

- Cómo expone el proxy LiteLLM el endpoint `/v1/systemone` de Clef-Flash (pass-through o llamada directa a SGLang).
- Formato exacto del Excel de Santander: se ajustará con `moooney inspect`, sin abrir el fichero desde aquí.
- Capital invertido antes del inicio de los datos (opcional, para `seed.yaml`).

## Decisiones tomadas

- 2026-10-10: modelo de categorización = Clef-Flash (Cloudflare, 9B, basado en Qwen3.5-9B, Apache-2.0). Modelo de redacción = qwen3.8-flash-next-fp8. Embeddings = bge-m3.
- 2026-10-10: una sola cuenta exportada; tarjetas, telepeaje e inversión se modelan con `flow_type`, no con cuentas separadas, hasta que exista importación de tarjeta.
- 2026-10-10: la inversión se hace dentro del propio Santander (fondos, planes, valores). Las suscripciones y aportaciones aparecen como cargos en la cuenta y se marcan `flow_type=investment`; los reembolsos, dividendos y cupones como `income` con subcategoría "rendimientos de inversión". Conceptos esperados: SUSCRIPCION, FONDO, APORTACION, PLAN DE PENSIONES, COMPRA/VENTA VALORES, DIVIDENDO, REEMBOLSO. Se verifican contra los datos reales al sembrar `rules.yaml`.
- 2026-10-10: el telepeaje es el Vía-T del Santander. Cargos tipo VIA-T, VIA T, PEAJE, AUTOPISTA, AUDASA → Transporte / peajes.
- 2026-10-10: el agente de OpenCode hace push a `origin/main` al cerrar cada fase.
- 2026-10-10 (fase 1, verificada sobre el Excel real): la exportación del Santander tiene una sola hoja ("Movimientos de tu cuenta"), filas 1-4 con Cuenta/IBAN/Titular/Saldo, cabecera de tabla en la fila 8 ("Fecha operación, Fecha valor, Concepto, Importe, Saldo, Divisa") y TODAS las celdas como texto: fechas `dd/mm/yyyy` e importes estilo es-ES (`1.234,56 €`). La celda del IBAN lleva un dígito suelto detrás; el parser recorta a 24 caracteres y guarda `account = ****NNNN`. Soportado `.xlsx`; `.xls` se rechaza con mensaje claro (exportar a xlsx).
- 2026-10-10 (fase 1): filas sin fecha o sin importe (separadores/totales) se omiten silenciosamente. El CSV limpio escribe importes con dos decimales y separador coma. La cabecera de una hoja no se confunde con la de la tabla porque la primera celda no vacía de esas filas es "Cuenta"/"Titular", no "Fecha".
- 2026-10-10 (fase 2): normalización de conceptos → clave de comercio determinista (quitar referencias numéricas, colapsar bizums/transferencias a particulares quitando nombres, marcador RECIBO corta mandatos). 1.252 conceptos crujidos → 562 claves.
- 2026-10-10 (fase 2): bizum enviado = `expense` (consumo), bizum recibido = `income`, transferencia a/de particular = `transfer_internal`. Nómina detectada por palabra NOMINA. Las devoluciones con prefijo DEVOLUCION son `Ingresos/income`; un reembolso que llega bajo la clave del comercio mantiene la categoría de gasto con importe positivo.
- 2026-10-10 (fase 2): `PAYPAL ADD TO BAL` es `transfer_internal` (recargar la propia cartera); los cargos genéricos `PAYPAL EUROPE` no revelan comercio y se resuelven por regla a `Otros/expense` (bloque irreducible sin fase 2.5). El colapso por marcador RECIBO agrupa sus referencias alfanuméricas en una única clave.
- 2026-10-10 (fase 2): anti-sesgo del state de Clef. El prefijo "Movimiento bancario." convertía Comisiones bancarias en sumidero para cualquier desconocido (conf ~0.8 con claves inventadas); la palabra "bancario" sola lo reproduce (probado con panel de 7 claves). El state definitivo es neutro: "Concepto de un apunte: {clave}. Importe medio ...". Los conocimientos de marca se trasladaron a las descripciones de las categorías (supermercados regionales, telepeaje WETACA, SUSCRIPCION+fondo = Ahorro e inversión ≠ Comisiones, AEAT = Ingresos, DEVOLUCION = Ingresos, Decathlon = Compras, bizum = gasto/ingreso nunca traspaso).
- 2026-10-10 (fase 2): criterio estricto de `card_settlement` = liquidación mensual de la tarjeta del propio banco (concepto LIQUIDACION DE TARJETAS); nada más debe llevar ese flow.
- 2026-10-10 (fase 2): prioridad de source en `merchants`: user > rule > embedding > clef > llm; la memoria con source=user nunca se reconsulta. Umbral de embeddings 0.92 (en la práctica hereda poco; Clef carga la cola larga — aceptado).
- 2026-10-10 (fase 2): métrica de "Otros" medida sobre el gasto bruto (suma de importes negativos). El criterio <10 % no se cumple en base bruta (≈16 %) por el bloque PayPal (≈6.500 €) y cargos oscuros; se documenta como tensión conocida y objetivo de la fase 2.5. Excluyendo transferencias/traspasos/inversión, la proporción baja notablemente.
- 2026-10-10 (revisión fase 2): WETACA es comida a domicilio, no telepeaje. Regla nueva de comida a domicilio (WETACA, GLOVO, UBER EATS, JUST EAT, DELIVEROO → Alimentación/restaurantes) antes de la de transporte; descripción de Transporte corregida.
- 2026-10-10 (revisión fase 2): una regla corregida no se reaplicaba a claves ya categorizadas (solo entran en la cascada claves sin entrada en `merchants`). Nuevo `categorize --refresh-rules`: reaplica `rules.yaml` a todo lo que no sea `source=user`. Sobre la base real cambió 3 claves de 562.
- 2026-10-10 (fase 2.5): fuente `clef_web` con prioridad entre `embedding` y `clef`. Disparador por defecto: confianza < 0,6 o categoría Otros, excluyendo `transfer_internal`, `income`, `card_settlement` y claves estructurales (bizum, transferencias, recibos, PayPal Europe). Tope de 50 consultas por ejecución, 1,5 s entre peticiones (Nominatim exige ≤ 1/s), resumen externo saneado (sin teléfonos ni referencias) y acotado a 600 caracteres antes de volver a Clef.
- 2026-10-10 (fase 2.5): `enrich --dry-run` sobre la base real: 305 candidatas, 225 con consulta válida, 80 descartadas por los patrones vetados. El usuario decide si activarlo tras revisar la lista en `reports/enrich_dry_run.txt`. Dato a vigilar: 305 de 562 claves están bajo el umbral 0,6, coherente con la calibración (Clef solo es fiable > 0,7); si la lista es demasiado larga, subir `max_confidence` no es la solución, bajarlo sí reduce consultas.
- 2026-10-11 (fase 2.5, revisión del dry-run por el usuario): NO se añade ninguna región al texto de la consulta. Un comercio puede estar en Sevilla, en Alemania o ser online, y "Galicia, España" sesgaba la búsqueda. Nominatim se acota por país con `countrycodes=es` (configurable); los buscadores reciben solo la clave. Un último token de 1-2 letras se descarta por ser ciudad truncada por el banco (SEVI, VI, MO). Si la clave empieza por un nombre de pila y tiene pinta de nombre y apellidos, no se consulta: los autónomos son personas. Alipay/WeChat se vetan (el comercio viene en pinyin, inencontrable).
- 2026-10-11 (revisión fase 2 derivada del dry-run): la normalización no quitaba "TRANSACCION CONTACTLESS EN" ni los prefijos de procesadores de pago (SUMUP, ZETTLE). Añadidos. Como cambiar la normalización renombra claves, nuevo `categorize --rebuild-keys`: recalcula `concept_clean` de todas las filas y copia a la clave nueva la entrada de memoria de la antigua (fuente y confianza incluidas), así una corrección manual sobrevive al renombrado. Sobre la base real: 16 transacciones cambiaron de clave, 10 entradas migradas.
- 2026-10-11 (reglas de marca a partir del dry-run): Zalando, Pull and Bear, Sprinter, Cortefiel, Hoff, Pedro del Hierro, Spacefoot, Fifty Factory → Compras/ropa; Petroprix, Plenoil, Ballenoil, Petrobierzo, Aral → combustible; Goiko y JustEat → restaurantes; TaxDown y AllDebrid → software; CINES → cultura. 23 claves cambiaron al reaplicar. Otros baja del 17,0 % al 15,6 % del gasto. Candidatas a enriquecimiento: 292 (185 con consulta, 107 descartadas).
- 2026-10-11 (fase 2.5, decisiones del usuario): "GA" al inicio de una clave es Gadis truncado por el banco (GA MOS, GA VI, GA PO) → Alimentación/supermercado. Los pagos ALIPAY/WEIXIN son gasto de un viaje a China → Ocio/viajes. Tras reconstruir claves, las entradas de memoria huérfanas (sin transacciones) se borran y solo las claves con transacciones pueden ser candidatas. "E S"/"ES" + nombre = estación de servicio → combustible. Otros queda en 12,6 % del gasto bruto antes de enriquecer.
- 2026-10-11 (fase 2.5): el usuario revisó dos dry-run y autorizó activar el enriquecimiento (`enabled: true`). Umbral de confianza 0,6 (coincide con la calibración de Clef: fiable > 0,7), tope 200 consultas por ejecución para que la tanda inicial (154) salga de una vez, 1,5 s entre peticiones.
- 2026-10-11 (fase 2.5, ejecución real): 219 candidatas, 65 descartadas, 154 consultadas. Nominatim respondió 51 (33 %), DuckDuckGo 72 de 75 intentos (20 errores de red registrados). 131 con resultado; la segunda ronda con Clef mejoró la confianza en 104 claves y cambió la categoría en 48. Otros baja del 12,6 % al 11,6 % del gasto bruto. Quedan 50 claves dudosas para `review` y 146 claves en Otros, la mayoría PayPal y cargos opacos. Fase 2.5 cerrada; el enriquecimiento queda activado para claves nuevas (caché por clave, no reconsulta).
- 2026-10-11 (fase 3): fecha de referencia = último movimiento cargado (`as_of`), no `hoy`: los datos llegan por exportación manual y pueden ir con retraso. Un mes es "completo" si termina antes de `as_of` y empieza después del primer dato; las comparaciones "vs media" solo usan meses completos. Gasto = `expense` + `card_settlement` (las devoluciones restan); ahorro neto = ingresos - gastos; la inversión se muestra aparte y no es gasto. Fijo = gasto en claves recurrentes activas.
- 2026-10-11 (fase 3): recurrencia por intervalos entre apariciones (semanal, quincenal, mensual, bimestral, trimestral, anual) con ≥ 70 % de intervalos dentro de ±30 % del periodo y ≥ 3 apariciones (2 para anual). El importe puede variar: `amount_cv` (MAD/mediana) ≤ 0,15 marca importe fijo. Activo si han pasado ≤ 1,6 periodos desde la última. Sobre la base real: 20 recurrentes, 12 activos.
- 2026-10-11 (fase 3): presupuesto propuesto = mediana del gasto por categoría en los últimos 6 meses completos, -10 % si la categoría es variable (< 70 % recurrente), redondeado a 5 € al alza. `config/budgets.yaml` está ignorado por git (importes personales); la tabla `budgets` guarda el límite vigente por mes con `UNIQUE(category, month)`.
- 2026-10-11 (fase 3): alertas con `dedup_key` única (migración: `ALTER TABLE alerts ADD COLUMN`), así re-ejecutar no duplica. El ingreso ausente se evalúa sobre todos los recurrentes de ingreso (no solo activos), porque faltar es justo lo que los vuelve inactivos; se deja de avisar pasados 3 periodos. Anomalía = importe > mediana + 3·σ robusta (1,4826·MAD) y > 20 € sobre la mediana, con ≥ 5 cargos de histórico. Primera ejecución real: 6 alertas.
- 2026-10-11 (orden de fases): el usuario decide que la fase 4 es la **web app (GUI)** para ver y controlar toda la pipeline; informe, consejos y automatización pasan a la fase 5.
- 2026-10-11 (fase 4): web con FastAPI + Jinja + HTMX (sin build de frontend), servida solo en 127.0.0.1, sin autenticación. Las acciones largas (ingesta, categorizar, enriquecer, alertas) corren como trabajos en un hilo con registro de líneas y la página los sondea cada 2 s; solo un trabajo a la vez porque la base es una. Las ediciones (categoría de comercio, límite de presupuesto, alerta vista) responden fragmentos HTML. La memoria de comercios se edita por clave, no por movimiento: una corrección aplica a todos sus movimientos y queda como `source=user`.
- 2026-10-11 (fase 4): `moooney demo` genera `data/demo/` con configuración propia y 14 meses sintéticos deterministas (semilla fija) para ver la web sin datos reales; los tests de la web usan el mismo generador. El enriquecimiento de la demo se escribe siempre desactivado.
- 2026-10-11 (fase 4, bugs encontrados con la demo): la normalización dejaba en "SIN CLAVE" cualquier concepto que empezara por un marcador de corte (COMISION…, RECIBO…), así que las comisiones no tenían clave propia; ahora un marcador al principio no corta. La proyección del presupuesto extrapolaba linealmente también lo fijo ya pagado (alquiler del día 2 proyectado ×3); ahora solo extrapola la parte variable.
- 2026-10-11 (fase 4): HTMX y Chart.js se cargan desde CDN con versión fija (cdnjs no tiene Chart.js 4.4.7; se usa jsDelivr). Empaquetarlos en `static/` queda para la fase 5 junto con el docker compose.
- 2026-10-11 (fase 4.5, diagnóstico): hallazgos deterministas ordenados por severidad e impacto en €/mes. Para el mes incompleto, las categorías se comparan a prorrata de días transcurridos y se omiten las dominadas por cargos fijos (un alquiler del día 2 no "se dispara" el día 10). Previsión: fin de mes = gastado + recurrentes que vencen en lo que queda + variable medio por día de los últimos 3 meses completos × días restantes; próximos meses = fijo normalizado a 30 días + variable medio + ingresos medios; colchón = saldo / gasto medio; saldo proyectado = saldo + ahorro neto medio × meses. Todo reproducible con una consulta.
- 2026-10-11 (fase 5, informe): el modelo de texto (`models.redaccion`, Qwen) recibe un JSON de agregados (resumen, serie, categorías con variación, hallazgos, recurrentes, alertas, presupuesto, previsión, colchón) acotado a 12.000 caracteres y pasado por `assert_safe_text` (mismas barreras que un state, tope propio). Prompt de sistema con estructura fija y prohibición de inventar cifras y de aconsejar productos financieros. Cada informe se guarda en la tabla `reports` (histórico de versiones) y en `reports/YYYY-MM.md`. Sin modelo u offline, `fallback_report` ordena los hallazgos deterministas.
- 2026-10-11 (fase 5, docker): imagen `python:3.12-slim` + uv, sin datos dentro (`.dockerignore`), `config/` copiado como valores por defecto y sustituido por el volumen del compose. El proceso escucha en 0.0.0.0 dentro del contenedor y el compose publica el puerto solo en `127.0.0.1`. Perfil `watch` opcional con el mismo código de la web (`service.job_*`). Probado: build y demo servida desde el contenedor.
- 2026-10-11 (fase 5, watch): sondeo por hash cada N segundos, sin dependencias; `--once` para el Programador de tareas de Windows o cron. Corre ingesta → categorización → enriquecimiento (si está activado) → alertas.
- 2026-10-11 (sistema visual, guía del usuario): tipografía Fraunces (display: titulares y cifras) + Work Sans (grotesca, texto), solo pesos 200 y 800; nunca Inter, Roboto ni fuentes del sistema. Un color dominante (verde profundo `--dominant`) y un acento (coral `--accent`), todo en variables CSS; positivo = dominante, negativo = acento; paleta de gráficos derivada (`--chart-1..9`). Sin degradados ni tarjetas con borde: la jerarquía sale del tipo y del espacio, con hairlines solo en tablas y en la franja de cifras. Ritmo de 8 px (`--s1..--s8`). Las fuentes (woff2, subsets latin y latin-ext, OFL) se sirven desde `static/vendor/fonts/` con su `fonts.css`: la web no hace ninguna petición externa.
- 2026-10-11 (demo por defecto, petición del usuario): si la base real no tiene movimientos, la web construye la demo en `data/demo/` (generador determinista del repo, no una base binaria en git) y la sirve con un aviso en todas las páginas y una etiqueta en la barra. La ingesta siempre escribe en la base real; en cuanto hay movimientos reales se usan sin reiniciar. `web --demo` fuerza la demo. Presupuestos, enriquecimiento e informes hechos en modo demo se guardan en `data/demo/config` y no tocan la configuración del usuario.
- 2026-10-11 (revisión UX, parche externo revisado e integrado): la interfaz nunca enseña identificadores internos (`card_settlement`, `clef_web`, `anomalous_amount`…); `app.py` expone etiquetas en castellano como globales de Jinja (`FLOW_LABEL`, `SOURCE_LABEL`, `SEVERITY_LABEL`, `ALERT_TYPE_LABEL`, `ALERT_EXPLAIN`) y filtros `fecha` (`6 oct`) y `mes` (`octubre 2026`). Resumen con franja «ahora mismo» (dudosas, alertas, ahorro previsto) y diagnóstico con ahorro potencial. Comercios con **cola de revisión** de dudosas (una clave cada vez, ordenadas por euros, sugerencias = categoría actual + las más usadas en tu memoria, teclas 1-3/↵/S, `skip` en la URL para saltar sin tocar nada); el formulario manda un hidden vacío y el botón pulsado, y el servidor se queda con el último valor no vacío. Movimientos con totales del filtro completo y categoría editable en la fila (corrige el comercio). Recurrentes con calendario a 30 días y saldo que va quedando. Presupuestos con marca del día de hoy, columna «Queda» y alta de límite sin recargar; el recorte de la propuesta se pide en % (10) y se convierte a tanto por uno. Alertas agrupadas por regla con explicación, filtro por nivel y «marcar el grupo». Pipeline con stepper de cinco pasos con estado. Fallos corregidos al integrar: `q` del buscador pisado por la variable de la cola, `SUM` nulo en `merchants_summary` con la tabla vacía, desbordamiento horizontal por tablas anchas (`min-width: 0` en el main de la rejilla), tipo de informe `reglas`/`modelo` (no `fallback`).
- 2026-10-11 (dudosas, cómo bajarlas sin revisar a mano): agregados de la base real: 222 claves dudosas, 145 en Otros, pero 3 claves con más de 10 movimientos concentraban el 63 % de los euros de Otros y 111 claves de un solo movimiento solo el 21 %. Tres medidas. (1) **Tipo de operación en el estado** (`normalize.operation_type`: recibo domiciliado, compra con tarjeta, bizum, transferencia…), que se perdía al construir la clave y es una pista fuerte; `KeyJob.op_type` y `build_state` lo incluyen; `categorize --retry-dudosas` (`categorize/reask.py`) vuelve a preguntar al decider por las dudosas con ese estado y con el resumen web si existe, y guarda solo si la confianza mejora (misma fuente, `force=True` en `upsert_merchant`, nunca sobre `user`). Resultado real: 12 de 222 mejoran; Clef ya había dicho lo que sabía. (2) **Segunda opinión del modelo de texto** (`categorize/second_opinion.py`): Qwen recibe por lotes de 15 la misma información que Clef más el resumen web y los criterios de la taxonomía, responde JSON con categoría, confianza y porqué; sustituye solo si confianza ≥ 0,7 y no Otros→Otros, con fuente `llm` (etiqueta «segunda opinión»). Resultado real: de 210 dudosas, 38 cambian y 49 se confirman; quedan 123. (3) **Cola «las que importan»** en Comercios: por defecto solo claves con ≥ 2 movimientos o ≥ 30 €; la cola larga se resume en euros y se abre con `?queue=all`. En la base real: 52 claves que importan, 71 pequeñas que suman 689 €. Ambos pasos tienen botón en Pipeline (`categorize-retry`, `second-opinion`).
- 2026-10-11 (multibanco, BBVA): el usuario aporta un «Últimos movimientos» del BBVA. Diferencias: columna A vacía, título y «Fecha de generación» encima (una fila que empieza por «Fecha» y no es cabecera), cabecera `F.Valor | Fecha | Concepto | Movimiento | Importe | Divisa | Disponible | Divisa | Observaciones`, sin IBAN, el tipo de operación en una columna aparte (`Movimiento`: «Pago con tarjeta», «Adeudo nº …», «Enviado: nota» para Bizum) y el número de tarjeta en `Observaciones`. Decisiones: (1) la cabecera se detecta **por columnas** (primera fila que casa ≥ 2 alias; si faltan obligatorias el error dice cuál), ya no por «primera celda con fecha»; (2) `settings.yaml` admite `banks:` con perfiles (`name`, `account` fijo o `iban`, `concept_builder`, alias) que se prueban en orden tras el Santander; (3) el constructor `ingest/banks.bbva` reescribe `Concepto + Movimiento` a la forma del Santander («COMPRA EN …», «RECIBO …», «BIZUM ENVIADO: …», «TRANSFERENCIA ENVIADA …», «RETIRADA DE EFECTIVO EN CAJERO», «NOMINA …») para que claves, tipo de operación y reglas sigan valiendo sin tocar nada; (4) `date` = `F.Valor` (día real de la compra; la tarjeta se contabiliza días después en `Fecha`, que va a `value_date`); (5) `Observaciones` no se mapea nunca; (6) cuenta = etiqueta `bbva`, así las huellas no chocan con las del Santander y ambos conviven en la misma base. Pendiente de confirmar con un Excel real con importes: el formato numérico de `Importe`/`Disponible` (la muestra venía sin ellos; el parser acepta número y texto es-ES).
- 2026-10-10 (fase 2): `eval-clef` mide al decider contra `data/eval/merchants_labeled.csv` (50 claves reales etiquetadas a mano, git-ignored; BOM tolerado con utf-8-sig). No escribe en `merchants`. Umbrales: categoría >85 %, flow_type >95 %. Resultado final: categoría 90 %, flow_type 100 %. La calibración muestra que la confianza solo es fiable por encima de 0.7 (95-100 % de acierto); por debajo, ~83 %.
