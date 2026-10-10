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

## Privacidad

`data/`, `reports/` y los `.env` están ignorados por git. Ningún comando imprime conceptos,
importes individuales, saldos ni el IBAN completo. Los tests usan únicamente datos sintéticos.

## Desarrollo

```powershell
uv run pytest        # tests
uv run ruff check .  # lint
```
