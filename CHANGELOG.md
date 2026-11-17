# Cambios

## 1.23.1 · 2026-11-17

- **Repaso en móvil**: en Tendencias, «Qué está cambiando» se apila (categoría y previsión arriba, la
  frase debajo a todo el ancho) en vez de estrujar la frase en una columna; el selector de Comercios
  (Dudosos / En Otros / Corregidas / Todas) ya cabe entero; el Historial de Importar no parte las
  palabras letra a letra; los desplegables «↓» tienen 40 px de alto para el dedo.
- **Arreglo**: en «Evolución por categoría» todas las categorías parecían elegidas (también en
  escritorio); ahora solo se rellena la que estás viendo.

## 1.23.0 · 2026-11-17

- **Tendencias, segunda vuelta**: cada categoría dice ahora, además de si sube o baja, si **cambió de
  escalón** («cambió de escalón en julio de 2026: de 103 € a 156 € al mes»: los últimos 3-6 meses quedan
  todos por encima o por debajo de los 6 anteriores) o si tuvo un **mes raro** (uno de los 6 últimos muy
  por encima de lo normal; un pico de temporada conocido no cuenta).
- **Gasto de fondo frente a puntual**: qué parte del gasto, en total y por categoría, va a comercios a
  los que vuelves al menos la mitad de los meses. El resto son compras puntuales. La lectura del modelo
  recibe también estos tres datos.

## 1.22.0 · 2026-11-17

- **Fuera las funciones que ya estaban ocultas**: se borra el código de las etiquetas, los gastos
  compartidos y «la categoría solo de este movimiento» (rutas `/shared` y `/transactions/…/tags|category`,
  `moooney tag`, `analytics/shared.py`, los filtros `tag` y `shared`). El CSV exportado pierde la columna
  `etiquetas`. Las tablas ya no se crean; en una base antigua se quedan como estén y nadie las lee: si
  tenías algo etiquetado, emparejado o con categoría propia, deja de aplicarse.

## 1.21.1 · 2026-11-17

- **Una sola previsión también en el asistente y el informe**: para los meses siguientes ya no reciben
  la cuenta antigua (tres meses iguales), sino el gasto total previsto con su rango, la misma cifra que
  enseña Tendencias. Con menos de 6 meses completos no se da previsión de meses futuros.

## 1.21.0 · 2026-11-17

- **Categorías con tilde**: «Alimentación», «Tarjeta de crédito», «nómina»… se escriben bien en todas las
  pantallas y gráficos. Es solo cómo se enseñan: en la base, en las reglas y en lo que recibe el modelo
  siguen sin tilde, así que no hay nada que migrar.
- **Menú de siete a cinco**: Resumen, Movimientos, Tendencias, Plan y Pregunta, con «Datos» aparte debajo.
  Avisos pasa a ser una pestaña del Resumen y su contador va en «Resumen».

## 1.20.0 · 2026-11-15

Repaso de claridad («menos es más»): cada cifra se calcula de una sola manera y cada cosa se dice en un sitio.

- **Resumen más corto**: con el mes en curso la cifra grande es la **previsión a fin de mes**, no lo
  ahorrado hasta hoy (el día 10, con la nómina cobrada, siempre parecía un buen mes). Debajo, una sola
  lista **«Para mirar»** funde las alertas sin ver, los comercios por revisar y los hallazgos serios, sin
  repetir lo que ya dice una alerta. Salen de la página el «ahorro potencial», los dos gráficos de
  12 meses, el presupuesto y la tarjeta de alertas (siguen en Tendencias, Plan y Avisos).
- **Sin extrapolar el mes a medias**: el diagnóstico ya no multiplica el gasto de unos días por lo que
  queda de mes; solo avisa de la categoría que ya supera un mes normal entero.
- **Una sola previsión de fin de mes**: la proyección de cada presupuesto usa la misma cuenta que la
  previsión general (lo gastado + recurrentes que aún vencen + gasto variable habitual por los días
  que quedan), así Plan y Resumen dejan de dar cifras distintas.
