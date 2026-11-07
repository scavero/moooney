// Demo estática (GitHub Pages): no hay servidor. Las páginas son ficheros generados por
// moooney snapshot y window.MOOONEY_PAGES (static/pages.js) traduce cada URL de la app al
// fichero que la guarda. Lo que no está generado o escribe datos se queda en un aviso.
(function () {
  'use strict';
  var PAGES = window.MOOONEY_PAGES || {};
  var FALTA = 'Esta vista no está en la demo estática. Clona el repo para verlo todo con los datos de demo.';
  var SOLO_LECTURA = 'La demo estática es de solo lectura: los cambios no se guardan.';

  // Misma forma canónica que snapshot.py: ruta + parámetros no vacíos, ordenados, sin codificar.
  function clave(path, params) {
    var pares = [];
    params.forEach(function (v, k) { if (v !== '') pares.push([k, v]); });
    pares.sort(function (a, b) { return a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0; });
    return path + (pares.length ? '?' + pares.map(function (p) { return p[0] + '=' + p[1]; }).join('&') : '');
  }
  function aviso(msg) { if (window.showToast) window.showToast(msg, 4000); }
  function ir(path, params) {
    var f = PAGES[clave(path, params)];
    if (f) window.location = f; else aviso(FALTA);
  }

  window.go = function (url) {
    var u = new URL(url, 'http://demo.invalid');
    ir(u.pathname, u.searchParams);
  };

  document.addEventListener('htmx:configRequest', function (e) {
    e.preventDefault();
    if (e.detail.verb !== 'get') { aviso(SOLO_LECTURA); return; }
    var params = new URLSearchParams();
    var datos = e.detail.formData || e.detail.parameters;
    if (datos && datos.forEach) datos.forEach(function (v, k) { if (typeof v === 'string') params.append(k, v); });
    var u = new URL(e.detail.path, 'http://demo.invalid');
    u.searchParams.forEach(function (v, k) { params.append(k, v); });
    ir(u.pathname, params);
  });

  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (f.hasAttribute('hx-get') || f.hasAttribute('hx-post')) return;  // lo gestiona htmx
    e.preventDefault();
    if ((f.getAttribute('method') || 'get').toLowerCase() !== 'get') { aviso(SOLO_LECTURA); return; }
    var params = new URLSearchParams();
    new FormData(f).forEach(function (v, k) { if (typeof v === 'string') params.append(k, v); });
    ir(f.getAttribute('action') || location.pathname, params);
  }, true);

  document.addEventListener('click', function (e) {
    var a = e.target.closest && e.target.closest('a[data-falta]');
    if (a) { e.preventDefault(); aviso(FALTA); }
  });
})();
