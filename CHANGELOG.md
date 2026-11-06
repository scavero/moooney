# Cambios

## Sin publicar (tras 1.0.0)

- **Dudosas sin revisar a mano**: tipo de operación en lo que ve el modelo, `categorize --retry-dudosas`
  (el decider vuelve a preguntar con el tipo de operación y el resumen web) y `categorize --second-opinion`
  (el modelo de texto revisa por lotes; sustituye solo con confianza ≥ 0,7). Botones en *Pipeline*.
- **Comercios**: la cola de revisión es la página: clave como titular, evidencia (últimos movimientos,
  categoría actual, resumen web), sugerencias con tecla, tabla completa plegada; por defecto solo las claves
  que importan (≥ 2 movimientos o ≥ 30 €).
- **Resumen**: cifra de ahorro neto y veredicto en una frase; KPIs en una línea con saldo y colchón;
  diagnóstico con los euros primero.
- **Multibanco**: perfiles en `settings.yaml` (`banks:`), cabecera detectada por columnas, Excel del BBVA.
- **Varias cuentas**: saldo = suma del último saldo de cada cuenta; filtro y columna de cuenta en Movimientos.
- **Etiquetas** libres en movimientos sueltos, con filtro, totales, CSV y comando `tag`.
- **Exportar CSV** desde Movimientos y comando `export`; tarea programada documentada; licencia MIT; CI.
- Revisión UX de toda la interfaz: etiquetas humanas, fechas cortas, chips, totales del filtro, calendario
  de cargos, marca «hoy» en presupuestos, alertas por grupo, stepper en Pipeline.

## 1.0.0 · 2026-10-11

Primera versión completa. Todo corre en local; a los modelos (autoalojados) solo van claves de
comercio limpias o agregados mensuales.

- **Ingesta** de los Excel exportados del Santander: limpieza a CSV, deduplicación por huella y
  carga en SQLite. `inspect` comprueba un fichero sin leer valores.
- **Categorización en cascada** por clave de comercio: reglas → memoria → embeddings → Clef-Flash
  (modelo de decisión). Tus correcciones mandan sobre todo lo demás.
- **Enriquecimiento externo opcional** (Nominatim, SearXNG, DuckDuckGo, Google Places) para las
  claves que siguen dudosas, con vista previa de las consultas y auditoría en `web_calls`.
- **Analítica determinista**: métricas mensuales, recurrentes, presupuestos (mediana de meses
  completos), ocho alertas, diagnóstico con impacto en €/mes, previsión a fin de mes y a tres meses,
  colchón y saldo proyectado.
- **Informe mensual** con consejos redactados por el modelo de texto a partir de agregados; versión
  determinista sin modelo.
- **Web local** (FastAPI + HTMX) con todo lo anterior: resumen, tendencias, movimientos con
  exportación a CSV, comercios con cola de revisión, recurrentes, presupuestos, alertas, informe y
  pipeline. Demo sintética por defecto mientras no haya datos propios.
- **CLI** con el mismo alcance, `watch` para vigilar la carpeta y `docker compose` para empaquetarlo.
- 140 tests con datos sintéticos; integración continua con ruff, pytest y build de la imagen.