- **Tendencias**: fuera la tabla «Próximos meses» (tres filas iguales y otra previsión distinta) y la
  columna «Tend.» de la rejilla, que contradecía a «Qué está cambiando». En su sitio, el gráfico de lo
  que entra y lo que sale, que estaba en el Resumen.
- **Importar en una pantalla**: queda la zona para arrastrar el Excel, los ficheros y el historial. Los
  cinco pasos numerados desaparecen; la página solo avisa de lo que pide acción (Excel sin importar,
  movimientos sin categoría, comercios dudosos) y los doce botones restantes viven tras «Avanzado».
  El registro de lo que ha salido a modelos e Internet pasa a Ajustes.
- **Movimientos sin lo que nadie usaba**: salen de la pantalla las etiquetas, los gastos compartidos y
  «cambiar la categoría solo de este movimiento» (filtros, aviso fijo, medio menú «···» y la nube de
  etiquetas). No se borra nada: lo que ya estuviera etiquetado, emparejado o con categoría propia se
  sigue viendo, `moooney tag` sigue en la CLI y los gastos compartidos en `/shared`.
- **Plan**: las metas y el «¿qué pasaría si?» van plegados hasta que se usan.
- **Avisos en una lista**: todas las alertas en una sola tabla, lo más serio primero, con el tipo en
  cada fila (antes, una tabla con su cabecera por cada tipo). Qué significa cada aviso va plegado debajo
  y ya no cita la fórmula. Fuera el filtro por nivel y «Marcar el grupo como visto».
- **Movimientos**: a la vista quedan el buscador, el periodo y la categoría; fechas, flujo y cuenta van
  tras «Más filtros», que se abre solo si alguno está en uso.
- **Lenguaje**: los avisos nuevos escriben los importes y las fechas como el resto de la app
  («39,00 €», «8 oct»; los ya guardados conservan su texto). Comercios habla de «comercios», no de
  «claves»; el nivel se llama igual en Resumen y Avisos (importante, aviso, dato); el informe sin
  modelo ya no enseña `[critical]`; el gráfico de categoría no nombra el método de previsión.

## 1.19.0 · 2026-11-15

- **Tendencias con más fondo**: nueva tarjeta «Qué está cambiando» con la tendencia de cada categoría
  (12 meses frente a los 12 anteriores cuando hay dos años; si no, 3 frente a 3 y marcada como
  provisional), los picos de temporada que se repiten y la previsión de los tres meses siguientes
  con su rango. Tres métodos sencillos compiten prediciendo los meses ya conocidos; gana el que
  menos falla y la página enseña su error.
- **Previsión en el gráfico de categoría**: línea discontinua con el rango probable.
- **Lectura del modelo**: el modelo de redacción interpreta esas tendencias a petición. Recibe solo
  la tabla de agregados por categoría, nunca movimientos; queda en la auditoría como `trends`.

## 1.18.0 · 2026-11-15

- **Desplegables que se leen enteros**: el de «Categoría → subcategoría» de Movimientos y Comercios
  tiene un ancho mínimo y ya no sale cortado a ningún ancho de ventana. Por debajo de 1.200 px ceden
  sitio el saldo (Movimientos) y el número de movimientos (Comercios).
- **Móvil**: en Movimientos la categoría ocupa todo el ancho de la fila y los filtros se pliegan tras
  un botón «Filtros»; Comercios deja de desplazarse en horizontal (clave y total arriba, categoría y
  flujo debajo); el formulario de reglas pone un desplegable por línea.
- **«Mostrar más»**: las tablas y listas que pueden crecer (avisos, recurrentes, calendario de cargos,
  presupuestos, fuentes de ingresos, categorías y comercios del periodo, mapa de calor de Tendencias,
  ficheros e historial de Importar, reglas, apartados, etiquetas y grupos de posibles duplicados)
  enseñan las primeras filas y pliegan el resto tras un botón.

## 1.17.1 · 2026-11-15

- **Movimientos en pantallas medianas**: la columna Categoría se quedaba en 87 px (hacia 1.000 px de
  ancho) y el desplegable salía cortado; ahora el concepto cede ancho y la categoría se lee.

## 1.17.0 · 2026-11-15

