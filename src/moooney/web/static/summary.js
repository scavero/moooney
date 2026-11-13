// Explorador del Resumen: anillo de categorías, ritmo del mes (o gasto mes a mes en un periodo)
// y comercios. Elegir una categoría (en el anillo o en la tabla) la deja sola en toda la página.
// Los datos llegan en <script id="explorer-data">; aquí no se pide nada al servidor.
(function () {
  'use strict';
  var MESES = ['ene', 'feb', 'mar', 'abr', 'may', 'jun', 'jul', 'ago', 'sep', 'oct', 'nov', 'dic'];
  var selected = {};  // categoría elegida por página (clave del periodo): sobrevive al cambio de tema
  var eur = function (v) {
    return new Intl.NumberFormat('es-ES', { style: 'currency', currency: 'EUR', maximumFractionDigits: 0, useGrouping: 'always' }).format(v);
  };
  var $ = function (id) { return document.getElementById(id); };
  var sum = function (a) { return a.reduce(function (x, y) { return x + y; }, 0); };

  function theme() {
    var css = getComputedStyle(document.documentElement);
    var v = function (n) { return css.getPropertyValue(n).trim(); };
    Chart.defaults.font = Object.assign({}, Chart.defaults.font, { family: 'Work Sans', weight: 400, size: 13 });
    Chart.defaults.color = v('--chart-text');
    return { palette: [1, 2, 3, 4, 5, 6, 7, 8, 9].map(function (i) { return v('--chart-' + i); }),
             grid: { color: v('--chart-grid') }, paper: v('--surface') };
  }
  function mk(id, cfg) {
    var el = $(id); if (!el) return null;
    if (el._chart) el._chart.destroy();
    el._chart = new Chart(el, cfg); return el._chart;
  }
  // Suma por posición de las series de las categorías elegidas (todas si no hay ninguna).
  function total(series, cat, n) {
    var out = new Array(n).fill(0);
    Object.keys(series || {}).forEach(function (c) {
      if (cat && c !== cat) return;
      series[c].forEach(function (x, i) { if (i < n) out[i] += x; });
    });
    return out;
  }
  function cumulative(a) { var s = 0; return a.map(function (x) { s += x; return Math.round(s * 100) / 100; }); }

  window.renderSummary = function () {
    var node = $('explorer-data');
    if (!node || !window.Chart) return;
    var d = JSON.parse(node.textContent);
    var t = theme();
    var slices = d.cats.slice(0, 8);
    var rest = d.cats.slice(8);
    var colorOf = function (c) { var i = d.cats.indexOf(c); return t.palette[i < 0 || i > 8 ? 8 : Math.min(i, 8)]; };
    var all = sum(d.cats.map(function (c) { return d.amounts[c]; }));

    function draw() {
      var cat = selected[d.id] && d.cats.indexOf(selected[d.id]) >= 0 ? selected[d.id] : null;
      var labels = slices.concat(rest.length ? ['Resto'] : []);
      var values = slices.map(function (c) { return d.amounts[c]; }).concat(rest.length ? [sum(rest.map(function (c) { return d.amounts[c]; }))] : []);
      var active = cat ? (slices.indexOf(cat) >= 0 ? slices.indexOf(cat) : 8) : -1;

      mk('chart-donut', { type: 'doughnut',
        data: { labels: labels, datasets: [{ data: values, borderColor: t.paper, borderWidth: 2, hoverOffset: 6,
          backgroundColor: labels.map(function (_, i) { return t.palette[i] + (active >= 0 && i !== active ? '59' : ''); }),
          offset: labels.map(function (_, i) { return i === active ? 10 : 0; }) }] },
        options: { responsive: true, maintainAspectRatio: false, cutout: '68%', layout: { padding: 10 },
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (c) { return c.label + ': ' + eur(c.raw) + ' · ' + Math.round(c.raw / all * 100) + ' %'; } } } },
          onHover: function (e, els) { e.native.target.style.cursor = els.length && labels[els[0].index] !== 'Resto' ? 'pointer' : 'default'; },
          onClick: function (e, els) {
            if (!els.length) return pick(null);
            var name = labels[els[0].index];
            if (name !== 'Resto') pick(name === cat ? null : name);
          } } });
      $('donut-label').textContent = cat || 'Gasto';
      $('donut-value').textContent = eur(cat ? d.amounts[cat] : all);
      $('donut-hint').textContent = cat ? Math.round(d.amounts[cat] / all * 100) + ' % del gasto' : d.cats.length + ' categorías';

      document.querySelectorAll('[data-cat-row]').forEach(function (tr) {
        var on = tr.dataset.catRow === cat;
        tr.classList.toggle('on', on);
        var b = tr.querySelector('.cat-pick'); if (b) b.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      document.querySelectorAll('[data-cat-group]').forEach(function (g) { g.hidden = g.dataset.catGroup !== (cat || ''); });
      var bar = $('sel-bar');
      if (bar) {
        bar.hidden = !cat;
        if (cat) { $('sel-name').textContent = cat; $('sel-link').dataset.url = '/transactions?' + d.tx + '&category=' + encodeURIComponent(cat); }
      }
      var mt = $('merch-title'); if (mt) mt.textContent = cat ? 'Dónde se va en ' + cat : mt.dataset.all;
      if (d.kind === 'month') pace(cat); else monthly(cat);
      if (window.highlightCategory) window.highlightCategory(cat);
    }

    // Mes: gasto acumulado día a día frente al mes anterior.
    function pace(cat) {
      var days = Array.from({ length: Math.max(d.days, d.prev ? d.prev_days : 0) }, function (_, i) { return i + 1; });
      var cur = cumulative(total(d.daily, cat, d.cutoff));
      var before = d.prev ? cumulative(total(d.prev, cat, d.prev_days)) : null;
      var now = cur.length ? cur[cur.length - 1] : 0;
      var text = (d.partial ? 'A día ' + d.cutoff + ' llevas ' : 'Gastaste ') + eur(now) + (cat ? ' en ' + cat : '') + '.';
      if (before && before.length) {
        var then = before[Math.min(d.cutoff, before.length) - 1];
        var diff = now - then;
        text += Math.abs(diff) < 1 ? ' Igual que en ' + d.prev_label + (d.partial ? ' a estas alturas.' : '.')
          : ' ' + eur(Math.abs(diff)) + (diff > 0 ? ' más' : ' menos') + ' que en ' + d.prev_label + (d.partial ? ' a estas alturas' : '') + ' (' + eur(then) + ').';
      }
      $('pace-text').textContent = text;
      var sets = [{ label: d.label, data: cur, borderColor: cat ? colorOf(cat) : t.palette[0], backgroundColor: (cat ? colorOf(cat) : t.palette[1]) + '26',
                    fill: true, stepped: true, borderWidth: 2, pointRadius: 0, pointHitRadius: 12 }];
      if (before) sets.push({ label: d.prev_label, data: before, borderColor: t.palette[5], borderDash: [4, 4], fill: false, stepped: true, borderWidth: 1.5, pointRadius: 0, pointHitRadius: 12 });
      mk('chart-pace', { type: 'line', data: { labels: days, datasets: sets },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 10 } },
            tooltip: { callbacks: { title: function (c) { return 'Día ' + c[0].label; }, label: function (c) { return c.dataset.label + ': ' + eur(c.raw); } } } },
          scales: { x: { grid: { display: false }, ticks: { maxTicksLimit: 8 } }, y: { beginAtZero: true, ticks: { callback: function (v) { return eur(v); } }, grid: t.grid } } } });
    }

    // Trimestre, año o rango: gasto de cada mes (y lo que entró, si no hay categoría elegida).
    function monthly(cat) {
      var labels = d.months.map(function (m) { return MESES[+m.slice(5, 7) - 1] + ' ' + m.slice(2, 4); });
      var sets = [{ label: cat || 'Gastos', data: total(d.by_month, cat, d.months.length), backgroundColor: cat ? colorOf(cat) : t.palette[5], borderRadius: 3 }];
      if (!cat) sets.unshift({ label: 'Ingresos', data: d.income, backgroundColor: t.palette[2], borderRadius: 3 });
      mk('chart-months', { type: 'bar', data: { labels: labels, datasets: sets },
        options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
          plugins: { legend: { position: 'bottom', labels: { boxWidth: 10 } }, tooltip: { callbacks: { label: function (c) { return c.dataset.label + ': ' + eur(c.raw); } } } },
          scales: { x: { grid: { display: false } }, y: { beginAtZero: true, ticks: { callback: function (v) { return eur(v); } }, grid: t.grid } },
          onHover: function (e, els) { e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
          onClick: function (e, els) { if (els.length) window.go('/?month=' + d.months[els[0].index]); } } });
    }

    function pick(cat) { selected[d.id] = cat; draw(); }
    window.pickCategory = pick;
    draw();
  };

  document.addEventListener('click', function (e) {
    var b = e.target.closest && e.target.closest('.cat-pick');
    if (b && window.pickCategory) {
      var row = b.closest('[data-cat-row]');
      window.pickCategory(row.classList.contains('on') ? null : row.dataset.catRow);
      return;
    }
    if (e.target.closest && e.target.closest('#sel-clear') && window.pickCategory) window.pickCategory(null);
    var link = e.target.closest && e.target.closest('#sel-link');
    if (link && link.dataset.url) window.go(link.dataset.url);
  });
})();
