# AGENTS.md — moooney

Contexto permanente para cualquier agente de IA que trabaje en este repositorio. Léelo entero antes de tocar nada. El diseño completo está en `docs/DESIGN.md` y manda sobre este fichero en lo técnico. Los diagramas (Mermaid) están en `docs/ARQUITECTURA.md`; actualízalos cuando cambie una etapa, una tabla o una frontera de datos.

## Qué es este proyecto

Pipeline local de finanzas personales. El usuario exporta a mano extractos de su cuenta del Santander (Excel), la pipeline los detecta, los limpia a CSV, los carga en SQLite, categoriza cada movimiento con un modelo de decisión (Clef-Flash) y calcula métricas, presupuestos, alertas, previsiones y consejos. Objetivo: mejorar la economía mensual del usuario sin que sus datos salgan de su máquina ni de la infraestructura de su organización.

## Reglas inviolables

1. **Los datos reales se quedan en esta máquina y en la infraestructura de ClusterTIC.** Puedes abrir `data/excel/*`, `data/clean/*` y `data/moooney.db` para entender el formato y depurar el parser, porque los modelos que usas son autoalojados. Pero lee solo lo necesario (cabeceras, unas pocas filas, agregados), y nunca copies filas reales a commits, tests, fixtures, docstrings ni a `docs/`. Los ejemplos que escribas en código o documentación son siempre inventados.
2. **Nunca subas datos ni secretos a git.** `data/`, `reports/` y los ficheros `.env` están en `.gitignore`. Comprueba con `git status` antes de cada commit que no aparece nada de esas carpetas. La clave del proxy vive en la variable de entorno `LITELLM_API_KEY` y jamás en código, config o commits.
3. **Nunca envíes datos crudos a un modelo.** A los modelos solo van claves de comercio normalizadas (sin IBAN, sin saldos, sin nombres de personas, sin referencias numéricas largas) o agregados mensuales. Toda llamada pasa por la capa `src/moooney/llm/` y se registra en la tabla `llm_calls`. La única salida fuera de ClusterTIC es el enriquecimiento web de la fase 2.5: opcional, desactivado por defecto, solo claves de comercio, registrado en `web_calls`.
4. **Determinista antes que IA.** Parseo, deduplicación, métricas, presupuestos y detección de alertas son código normal y testeable. El modelo solo interpreta texto libre (conceptos) o redacta (informes).
5. **Idempotente.** Ejecutar la pipeline dos veces sobre el mismo Excel no duplica nada.
6. **Los tests usan datos sintéticos** generados en el propio test (`tmp_path`), nunca ficheros reales ni fixtures con movimientos verosímiles de una persona concreta.

## Stack y convenciones

- Python 3.12, gestionado con `uv`. Layout `src/moooney/`. Entrada por CLI con `typer`: `uv run moooney <comando>`.
- Dependencias: `pandas`, `openpyxl`, `pydantic`, `pyyaml`, `typer`, `rich`, `httpx`. Dev: `pytest`, `ruff`. Sin LangChain ni frameworks de agentes.
- SQLite en `data/moooney.db`, esquema en `src/moooney/db/schema.sql`, acceso con `sqlite3` de la biblioteca estándar. Sin ORM.
- Configuración en `config/*.yaml`. Rutas en `config/settings.yaml`.
- Código, nombres de variables, commits y docstrings en español salvo identificadores técnicos habituales en inglés (`date`, `amount`, `flow_type`).
- Formateo y lint con `ruff`. Tests con `pytest`. Todo verde antes de cada commit.
- La imagen Docker no contiene datos: `.dockerignore` excluye `data/`, `reports/`, `budgets.yaml` y `.env`. `config/` sí va dentro como valores por defecto.
- Commits pequeños y descriptivos, un tema por commit. Push a `origin/main` solo al cerrar cada fase, tras verificar con `git status` y `git log --stat -1` que no entra nada de `data/` ni `reports/`.
- Sistema visual de la web (guía del usuario, no negociable): Fraunces para display y Work Sans para texto, pesos 200 y 800, nunca Inter/Roboto/sistema; un dominante y un acento en variables CSS (`static/app.css`); sin degradados ni tarjetas con borde; ritmo de 8 px; jerarquía por tipo y espacio.
- Windows 11 y PowerShell. Usa rutas con `pathlib`, nunca cadenas con barras fijas. Codificación UTF-8 explícita al leer y escribir ficheros.

## Modelos disponibles (proxy LiteLLM de ClusterTIC)