- **Subcategoría en el mismo desplegable**: en Comercios y en Movimientos la categoría y la subcategoría
  se eligen juntas («Vivienda → alquiler»), en un solo desplegable agrupado por categoría. Antes la
  subcategoría era un segundo desplegable gris en Comercios y no se veía ni se podía cambiar en
  Movimientos. «Solo este» también admite subcategoría.

## 1.16.1 · 2026-11-15

- **Posibles duplicados**: preguntar al modelo «¿es un duplicado?» daba «sí» en casi todos los grupos.
  Ahora se le pregunta qué clase de pago es (periódico, cotidiano, entre personas u ocasional) y el
  veredicto lo pone el código: un pago periódico repetido es probable duplicado; lo cotidiano y el
  dinero entre personas, normal; una compra ocasional, duplicado solo si es del mismo día.

## 1.16.0 · 2026-11-15

- **Posibles duplicados**: página nueva (enlazada desde Movimientos) con los grupos de movimientos
  iguales y cercanos: misma cuenta, comercio e importe con 3 días o menos entre ellos. El botón
  **Revisar con IA** pide al modelo de decisión un veredicto por grupo (probable duplicado, dudoso o
  normal); al modelo solo van el comercio, el tipo de operación, el importe y los días de separación.
  Desde ahí marcas cada movimiento como duplicado o descartas el grupo. Nada se aparta solo.
- **De dónde viene**: el resumen (del mes y de trimestre, año o rango) enseña también los ingresos
  por categoría y subcategoría, con su parte del total y un enlace a sus movimientos.

## 1.15.1 · 2026-11-15

- **Transferencias**: al reconstruir claves, cada clave nueva por destinatario hereda la categoría
  que tenía la clave mezclada, también si la habías corregido tú. Así reconstruir no cambia ningún
  total; después afinas cada destinatario. (En la 1.15.0 se perdía esa corrección.)

## 1.15.0 · 2026-11-15

- **Transferencias**: cada destinatario (y cada ordenante) tiene ahora su propia clave de comercio,
  con un código de cuatro letras en vez del nombre. Antes todas las transferencias enviadas
  compartían una sola clave, y al categorizar el alquiler se llevaba detrás los traspasos a otra
  cuenta. Hay que pulsar **Reconstruir claves** en Importar (o `categorize --rebuild-keys`) para
  aplicarlo a lo ya cargado: las transferencias se vuelven a categorizar por reglas.
- **Movimientos**: en el «···» de cada movimiento, **Solo este** cambia su categoría sin tocar los
  demás del mismo comercio.

## 1.14.0 · 2026-11-15

- **Movimientos**: cada movimiento se puede **borrar** o **marcar como duplicado** desde su botón
  «···». Deja de contar en todo (resumen, presupuestos, avisos, exportación) y no vuelve aunque
  reimportes el extracto. Los apartados se listan al pie de Movimientos y se pueden restaurar.
- **Asistente**: el autocompletado completa lo que escribes en vez de reformularlo (prompt nuevo
  con reglas y ejemplos para el modelo ligero).

## 1.13.4 · 2026-11-15

- **Asistente**: el autocompletado aparece más a menudo. El modelo ligero suele corregir acentos y
  añadir el «¿» al completar, y esas propuestas se descartaban por no empezar igual que lo tecleado;
  ahora la comparación no mira acentos ni signos de apertura.

## 1.13.3 · 2026-11-15

- **Asistente**: al pulsar una propuesta de «¿Quieres decir…?» el campo se vacía (antes se quedaba
  el texto a medias), y las sugerencias para seguir ya no pisan el autocompletado si empiezas a
  escribir nada más llegar la respuesta.

## 1.13.2 · 2026-11-13

- **Asistente**: el autocompletado ya no aparece mientras se espera una respuesta (pasaba al
  pulsar Enter justo después de teclear), y en una conversación vacía las ideas para empezar se
  apartan cuando hay propuestas de autocompletado.

## 1.13.1 · 2026-11-13

