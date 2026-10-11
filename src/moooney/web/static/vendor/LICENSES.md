# Librerías de terceros servidas en local

| Fichero | Proyecto | Versión | Licencia | Origen |
|---|---|---|---|---|
| htmx.min.js | htmx (bigskysoftware/htmx) | 2.0.4 | BSD-2-Clause | https://cdnjs.cloudflare.com/ajax/libs/htmx/2.0.4/htmx.min.js |
| chart.umd.min.js | Chart.js (chartjs/Chart.js) | 4.4.7 | MIT | https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js |
| fonts/fraunces-*.woff2 | Fraunces (undercase.type) | variable, pesos 200 y 800, subsets latin y latin-ext | SIL Open Font License 1.1 | Google Fonts (fonts.gstatic.com) |
| fonts/work-sans-*.woff2 | Work Sans (Wei Huang) | pesos 300 y 800, subsets latin y latin-ext | SIL Open Font License 1.1 | Google Fonts (fonts.gstatic.com) |

Se sirven desde `/static/vendor/` para que la web funcione sin acceso a Internet y sin cargar nada de un CDN. `fonts/fonts.css` declara los `@font-face` apuntando a los woff2 locales.
