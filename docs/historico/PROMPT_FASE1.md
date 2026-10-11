# Prompt de arranque — Fase 1: ingesta

Copia desde la línea siguiente hasta el final y pégalo como primer mensaje en OpenCode.

---

Lee `AGENTS.md` y `docs/DESIGN.md` antes de empezar. Vas a implementar la **fase 1 (ingesta)** del proyecto moooney. No uses ningún modelo de IA dentro de la pipeline en esta fase: todo es código determinista y testeable. Puedes abrir el Excel real de `data/excel/` para entender su formato y validar el parser (regla 1 de `AGENTS.md`), pero nunca copies filas reales a código, tests, fixtures ni documentación.

## Estado actual del repo

- Existe `.gitignore` (excluye `data/`, `reports/`, secretos, `*.xlsx`, `*.csv`), `README.md`, `AGENTS.md`, `docs/DESIGN.md` y dos ficheros de prueba de Clef en `docs/`.
- En `data/excel/` hay un Excel real del Santander con dos años de movimientos de una cuenta. Úsalo para verificar el formato y probar `inspect` e `ingest` de principio a fin.
- Hay un commit inicial. Haz commit de `AGENTS.md` y `docs/` como primer paso.

## Entregables

### 1. Proyecto

- `uv init` con layout `src/moooney/`, Python 3.12. Dependencias: `pandas`, `openpyxl`, `pydantic`, `pyyaml`, `typer`, `rich`. Dev: `pytest`, `ruff`.
- Script de consola `moooney` → `moooney.cli:app`. Debe funcionar `uv run moooney --help`.
- `config/settings.yaml` con rutas (`excel_dir`, `clean_dir`, `db_path`) y el mapeo de columnas del parser (ver punto 3). `src/moooney/config.py` lo carga con pydantic.

### 2. Base de datos

`src/moooney/db/schema.sql` con todas las tablas del diseño, aunque en esta fase solo se usen las dos primeras:

- `imports(id, sha256 UNIQUE, filename, imported_at, rows_total, rows_new, date_min, date_max)`
- `transactions(id, fingerprint UNIQUE, date, value_date, concept, concept_clean, amount, balance, account, flow_type, category, subcategory, confidence, import_id, created_at)`
- `merchants(key PRIMARY KEY, category, subcategory, flow_type, source, confidence, updated_at)`
- `budgets(id, category, month, limit_amount, source)`
- `alerts(id, type, severity, date, transaction_id, message, acknowledged)`
- `llm_calls(id, ts, model, purpose, input_tokens, output_tokens, payload_hash)`

`src/moooney/db/repo.py`: `connect()` que crea el fichero y aplica el esquema si no existe, y funciones de acceso para `imports` y `transactions`. `sqlite3` estándar, sin ORM.

### 3. Parser de Santander (`src/moooney/ingest/parse_santander.py`)

Función `parse(path) -> pandas.DataFrame` con columnas exactas `date, value_date, concept, amount, balance, account`.

Lo que se sabe del formato de exportación del Santander España (verifícalo abriendo las cabeceras del Excel real antes de escribir el parser, no lo des por hecho):

- Fichero `.xlsx`, una hoja. Varias filas de cabecera con datos del titular, IBAN y saldo antes de la tabla real.
- La tabla empieza en la fila cuya primera celda no vacía contiene "Fecha" (ignorar mayúsculas y acentos).
- Columnas habituales: "Fecha Operación", "Fecha Valor", "Concepto", "Importe", "Saldo". Puede haber columnas vacías intermedias.
- Fechas `dd/mm/yyyy` o celdas de tipo fecha. Importes con coma decimal y punto de miles, a veces como texto, a veces numéricos; negativos para cargos.
- El IBAN puede aparecer en las filas de cabecera. Si se encuentra, `account` = últimos 4 dígitos precedidos de `****`. Si no, `account` = `"principal"`.

Requisitos:

- Mapeo de nombres de columna configurable en `config/settings.yaml` (lista de alias por cada columna destino), para ajustarlo sin tocar código.
- Normalización: fechas a ISO `YYYY-MM-DD`, importes a `float` con punto decimal, `concept` en mayúsculas con espacios colapsados.
- Si no se localiza la fila de cabecera o falta alguna columna obligatoria, lanza una excepción cuyo mensaje liste **solo** los nombres de columna encontrados, nunca valores de celdas.

### 4. Comando `inspect`

`uv run moooney inspect <ruta>`: muestra nombre de hoja, índice de la fila de cabecera detectada, nombres de columna originales y a qué columna destino se han mapeado, número de filas de datos, fecha mínima y máxima, y suma de control (número de importes positivos y negativos). No muestra conceptos, importes individuales, saldos ni el IBAN completo: es el comando que el usuario usará en el futuro con cada Excel nuevo para comprobar que el parser lo entiende.

### 5. Comando `ingest`

`uv run moooney ingest [--dir data/excel]`:

1. Lista los `.xlsx` y `.xls` del directorio, calcula SHA-256 de cada uno y omite los que ya estén en `imports`.
2. Para cada fichero nuevo: `parse`, escribe `data/clean/<YYYYMMDD>_<sha8>.csv` (UTF-8, separador coma, cabecera fija), calcula la huella `sha1(account|date|amount|concept|balance)` por fila, inserta solo las huellas que no existan en `transactions`, registra el import con `rows_total` y `rows_new`.
3. Al final imprime una tabla con `rich`: fichero, filas totales, filas nuevas, rango de fechas. Nada más.
4. Idempotente: ejecutarlo dos veces seguidas debe reportar 0 filas nuevas la segunda vez. Si un segundo Excel solapa con el primero, solo entran las filas nuevas.

### 6. Comando `status`

`uv run moooney status`: número de transacciones, rango de fechas, número de imports, fecha del último import, número de transacciones sin categoría. Solo agregados.

### 7. Tests (`tests/`)

- Un helper que genera un `.xlsx` sintético en `tmp_path` con la estructura del Santander descrita arriba (filas de cabecera, IBAN falso, columna vacía intermedia, importes como texto con coma, fechas como texto y como fecha). Datos inventados y obviamente falsos.
- Tests de: detección de cabecera, mapeo de columnas con alias, normalización de importes y fechas, enmascarado de IBAN, huella estable, idempotencia de `ingest` (dos ejecuciones, segunda con 0 nuevas), solapamiento parcial entre dos ficheros, error claro cuando falta una columna.
- `uv run pytest` y `uv run ruff check .` en verde.

## Flujo de trabajo

1. Commit de `AGENTS.md` y `docs/`.
2. Proyecto + esquema + `status` vacío. Commit.
3. Parser + `inspect` con tests sintéticos. Commit.
4. Ejecuta `uv run moooney inspect data/excel/<fichero>.xlsx` sobre el Excel real. Ajusta `settings.yaml` si el mapeo no cuadra. No sigas hasta que `inspect` muestre las cinco columnas mapeadas.
5. `ingest` + tests de idempotencia. Commit.
6. Ejecuta `uv run moooney ingest` dos veces y luego `uv run moooney status`. La primera carga debe dar `rows_new` igual a `rows_total` del `inspect`, la segunda 0 filas nuevas, y `status` un rango de unos dos años. Comprueba con `git status` que nada de `data/` aparece como cambio. Entonces la fase 1 está cerrada.
7. Actualiza `docs/DESIGN.md` con cualquier decisión tomada y añade a `README.md` cómo ejecutar los tres comandos. Commit final de la fase.

## Criterios de aceptación

- `uv run moooney inspect` sobre el Excel real muestra cabecera detectada y las cinco columnas mapeadas, sin revelar ningún valor.
- `uv run moooney ingest` carga el Excel real y una segunda ejecución reporta 0 filas nuevas.
- `uv run moooney status` muestra un rango de ~2 años.
- `git status` limpio y sin rastro de `data/` en ningún commit.
- Tests y lint en verde.

No empieces la fase 2. Cuando termines, resume en tres líneas qué has hecho, cuántas transacciones hay cargadas y qué rango de fechas cubren.