- **Asistente**: al borrar lo escrito vuelven las sugerencias para seguir (antes se quedaban los
  botones de «¿Quieres decir…?»), y al recargar la página salen las sugerencias del modelo
  ligero, no las fijas; se guardan en memoria para la última respuesta y no se vuelven a pedir.

## 1.13.0 · 2026-11-13

- **Autocompletado al escribir, con el modelo ligero**: tras una pausa de medio segundo (y al
  menos 4 caracteres), `gemma-4-e2b` propone formas de terminar la frase teniendo en cuenta la
  conversación. Salen como botones bajo el chat («¿Quieres decir…?») y al pulsar uno se envía.
  Queda en `llm_calls` como `chat_complete`.
- Desaparece el desplegable del campo de texto con la lista fija de preguntas.

## 1.12.0 · 2026-11-13

- **El asistente infiere cómo seguir**: tras cada respuesta, el modelo ligero (`gemma-4-e2b`,
  clave `sugerencias` de `llm.yaml`) lee la conversación y propone los siguientes mensajes:
  contesta a lo que el asistente acaba de ofrecer o profundiza en lo último. Las sugerencias
  deterministas salen al momento y se sustituyen cuando llegan las del modelo; si no responde,
  se quedan. Queda registrado en `llm_calls` como `chat_followups`.

## 1.11.2 · 2026-11-13

- Botón **Rellenar subcategorías** en Importar, junto a «Segunda opinión»: lo mismo que
  `moooney categorize --fill-subcategories`, desde la web.

## 1.11.1 · 2026-11-13

- `moooney categorize --fill-subcategories` pregunta al modelo la subcategoría de los comercios
  ya categorizados que no la tienen. Solo cambia la subcategoría: categoría, flujo, origen y
  confianza se quedan como estaban.

## 1.11.0 · 2026-11-13

- **Subcategorías de verdad**: hasta ahora solo las ponían las reglas. El modelo elige ahora la
  subcategoría en una segunda pregunta corta, ya dentro de la categoría decidida (supermercado o
  restaurantes, combustible o peajes…); si esa pregunta falla, la categoría se guarda igual.
  Se corrige a mano en Comercios, bajo la categoría, y corregir solo la categoría ya no borra
  la subcategoría. Al elegir una categoría en el Resumen se ve su reparto por subcategoría.
  Nuevas: «mascotas» en Salud y «regalos» en Ocio. Lo ya categorizado no cambia hasta que se
  vuelva a preguntar al modelo.

## 1.10.2 · 2026-11-13

- **Asistente**: las sugerencias para seguir ya no vuelven a las genéricas cuando la respuesta no
  consulta nada nuevo (siguen el hilo de la última consulta), y las listas que el modelo escribe
  pegadas a un párrafo se muestran como listas.

## 1.10.1 · 2026-11-13

- **Informe sin modelo, en castellano**: el mes por su nombre («octubre de 2026»), importes con
  separador de miles y, si el mes va a medias, dice hasta qué día cuenta. Los informes ya
  guardados no cambian hasta pulsar *Regenerar sin modelo*.
- **Móvil apaisado**: con poca altura, la cabecera y la barra inferior se encogen (iconos y
  nombres en una línea) y el contenido empieza antes.

## 1.10.0 · 2026-11-13

- **Avisos, en una sola página**: Alertas y Recurrentes dejan de ser dos pestañas. Arriba, las
  alertas pendientes; debajo, los recurrentes, lo que ha subido y el calendario de cargos. La
  dirección `/recurring` lleva a esa parte de la página.
- **El asistente propone cómo seguir**: tras cada respuesta aparecen hasta tres preguntas para
  continuar, elegidas según lo que acaba de consultar (si miró recurrentes, propone preguntar
  cuáles han subido) y según la categoría que más se sale de su media; no repite lo ya
  preguntado. Además, el campo de texto autocompleta: al escribir ofrece las preguntas que
  encajan, también por categoría. Todo sin llamar al modelo.
