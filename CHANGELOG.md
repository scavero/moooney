# Cambios

## 1.1.4 · 2026-11-06

- **Informe, Pipeline y Ajustes** con el mismo criterio de legibilidad: subtítulos y notas como frases, navegación
  de mes de Informe como en Resumen, aviso de «no disponible» con la marca ámbar en vez de un color suelto, botones
  del stepper y del panel de trabajo a 40 px, mantenimiento de categorización plegado con explicación, tablas de
  Pipeline con la misma densidad que Comercios, etiquetas en los campos de clave, «opcional» en peso 400 y el texto
  largo de Ajustes a 16 px.

## 1.1.3 · 2026-11-06

- **Alertas** con el mismo criterio de legibilidad: filtros de nivel y «Pendientes / Con las vistas» como
  controles segmentados (los chips salían todos en verde desde el cambio de Movimientos), cada grupo como tabla
  con cabecera (nivel, fecha, qué pasa, acción), el comercio o la categoría en peso 500 al inicio del mensaje y
  aviso propio cuando un filtro no devuelve nada.

## 1.1.2 · 2026-11-06

- **Recurrentes y Presupuestos** con el mismo criterio de legibilidad: en Recurrentes, control «Activos / Con
  inactivos», tabla de cinco columnas con categoría y estabilidad bajo la clave, subida de precio marcada en ámbar
  y el calendario como tabla con cabecera y saldo restante. En Presupuestos, mes y día en el subtítulo, navegación
  de mes como en Resumen, seguimiento en cinco columnas con «gastado de límite» bajo la barra, campos con
  etiqueta y la página a una columna por debajo de 1400 px para que la tabla no se corte.

## 1.1.1 · 2026-11-06

- **Comercios y Tendencias** con el mismo criterio de legibilidad: en Comercios, barra de progreso de la cola,
  la dudosa marcada en ámbar junto a la categoría actual y la tabla de claves en cinco columnas (origen, última
  fecha y resumen web bajo la clave). En Tendencias, cabeceras en las tablas, el porcentaje junto a la categoría
  y notas al pie como texto secundario legible.

## 1.1.0 · 2026-11-06

- **Legibilidad**: pesos 400/500/700 en vez de 300/200 (las fuentes vendorizadas ya eran variables), grises y
  coral de texto que cumplen contraste 4,5:1, texto base de 16 px, controles de 40 px. Resumen con el mes como
  título, pendientes como botones y diagnóstico plegado a cinco puntos. Movimientos en cuatro columnas,
  agrupados por día con total, y un desplegable «···» con flujo, origen, etiquetas y cuenta.
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
- **Ajustes** en la web: la clave del proxy (y Google Places) se pega en la página y se guarda en `data/.env`,
  con prueba de conexión (`GET /models`) e interruptor de modo offline. La CLI y la web leen `data/.env` y
  `./.env` sin depender de variables del sistema; el entorno del proceso, si existe, sigue mandando.
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
