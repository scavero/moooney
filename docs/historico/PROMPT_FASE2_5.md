# Prompt de arranque — Fase 2.5: enriquecimiento externo de comercios

Copia desde la línea siguiente hasta el final y pégalo en OpenCode **cuando la fase 2 esté cerrada** (categorización ejecutada, `eval-clef` con cifras, push hecho).

---

Lee `AGENTS.md` y la sección "4b. Enriquecimiento externo" de `docs/DESIGN.md`. Vas a implementar la **fase 2.5**: cuando la cascada de categorización no sabe qué es un comercio, la pipeline consulta un buscador o un servicio de mapas para averiguarlo y vuelve a preguntar a Clef con esa información.

## Por qué hay que tener cuidado

Es el **único punto de toda la pipeline donde algo sale de la máquina del usuario y de la infraestructura de ClusterTIC**. Lo que sale es solo el nombre normalizado de un comercio (p. ej. `CAFETERIA EL GATO AZUL VIGO`), nunca importes, fechas, frecuencias, saldos ni nada que identifique al usuario. Aun así, el conjunto de consultas revela qué sitios frecuenta, así que esta función es **opcional, desactivada por defecto, con lista previa de consultas y con registro de todo lo enviado**.

## Entregables

### 1. Configuración (`config/enrichment.yaml`)

```yaml
enabled: false              # el usuario lo activa a mano
trigger:
  max_confidence: 0.6       # se enriquece si Clef queda por debajo
  also_if_category: [Otros]
  skip_flow_types: [transfer_internal, income, card_settlement]
  skip_patterns: ["^BIZUM", "^TRANSFERENCIA", "^RECIBO", "^COMISION", "^RETIRADA"]
providers:                  # en orden; se para en el primero con resultado útil
  - nominatim               # OpenStreetMap, sin clave, devuelve tipo de lugar
  - searxng                 # buscador propio si existe (url abajo); si no, se salta
  - duckduckgo              # buscador público sin clave
  # - google_places         # requiere clave y facturación; opcional
searxng_url: null           # p. ej. https://searx.clusterticgalicia.com
google_places_api_key_env: GOOGLE_PLACES_API_KEY
region_hint: "Galicia, España"
rate_limit_seconds: 1.5
max_queries_per_run: 50
max_summary_chars: 600
```

### 2. Construcción de la consulta (`src/moooney/enrich/query.py`)

`build_query(merchant_key) -> str | None`:
- Parte de la clave de comercio ya normalizada (fase 2). Elimina cualquier dígito restante, tokens de una letra y palabras genéricas de la operación (`COMPRA`, `PAGO`, `TARJETA`, `ADEUDO`).
- Si quedan menos de 2 tokens o la clave casa con `skip_patterns`, devuelve `None` (no se consulta).
- Si la clave contiene una ciudad reconocible, se conserva; si no, se añade `region_hint`.
- Nunca incluye importe, fecha, número de ocurrencias ni nada que no esté en la clave.
- Pasa por `llm.anonymize.assert_safe` igual que un `state` de Clef.

### 3. Proveedores (`src/moooney/enrich/providers/`)

Interfaz común `Provider.lookup(query) -> Enrichment | None` con `Enrichment(provider, query, summary, place_types: list[str], url: str | None)`.

- **`nominatim.py`**: `https://nominatim.openstreetmap.org/search?q=...&format=jsonv2&limit=3`. Cabecera `User-Agent: moooney/0.1 (uso personal)` obligatoria por su política de uso, máximo 1 petición por segundo. Del resultado se usan `category`, `type`, `display_name` (p. ej. `amenity/cafe`, `shop/supermarket`, `amenity/fuel`). Devuelve `place_types=[f"{category}/{type}"]` y `summary` con el nombre y el tipo. Es el proveedor más útil para comercios físicos de Galicia y el que menos información filtra.
- **`searxng.py`**: `GET {searxng_url}/search?q=...&format=json`. Solo si `searxng_url` está configurado. Toma título y snippet de los 3 primeros resultados.
- **`duckduckgo.py`**: paquete `ddgs` (`DDGS().text(query, max_results=3, region="es-es")`). Título y snippet.
- **`google_places.py`** (opcional): Places API Text Search. Solo si la variable de entorno con la clave existe. Usa `types` y `formatted_address`. Documenta que requiere facturación en Google Cloud.