- **Revisión pantalla a pantalla** (12 pantallas en 5 tamaños, de móvil a escritorio ancho):
  el calendario de cargos enseña siempre el saldo que queda (el importe va bajo el nombre); las
  tablas ocupan todo el ancho en tablet; «Lo que te ha subido» ya no parte el nombre palabra a
  palabra en el móvil; la lista de categorías del Resumen es más limpia; decimales con coma
  («0,8 meses»), «1 vez» en singular, el neto de Movimientos con separador de miles y los títulos
  de los grupos de alertas con mayúscula inicial.

## 1.9.0 · 2026-11-13

- **Resumen con más gráficas y que responde**: un anillo reparte el gasto por categoría y, al
  pulsar una (en el anillo o en la lista), toda la página se queda con ella: el *ritmo del mes*
  (gasto acumulado día a día frente al mes anterior, con la frase «a día 10 llevas X, Y más que
  en septiembre a estas alturas»), los comercios donde se va y los 12 meses. *Lo que entra y lo
  que sale* pasa a barras con la línea del ahorro, y pulsar un mes abre su resumen. En trimestre,
  año y rango hay el mismo anillo y una gráfica de ingresos y gastos mes a mes.

## 1.8.3 · 2026-11-12

- **Plan, en una sola página**: Presupuestos y Metas dejan de ser dos pestañas. Arriba, el
  seguimiento del presupuesto del mes; debajo, las metas y el simulador «qué pasaría si». La
  dirección `/goals` lleva a esa parte de la página.

## 1.8.2 · 2026-11-12

- **Compartidos, dentro de Movimientos**: desaparece como página y pestaña. Cada movimiento enseña
  en su fila si es la devolución de un gasto o cuánto le han devuelto, y en «···» se empareja, se
  descarta o se deshace. Un aviso en Movimientos dice cuántos ingresos podrían ser la parte de
  alguien y el filtro *Compartidos* enseña los ya emparejados o los que faltan por revisar (con
  sus propuestas ya abiertas). La dirección `/shared` lleva a ese filtro.

## 1.8.1 · 2026-11-12

- La página *Pipeline* pasa a llamarse *Importar*, como su pestaña en *Datos*. La dirección `/pipeline` no cambia.

## 1.8.0 · 2026-11-12

- **Periodos**: el Resumen ya no es solo mensual. Un selector *Mes · Trimestre · Año · Rango* enseña
  los totales del periodo (ingresos, gastos, ahorro, inversión), cada categoría, el gasto mes a mes y
  los comercios donde más se gasta, frente al periodo anterior equivalente: el trimestre con el
  trimestre anterior, el año con el año anterior y un rango con los mismos días de antes. Si el
  periodo está en curso, el anterior se corta a la misma altura para que la comparación sea justa.
  En Movimientos el filtro de mes pasa a ser de periodo (años, trimestres, meses) y hay *Desde* y
  *Hasta* para un rango libre; Exportar CSV respeta el periodo. El asistente tiene una consulta
  nueva para trimestres, años y rangos.
- **Menos menú**: de trece entradas a siete. *Resumen* (con *Año* dentro, como un periodo más),
  *Tendencias*, *Movimientos* (con *Compartidos*), *Pregunta* (con *Informe mensual*), *Plan*
  (*Presupuestos* y *Metas*), *Avisos* (*Alertas* y *Recurrentes*) y *Datos* (*Importar*, antes
  Pipeline; *Comercios* y *Ajustes*). Las páginas que comparten sección son pestañas dentro de
  ella; no se ha quitado ninguna función y las direcciones de siempre siguen valiendo (`/year`
  lleva al año en el Resumen). En el móvil, «Más» enseña solo lo que no está ya en la barra.
- El calendario de cargos de Recurrentes ya no ensancha la página en pantallas medianas: se
  desplaza dentro de su tarjeta.
- Las flechas de mes (Resumen, Presupuestos, Informe) se paran en el primer y el último mes con
  datos; ya no se puede ir a meses que no han ocurrido, y una dirección con un mes fuera de rango
  vuelve al más cercano que sí tiene datos.

## 1.7.1 · 2026-11-12