Base URL `https://litellm.clusterticgalicia.com/v1`, API compatible con OpenAI, clave en `LITELLM_API_KEY`. Todos autoalojados.

| Modelo | Uso en este proyecto |
|---|---|
| `clef-flash` | Categorización y decisiones estructuradas. **No conversa.** Se llama por chat completions con un único mensaje de usuario cuyo `content` es un JSON serializado `{"state": ..., "questions": {...}}`. Responde en `message.content` con JSON `{"answers": {...}}`. Ejemplo funcional en `docs/clef_test.json` y `docs/clef_test.ps1`. Máximo 4096 tokens de entrada. |
| `qwen3.8-flash-next-fp8` | Redacción de informes y consejos a partir de agregados. Reserva para categorizar con JSON Schema si Clef no está disponible. |
| `bge-m3` | Embeddings para similitud entre claves de comercio. |
| `gemma-4-e2b` | Reserva barata de redacción. |

## Estructura objetivo

```
moooney/
  AGENTS.md
  pyproject.toml
  config/      settings.yaml  categories.yaml  rules.yaml  llm.yaml  enrichment.yaml  budgets.yaml (ignorado)  budgets.example.yaml
  data/        (ignorado) excel/  clean/  moooney.db
  reports/     (ignorado)
  docs/        DESIGN.md  ARQUITECTURA.md  PROMPT_FASE1.md  PROMPT_FASE2.md  PROMPT_FASE2_5.md  clef_test.json  clef_test.ps1
  src/moooney/
    cli.py
    config.py
    db/          schema.sql  repo.py
    ingest/      detect.py  parse_santander.py  load.py
    categorize/  normalize.py  keys.py  rules.py  embed.py  pipeline.py  evaluate.py  taxonomy.py
    enrich/      settings.py  query.py  providers.py  runner.py
    analytics/   periods.py  metrics.py  recurring.py  budgets.py  alerts.py
    llm/         client.py  anonymize.py  prompts/
    web/         app.py  queries.py  service.py  jobs.py  templates/  static/
    demo.py
    report/      advice.py
    ingest/      watch.py (además de detect, parse_santander, load)
  tests/
```

## Fases

1. **Ingesta** (en curso): `inspect`, parseo Santander → CSV limpio, deduplicación, carga en SQLite, `status`. Sin IA. Ver `docs/PROMPT_FASE1.md`.
2. **Categorización**: normalización de conceptos, memoria de comercios, reglas, embeddings, Clef-Flash, `review`. Ver `docs/PROMPT_FASE2.md`.
2.5. **Enriquecimiento externo** (opcional): buscador o mapas para comercios que la cascada no resuelve. Ver `docs/PROMPT_FASE2_5.md`.
3. **Analítica** (hecha 2026-10-11): métricas mensuales, recurrentes, presupuestos, alertas. Comandos `metrics`, `recurring`, `budget init|set|show`, `alerts`.
4. **Web app (GUI)** (hecha 2026-10-11): FastAPI + Jinja + HTMX en `src/moooney/web/`, comando `moooney web`; toda la CLI desde el navegador. `moooney demo` genera una base sintética (`src/moooney/demo.py`) para probarla y para los tests de la web.
4.5. **Diagnóstico y previsión** (hecho 2026-10-11): `analytics/insights.py` y `analytics/forecast.py`, página `/trends` y tarjeta de diagnóstico en el resumen. htmx y Chart.js servidos desde `static/vendor/`.
5. **Empaquetado e informe** (hecho 2026-10-11): `Dockerfile` + `docker-compose.yml` (volúmenes `data/`, `config/`, `reports/`; puerto solo en 127.0.0.1; perfil `watch`), informe mensual con el modelo de texto sobre agregados (`report/advice.py`, página `/report`, comando `report`), vigilancia de la carpeta (`ingest/watch.py`, comando `watch`).

## Cómo trabajar con el usuario

- Ejecuta tú mismo los comandos sobre el Excel real para validar el parser y la carga. Al usuario repórtale solo agregados: columnas mapeadas, filas cargadas, rangos de fechas, resultado de los tests.
- Si el parser falla con el Excel real, inspecciona las cabeceras y ajusta el mapeo de columnas en `config/settings.yaml`, no en código.
- Cuando dudes entre dos interpretaciones que cambian el resultado, pregunta. Cuando sea una decisión rutinaria, decide y anótala en `docs/DESIGN.md` bajo "Decisiones tomadas".