Todos: `httpx` con timeout 10 s, sin reintentos agresivos, respeto de `rate_limit_seconds`, y truncado del `summary` a `max_summary_chars`.

### 4. Caché y registro

- Tabla `merchant_enrichment(key PRIMARY KEY, provider, query, summary, place_types, url, fetched_at)`. Una clave se consulta una sola vez en la vida del proyecto salvo `--force`.
- Tabla `web_calls(id, ts, provider, query, status, payload_hash)`: registro de **cada** consulta externa, exista resultado o no. Es el equivalente de `llm_calls` para el egreso web.
- Añade ambas a `schema.sql` y a `docs/DESIGN.md`.

### 5. Segunda ronda con Clef

Para cada clave enriquecida, se vuelve a llamar al `Decider` con el mismo esquema de preguntas y un `state` ampliado:

```
Movimiento bancario. Comercio: <clave>. Importe medio: <redondeado>. Ocurrencias: <n>.
Información externa (<provider>): <summary>. Tipo de lugar: <place_types>.
```

Si la nueva confianza supera la anterior, se guarda en `merchants` con `source=clef_web` y la confianza nueva; si no, se conserva la anterior y se anota en `merchant_enrichment` que no ayudó. Nunca se baja de `source=user`.

### 6. Comando `enrich`

`uv run moooney enrich [--dry-run] [--limit N] [--force] [--key CLAVE]`:
- Selecciona las claves que cumplen el disparador y no están en caché.
- **`--dry-run` imprime la lista exacta de consultas que se enviarían y a qué proveedor, sin enviar nada.** El usuario debe ejecutarlo siempre primero.
- Sin `--dry-run`, si `enabled: false` en la configuración, aborta con un mensaje que explique cómo activarlo.
- Al terminar imprime: consultas enviadas por proveedor, cuántas mejoraron la confianza, cuántas cambiaron de categoría, y las que siguen por debajo del umbral (para `review`).
- `MOOONEY_OFFLINE=1` bloquea también estos proveedores.

### 7. Integración en `categorize`

Flag `--enrich` en `uv run moooney categorize` que encadena `enrich` tras la cascada, respetando `enabled`. Sin el flag, nada cambia respecto a la fase 2.

### 8. Tests

Sintéticos, con los proveedores sustituidos por dobles que devuelven respuestas inventadas:
- `build_query` elimina dígitos y genéricos, devuelve `None` para claves vetadas y añade la región.
- `assert_safe` rechaza una consulta con dígitos largos.
- Caché: segunda llamada a la misma clave no toca el proveedor.
- `web_calls` registra una fila por consulta, incluso sin resultado.
- La segunda ronda solo sobreescribe si mejora la confianza y nunca pisa `source=user`.
- `--dry-run` no llama a ningún proveedor.
- Modo offline bloquea.

## Flujo de trabajo

1. Configuración + `build_query` + tests. Commit.
2. Proveedores Nominatim y DuckDuckGo con dobles en tests. Commit.
3. Caché, `web_calls`, segunda ronda con Clef. Commit.
4. Comando `enrich` y flag `--enrich`. Commit.
5. Ejecuta `uv run moooney enrich --dry-run` sobre los datos reales y **muestra al usuario la lista de consultas**. No actives `enabled` ni envíes nada: esa decisión es suya.
6. Actualiza `docs/DESIGN.md` y `README.md` (sección de privacidad explicando qué sale, a dónde y cómo verlo en `web_calls`). Push.

## Criterios de aceptación

- Con `enabled: false` no se hace ninguna petición externa, y los tests lo demuestran.
- `--dry-run` lista cada consulta con su proveedor antes de enviar nada.
- Cada consulta real queda en `web_calls`.
- Las consultas solo contienen la clave de comercio limpia y la región.
- Tests y lint en verde. Nada de `data/` en git.

Al terminar, resume en cuatro líneas: cuántas claves cumplen el disparador, cuántas consultas haría el `--dry-run`, por qué proveedor, y recuerda al usuario que debe activar `enabled` para ejecutarlo de verdad.