- **Móvil**: la navegación pasa a una barra inferior con cuatro accesos (Resumen, Tendencias,
  Movimientos, Pregunta) y «Más», que abre el menú completo por grupos; antes era una tira que se
  desplazaba de lado sin indicarlo. El contenido usa todo el ancho (los márgenes eran de 48 px por
  una regla que anulaba la del móvil), las cifras de cabecera van de dos en dos y los filtros en
  dos columnas iguales. Movimientos, Alertas y Presupuestos dejan de ser tablas que se desplazan
  de lado: cada fila se apila (importe, botón y límite siempre a la vista). Campos a 16 px para
  que iOS no amplíe la página al escribir y controles de 40 px también en los botones pequeños.

## 1.7.0 · 2026-11-11

- **Pregunta**: nueva página con un asistente al que preguntas por tus finanzas («¿cómo voy este
  mes?», «¿qué me ha subido?», «¿dónde recorto 100 €?») y te da consejos. Lo responde el modelo de
  texto (qwen3.8), pero las cifras no las calcula él: elige entre once consultas deterministas
  (resumen del mes, categorías, comercios, un comercio mes a mes, recurrentes y subidas,
  presupuestos, alertas, diagnóstico, previsión, año y metas), moooney las calcula y el modelo
  redacta con lo que le devuelven. Solo ve totales por mes, categoría y comercio, nunca movimientos
  sueltos; cada respuesta dice qué ha consultado y cada llamada queda en la auditoría. Recuerda la
  conversación hasta que pulsas *Nueva conversación*; *Empezar de cero* la borra. También desde la
  terminal: `moooney ask "¿cómo voy este mes?"`.
- El diagnóstico con el mes a medias deja claro que la media es «hasta ese día» y que el ahorro
  posible es una extrapolación («si el resto del mes sigue igual…»).

## 1.6.0 · 2026-11-11

- **Metas y «qué pasaría si»**: nueva página *Metas*. Apuntas cuánto quieres juntar, cuánto llevas y
  para cuándo, y te dice cuándo llegas a tu ritmo de ahorro y, si no llegas a tiempo, cuánto haría
  falta al mes. El simulador recorta la parte variable de tus categorías o da de baja recurrentes y
  recalcula al momento el ahorro al mes y al año y la fecha de cada meta. Las metas se guardan en la
  base: entran en las copias y *Empezar de cero* las conserva.
- **Resumen del año**: nueva página *Año*. Ingresos, gastos, ahorro e inversión del año, cada
  categoría frente al año anterior y el gasto mes a mes con la categoría que más pesó; dice cuál fue
  el mes más caro y el más barato. Si el año está en curso, el anterior se corta en el mismo día para
  que la comparación sea justa.
- **Gastos compartidos**: nueva página *Compartidos*. Si pagas una cena de 80 € y te devuelven 20 €
  por Bizum, emparejas la devolución con el gasto y el mes cuenta 60 € de restaurantes, no 80 € de
  gasto más 20 € de ingreso. Propone los emparejamientos que cuadran como reparto a partes iguales
  («entre 4») o del mismo puñado de días; cada propuesta se acepta, se descarta («no es una
  devolución») o se deshace después.
- **Reglas desde la web**: *Comercios → Tus reglas* crea una regla sin tocar YAML: «si la clave
  empieza por / contiene» un texto, a una categoría y subcategoría. Mientras escribes enseña a qué
  claves afectaría (y cuáles no cambian por estar corregidas a mano); al guardar se aplica en el acto
  y vale para los extractos futuros. Se guardan en `config/rules.web.yaml`, fuera de git.
- **Recibos que suben**: *Recurrentes* abre con «Lo que te ha subido»: cada recibo más caro que antes,
  el precio de antes y el de ahora, y cuánto supone al año. Distingue la cuota fija que cambia de
  precio (de 12,99 € a 15,99 €, y se sigue viendo aunque pasen meses) del recibo variable que va al
  alza (la luz, con sus tres últimos recibos un 15 % por encima de los anteriores). Las alertas
  *subida de precio* y la nueva *recibo al alza* salen una vez por subida, no en cada cobro, y se
  retiran solas si el precio vuelve.
