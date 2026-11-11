// Simulador de Metas: suma lo que ahorrarías recortando categorías o dando de baja recurrentes
// y recalcula cuándo llegarías a cada meta. Solo cuentas en el navegador; no guarda nada.
(() => {
  const src = document.getElementById('sim-data');
  if (!src) return;
  const data = JSON.parse(src.textContent);
  const MESES = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio', 'agosto', 'septiembre',
    'octubre', 'noviembre', 'diciembre'];
  // como el filtro eur del servidor: punto de miles también con cuatro cifras (1.250 €)
  const fmt = (v) => String(Math.round(v)).replace(/\B(?=(\d{3})+(?!\d))/g, '.') + ' €';
  const addMonths = (ym, n) => {
    const [y, m0] = ym.split('-').map(Number);
    const m = m0 - 1 + n;
    return MESES[m % 12] + ' de ' + (y + Math.floor(m / 12));
  };

  function render() {
    let extra = 0;
    document.querySelectorAll('.sim-cut').forEach((s) => {
      extra += parseFloat(s.dataset.amount) * parseInt(s.value, 10) / 100;
    });
    document.querySelectorAll('.sim-drop').forEach((c) => {
      if (c.checked) extra += parseFloat(c.dataset.amount);
    });
    const net = data.net + extra;
    const value = document.getElementById('sim-net');
    value.textContent = fmt(net);
    value.className = 'value ' + (net >= 0 ? 'pos' : 'neg');
    document.getElementById('sim-delta').textContent =
      extra > 0 ? fmt(extra) + ' más al mes, ' + fmt(extra * 12) + ' al año' : 'como ahora';
    const box = document.getElementById('sim-goals');
    box.replaceChildren();
    data.goals.filter((g) => g.missing > 0).forEach((g) => {
      const line = document.createElement('div');
      const name = document.createElement('strong');
      name.textContent = g.name;
      line.append(name);
      if (net <= 0) {
        line.append(': así tampoco llegas.');
      } else {
        const n = Math.ceil(g.missing / net);
        line.append(': llegarías en ' + addMonths(data.month, n) + ' (' + n + (n === 1 ? ' mes' : ' meses') + ')');
      }
      box.append(line);
    });
  }

  document.querySelectorAll('.sim-cut, .sim-drop').forEach((e) => e.addEventListener('change', render));
  render();
})();
