# moooney

Pipeline local de finanzas personales. Analiza los extractos exportados del Banco Santander
sin enviar ningún dato bancario fuera de la máquina o de la infraestructura propia.

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
embedding > clef > llm) y todo lo que hace el modelo queda registrado en `llm_calls`.

## Privacidad

`data/`, `reports/` y los `.env` están ignorados por git. Ningún comando imprime conceptos,
importes individuales, saldos ni el IBAN completo. Los tests usan únicamente datos sintéticos.

## Desarrollo

```powershell
uv run pytest        # tests
uv run ruff check .  # lint
```