- **Copias de seguridad de la base**: la vigilancia guarda una copia al día y *Empezar de cero* otra
  antes de borrar, en `data/backups/` (nuevo ajuste `paths.backups_dir`); se conservan las 14 más
  recientes. Se hacen con la API de copia de SQLite, así salen coherentes aunque la web esté
  escribiendo. `moooney backup` copia ahora, `--list` las enseña y `--restore <fichero>` vuelve a
  una (antes guarda la base actual). En *Pipeline → Mantenimiento* se ve la última y hay un botón
  *Hacer copia ahora*.

## 1.5.5 · 2026-11-09

- **Cheques y pagarés**: una regla nueva clasifica los ingresos de cheques y pagarés (ENTREGA DE
  DOCUMENTOS PARA SU COMPENSACION, INGRESO DE CHEQUE, REMESA DE PAGARES…) como *Ingresos → otros
  ingresos* sin preguntar al modelo. Si era dinero tuyo de otra cuenta, corrígelo a traspaso en
  *Comercios*. Para aplicarla a lo ya categorizado: `moooney categorize --refresh-rules`.

## 1.5.4 · 2026-11-09

- La demo de GitHub Pages enseña *Empezar de cero* en *Pipeline → Mantenimiento*, con el botón
  desactivado y una nota.
- Los botones desactivados ya no se aclaran con opacidad: pasan al gris de texto más claro permitido,
  así que siguen en 4,5:1 de contraste en los dos temas.

## 1.5.3 · 2026-11-08

- **Empezar de cero**: *Pipeline → Mantenimiento → Borrar movimientos y extractos* (o `moooney reset
  --yes`) borra todos los movimientos, las importaciones, los Excel de la carpeta de entrada y lo
  calculado con ellos (alertas, recurrentes, informes, etiquetas). Se conservan la memoria de
  comercios con tus correcciones, los presupuestos, las claves y la configuración, así que lo que
  vuelvas a subir sale categorizado al instante. Para confirmarlo hay que escribir BORRAR.
- Nuevo ajuste `paths.reports_dir` (por defecto `reports`): los informes en Markdown ya no se escriben
  en una ruta relativa fija, y la demo guarda los suyos en su propia carpeta.

## 1.5.2 · 2026-11-08

- **Excel antiguos (.xls)**: se leen los extractos en formato Excel 97-2003 (algunos bancos y la
  exportación de la tarjeta los dan así), tanto copiados a la carpeta como subidos desde la web. Antes
  se rechazaban con «formato .xls no soportado».
- **Movimientos nuevos de comercios ya conocidos**: heredan la categoría de la memoria de comercios al
  importarse. Antes se quedaban sin categoría si el comercio se había categorizado en una importación
  anterior (la cascada solo pregunta por comercios nuevos); basta con volver a importar o categorizar.

## 1.5.1 · 2026-11-08

- **Extractos de la tarjeta de crédito**: se reconoce el Excel de la tarjeta del Santander («Fecha
  operación», «Concepto», «Importe» como «-4,99€», «Divisa»; sin saldo). Las compras entran como
  «COMPRA EN …» en una cuenta «tarjeta» y cuentan una a una; la liquidación mensual de la cuenta
  deja de contar como gasto en los meses que cubre el extracto de la tarjeta, así que nada se cuenta
  dos veces y los meses sin extracto de tarjeta siguen contando la liquidación.
- **Extractos que se solapan**: un movimiento ya cargado no vuelve a entrar aunque el banco escriba
  distinto el concepto en otro informe (se compara cuenta, fecha, importe y saldo). Y dos movimientos
  idénticos del mismo día ya no se quedan en uno: se cuentan (dos cafés iguales en la tarjeta, o un
  cargo, su devolución y otra vez el cargo en la cuenta).
- **Reparar lo ya cargado**: *Pipeline → Mantenimiento → Revisar extractos solapados* (o `moooney
  dedupe --apply`) quita las copias que entraron dos veces y relee los Excel de la carpeta para
  recuperar los movimientos que faltaban.
- Un Excel que no se puede leer ya no bloquea la importación de los demás, y la subida desde la web
  rechaza en el acto un formato que no reconoce ningún perfil, sin dejarlo en la carpeta.
- **La vigilancia de la carpeta (`moooney watch`) ya no se cae**: un Excel que no se puede leer se
  aparta por su hash y no vuelve a lanzar la pipeline cada vuelta (vuelve a probarse si el fichero
  cambia), y cualquier otro error de una vuelta se informa y se reintenta en la siguiente. Antes, en
  el contenedor, un fichero ilegible dejaba el servicio `watch` reiniciándose en bucle.

## 1.5.0 · 2026-11-08

- **Los trabajos cuentan qué hacen**: la tarjeta de un trabajo enseña sus etapas (subir → importar →
  categorizar → alertas), qué está haciendo ahora, cuánto lleva (p. ej. 12 de 40 comercios, con barra)
  y el tiempo. Al terminar, la página recarga sola sus cifras, pasos, ficheros e historial: ya no hace
  falta pulsar F5. Un indicador en la barra lateral se ve desde cualquier página y, si el trabajo
  acaba mientras miras otra, un aviso lleva al resultado. Los botones que lanzan trabajos se
  desactivan mientras hay uno en marcha, la subida muestra su porcentaje y el panel se trae a la vista
  al lanzar un trabajo desde abajo.

## 1.4.0 · 2026-11-08

- **Subir Excel desde la web**: en *Pipeline* se arrastran o eligen uno o varios Excel y se importan y
  categorizan en el acto, sin tocar la carpeta. Se guardan en la carpeta de entrada con nombre saneado,
  sin pisar nada (un fichero idéntico no se duplica; otro con el mismo nombre recibe (2)), y se rechazan
  los que no son Excel, los de más de 20 MB y los dañados, que antes habrían bloqueado la importación.

## 1.3.0 · 2026-11-07

- **Modo oscuro**: botón en el pie de la barra lateral (icono en móvil) que alterna automático, oscuro y claro;
  automático sigue al sistema y la elección se guarda en el navegador. Todos los colores del CSS pasan a
  variables, el tema oscuro las redefine con contraste ≥ 4,5:1 en todo el texto, y los gráficos leen texto,
  rejilla y paleta del tema y se repintan al cambiarlo.
- El autor del paquete usa el correo privado de GitHub.

## 1.2.0 · 2026-11-07

- **Preparación para repo público**: la URL del proxy sale del repo (`LITELLM_BASE_URL` manda sobre `base_url`
  de `config/llm.yaml`, que queda como marcador; también en `.env.example`, `opencode.json` y el script de prueba
  de Clef). Nuevo `config/rules.local.yaml`, ignorado por git y cargado antes que `rules.yaml`, para las reglas
  que retratan al usuario; las generales se quedan con marcas y cadenas. Las cifras reales de `docs/DESIGN.md`
  pasan a proporciones.
- **README para quien clona**: requisitos, sección *Modelos* (los tres papeles, cómo apuntar a otro servidor
  como Ollama, `decider: qwen` sin Clef-Flash, prioridad de URL y clave), paso de reglas propias, aviso de
  que la web no tiene autenticación, limitaciones y cómo contribuir sin datos reales.
- **Demo pública en GitHub Pages** (https://scavero.github.io/moooney/): nuevo comando `moooney snapshot` que
  guarda la web de la demo como páginas estáticas de solo lectura. Meses, filtros generados, gráficos y CSV
  funcionan; lo que escribe muestra un aviso. Se genera sin nada del entorno (ni claves, ni URL del proxy, ni
  reglas locales) y la publica el flujo `pages.yml` en cada push a main. Las navegaciones por JavaScript de las
  plantillas pasan por un `go()` común.

## 1.1.5 · 2026-11-07

- **Móvil y barra lateral**, a partir de una crítica de diseño del Resumen: por debajo de 1000 px el diagnóstico va a
  una columna con el impacto bajo el texto (antes la cifra reservaba 150 px y el título caía palabra por palabra),
  y la cabecera es una fila compacta con marca y navegación desplazable, sin el pie. En la barra lateral, el pie
  «local» y el contador de alertas cumplen 4,5:1 (antes 2,2:1 y 3,1:1).

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
