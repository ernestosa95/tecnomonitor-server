/* ============================================================
   Mapa de integraciones Mirth -- adaptación de
   prototipo_red_integraciones.html para consumir datos reales de
   GET /api/hospital/{id}/mirth/mapa en vez de datos sintéticos.

   Todo vive en un IIFE con fachada window.MapaIntegraciones para no
   ensuciar el scope global de script.js (los nombres de función del
   prototipo -- render, resaltar, salud, etc. -- son genéricos y
   colisionarían si quedaran como funciones sueltas).
   ============================================================ */
(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  // --- Estado del módulo (se resetea en cargar()/destroy()) ---
  let ORIGENES = [], DESTINOS = [], CANALES = [], TL = [], UMBRALES = {}, META = {};
  let PASOS = 0;
  let modo = 'op', formato = 'mapa', paso = 0, tocando = null, playing = false, timer = null;
  let hospitalActualId = null, vmsCache = null;
  // Vista secundaria: flujo acumulado de los últimos ACUM_MIN minutos (el texto del botón
  // #mi-acum en index_beta.html debe coincidir). CADENCIA = cada cuántos tramos reporta el hospital.
  const ACUM_MIN = 30;
  let acumulado = false, CADENCIA = 1;

  const L = { oX: 34, oW: 158, cX: 404, cW: 330, dX: 966, dW: 180, y0: 64, paso: 46, alto: 34, nAlto: 42 };
  const COLOR = { ok: 'var(--mi-green)', warn: 'var(--mi-amber)', bad: 'var(--mi-red)', idle: 'var(--mi-muted2)', nodata: 'var(--mi-muted2)' };

  function $(id) { return document.getElementById(id); }
  function el(t, a, txt) {
    const e = document.createElementNS(SVG_NS, t);
    for (const k in (a || {})) e.setAttribute(k, a[k]);
    if (txt != null) e.textContent = txt;
    return e;
  }

  // ============================================================
  // CARGA DE DATOS
  // ============================================================
  async function cargar(hid) {
    if (!hid) return;
    hospitalActualId = hid;
    _mostrarCargando();
    try {
      const res = await authFetch(`/api/hospital/${encodeURIComponent(hid)}/mirth/mapa?minutos=180&paso=5&incluir_auto=1`);
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const json = await res.json();
      aplicarDatos(json);
      _cargarVmsCache(hid); // best-effort, no bloquea el render
    } catch (e) {
      console.error('[MapaIntegraciones] error cargando', e);
      _mostrarError();
    }
  }

  function destroy() {
    if (playing) togglePlay(); // frena el timer y resetea el ícono de play
    if (timer) { clearInterval(timer); timer = null; playing = false; }
    cerrarDrawer();
    tocando = null;
  }

  async function _cargarVmsCache(hid) {
    vmsCache = null;
    try {
      const res = await authFetch(`/api/hospital/${encodeURIComponent(hid)}`);
      if (!res.ok) return;
      const data = await res.json();
      vmsCache = data.virtual_layer || [];
      if (tocando) abrirNodo(tocando.tipo, tocando.id); // refresca el drawer si ya estaba abierto
    } catch (e) { /* silencioso: CPU/RAM del nodo es un extra, no bloquea el mapa */ }
  }

  function _vmInfo(vmId) {
    if (!vmId || !vmsCache) return null;
    const v = vmsCache.find(x => x.id === vmId);
    if (!v) return null;
    const cpu = v.telemetry && v.telemetry.cpu ? v.telemetry.cpu.usage_percent : null;
    const ram = v.telemetry && v.telemetry.ram ? v.telemetry.ram.usage_percent : null;
    if (cpu == null && ram == null) return null;
    const fc = cpu != null ? Math.round(cpu) + '%' : '—';
    const fr = ram != null ? Math.round(ram) + '%' : '—';
    return `CPU ${fc} · RAM ${fr}`;
  }

  function aplicarDatos(json) {
    UMBRALES = json.umbrales || {};
    META = json.meta || {};
    ORIGENES = json.origenes || [];
    DESTINOS = json.destinos || [];
    CANALES = (json.canales || []).map(c => ({
      id: c.id, nom: c.nom || c.id, hum: c.hum || c.nom || c.id,
      origen: c.origen || null, destino: c.destino || null, padre: c.padre || null,
      crit: c.crit || 'media', tipo_alerta: c.tipo_alerta || `MIRTH_${c.id}`,
      endpoint_origen: c.endpoint_origen, endpoint_destino: c.endpoint_destino,
      clasificado: !!c.clasificado,
    }));
    TL = (json.tl || []).map(snap => ({ ts: new Date(snap.ts), ch: snap.ch || {} }));
    PASOS = TL.length;
    paso = Math.max(0, PASOS - 1);
    CADENCIA = _calcularCadencia();

    if (!CANALES.length) {
      _mostrarSinDatos();
      return;
    }
    _mostrarMapa();

    CANALES.forEach((c, i) => { c.y = L.y0 + i * L.paso; c.cy = c.y + L.alto / 2; });
    _ubicarLateral(ORIGENES, 'origen');
    _ubicarLateral(DESTINOS, 'destino');

    const scrubEl = $('mi-scrub');
    if (scrubEl) { scrubEl.max = String(Math.max(0, PASOS - 1)); scrubEl.value = String(paso); }

    const svg = $('mi-mapa');
    if (svg) svg.setAttribute('viewBox', `0 0 1180 ${L.y0 + CANALES.length * L.paso + 40}`);

    render();
  }

  function _ubicarLateral(nodos, campoRef) {
    nodos.forEach(n => {
      const rel = CANALES.filter(c => c[campoRef] === n.id);
      n.cy = rel.length ? rel.reduce((s, c) => s + c.cy, 0) / rel.length : L.y0 + 300;
      n.rel = rel.map(c => c.id);
    });
    nodos.sort((a, b) => a.cy - b.cy);
    const gap = L.nAlto + 18;
    for (let i = 1; i < nodos.length; i++) if (nodos[i].cy - nodos[i - 1].cy < gap) nodos[i].cy = nodos[i - 1].cy + gap;
    if (!nodos.length) return;
    const exceso = nodos[nodos.length - 1].cy - (L.y0 + Math.max(0, CANALES.length - 1) * L.paso + L.alto / 2);
    if (exceso > 0) nodos.forEach(n => n.cy -= exceso / 2);
  }

  // ============================================================
  // ESTADOS (cargando / vacío / error / mapa)
  // ============================================================
  function _estadoVacio(html) {
    const ev = $('mi-estado-vacio'), ci = $('mi-canvas-inner'), lv = $('mi-listview'), tb = document.querySelector('#tab-mapa .mi-timebar');
    if (ev) { ev.style.display = 'block'; ev.innerHTML = html; }
    if (ci) ci.style.display = 'none';
    if (lv) lv.style.display = 'none';
    if (tb) tb.style.display = 'none';
  }
  function _mostrarCargando() { _estadoVacio('Cargando mapa de integraciones…'); const s = $('mi-summary'); if (s) s.innerHTML = ''; }
  function _mostrarError() { _estadoVacio('No se pudo cargar el mapa de integraciones. Probá de nuevo en unos segundos.'); }
  function _mostrarSinDatos() {
    _estadoVacio('Este hospital no tiene canales de Mirth monitoreados todavía.');
    const s = $('mi-summary'); if (s) s.innerHTML = '';
  }
  function _mostrarMapa() {
    const ev = $('mi-estado-vacio'), ci = $('mi-canvas-inner'), lv = $('mi-listview'), tb = document.querySelector('#tab-mapa .mi-timebar');
    if (ev) ev.style.display = 'none';
    if (tb) tb.style.display = 'flex';
    // No fijamos display inline en canvas-inner/listview acá: eso lo maneja
    // la clase #tab-mapa.mi-aslist (setFormato) + los defaults del CSS. Un
    // inline style puesto acá persistía después de cambiar de formato (el
    // inline gana por especificidad sobre la regla de la clase) y dejaba la
    // vista Lista en blanco aunque la clase ya estuviera bien aplicada.
    if (ci) ci.style.display = '';
    if (lv) lv.style.display = '';
    setFormato(formato); // reaplica el toggle mapa/lista vigente
  }

  // ============================================================
  // ESTADO DERIVADO
  // ============================================================
  function salud(c, d) {
    d = d || {};
    if (!d.fresco) return 'nodata';
    if (d.estado === 'ERROR' || d.estado === 'STOPPED' || d.estado === 'PAUSED') return 'bad';
    const u = UMBRALES[c.crit] || UMBRALES.media || { warn: 60, crit: 200 };
    const cola = d.cola || 0;
    if (cola >= u.crit) return 'bad';
    if (cola >= u.warn) return 'warn';
    if ((d.trafico || 0) === 0) return 'idle';
    return 'ok';
  }

  // ── Datos según la vista (instantáneo / acumulado) ──
  // El server ya entrega deltas por tramo (`trafico`, `rx`, `tx`, `err`), así que el acumulado
  // es la suma de los tramos de la ventana, terminando en la posición de la barra. Estado, cola
  // y `fresco` son los del tramo puntual: no se acumulan.
  function _tramosVentana() {
    const pedidos = Math.max(1, Math.round(ACUM_MIN / (META.paso_min || 5)));
    const desde = Math.max(0, paso - pedidos + 1);
    return { desde, cant: paso - desde + 1, pedidos };
  }

  // Cada cuántos tramos llega una lectura fresca (mediana de los huecos entre lecturas de todos
  // los canales): 1 si el hospital reporta cada `paso` min, 2 si cada el doble, etc. La mediana
  // no se deja arrastrar por una caída puntual.
  function _calcularCadencia() {
    const huecos = [];
    CANALES.forEach(c => {
      let previo = null;
      TL.forEach((s, i) => {
        if (!(s.ch[c.id] || {}).fresco) return;
        if (previo !== null) huecos.push(i - previo);
        previo = i;
      });
    });
    if (!huecos.length) return 1;
    huecos.sort((a, b) => a - b);
    return Math.max(1, huecos[Math.floor(huecos.length / 2)]);
  }

  // Tramos consecutivos sin lectura justo antes del tramo actual (el actual no cuenta: en vivo su
  // lectura puede no haber llegado todavía). Un hueco a mitad de la ventana no pierde tráfico, porque
  // el delta de la lectura siguiente lo recoge; lo que queda sin contar es lo posterior a la última.
  function _tramosSinLectura(cid) {
    let n = 0;
    for (let i = paso - 1; i >= 0 && !((TL[i].ch[cid] || {}).fresco); i--) n++;
    return n;
  }

  function datoDe(cid) {
    const inst = (TL[paso] || { ch: {} }).ch[cid] || {};
    if (!acumulado) return inst;
    const v = _tramosVentana();
    let trafico = 0, rx = 0, tx = 0, err = 0;
    for (let i = v.desde; i <= paso; i++) {
      const t = (TL[i] || { ch: {} }).ch[cid] || {};
      trafico += t.trafico || 0; rx += t.rx || 0; tx += t.tx || 0; err += t.err || 0;
    }
    const sinLectura = _tramosSinLectura(cid);
    return Object.assign({}, inst, { trafico, rx, tx, err, tramos: v.cant, sinLectura, parcial: sinLectura > CADENCIA });
  }

  const _fmtN = n => (n || 0).toLocaleString('es-AR');
  // Cifra de tráfico de un canal; el asterisco marca un acumulado al que le falta lo posterior a la última lectura.
  const _fmtTrafico = d => acumulado ? _fmtN(d.trafico) + (d.parcial ? '*' : '') : String(d.trafico || 0);

  // ============================================================
  // RENDER
  // ============================================================
  function curva(x1, y1, x2, y2) { const dx = Math.max(46, (x2 - x1) * 0.46); return `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`; }
  function grosor(tr) { return Math.max(1.1, Math.min(5.4, 1.1 + tr / 13)); }
  // En acumulado el grosor sigue la tasa media por tramo (total / tramos de la ventana), así el
  // mismo grosor significa el mismo caudal en ambas vistas y no satura con ventanas largas.
  function _tasa(d) { return (d.trafico || 0) / (d.tramos || 1); }

  function render() {
    const svg = $('mi-mapa');
    if (!svg) return;
    svg.innerHTML = '';
    const gE = el('g'), gN = el('g');

    const heads = modo === 'op'
      ? [[L.oX, 'Orígenes'], [L.cX, 'Canales de integración'], [L.dX, 'Destinos']]
      : [[L.oX, 'De dónde viene'], [L.cX, 'Flujos'], [L.dX, 'Hacia dónde va']];
    heads.forEach(([x, t]) => svg.appendChild(el('text', { x, y: 30, class: 'mi-col-head' }, t)));

    // ── Aristas
    CANALES.forEach(c => {
      const d = datoDe(c.id), s = salud(c, d);
      const col = COLOR[s], w = grosor(_tasa(d));
      const anim = (s === 'ok' || s === 'warn') && (d.trafico || 0) > 0;

      if (c.origen) {
        const o = ORIGENES.find(n => n.id === c.origen);
        if (o) _dibujarArista(gE, curva(L.oX + L.oW, o.cy, L.cX, c.cy), col, w, anim, s, c.id, 'in');
      }
      if (c.padre) {
        const p = CANALES.find(x => x.id === c.padre);
        if (p) {
          const path = `M${L.cX - 6},${p.cy + 6} C${L.cX - 52},${p.cy + 16} ${L.cX - 52},${c.cy - 14} ${L.cX - 6},${c.cy}`;
          _dibujarArista(gE, path, 'var(--mi-purple)', 1.6, anim, s, c.id, 'int');
        }
      }
      if (c.destino) {
        const dn = DESTINOS.find(n => n.id === c.destino);
        if (dn) {
          _dibujarArista(gE, curva(L.cX + L.cW, c.cy, L.dX, dn.cy), col, w, anim, s, c.id, 'out');
          if ((d.cola || 0) > 0 && modo === 'op') {
            const bx = L.cX + L.cW + ((L.dX - L.cX - L.cW) / 2), by = (c.cy + dn.cy) / 2;
            const g = el('g', { class: 'mi-qbadge' });
            const txt = String(d.cola), ancho = 18 + txt.length * 6;
            g.appendChild(el('rect', { x: bx - ancho / 2, y: by - 9, width: ancho, height: 18, rx: 9, fill: 'var(--mi-bg)', stroke: col, 'stroke-width': 1 }));
            g.appendChild(el('text', { x: bx, y: by + 3.5, 'text-anchor': 'middle', fill: col }, txt));
            gE.appendChild(g);
          }
        }
      }
    });

    // ── Nodos laterales
    [[ORIGENES, L.oX, L.oW, 'origen'], [DESTINOS, L.dX, L.dW, 'destino']].forEach(([lista, x, w, tipo]) => {
      lista.forEach(n => {
        const g = el('g', { class: 'mi-node', tabindex: '0', role: 'button', 'data-nodo': n.id, 'aria-label': n.label });
        g.appendChild(el('rect', { class: 'mi-nbox', x, y: n.cy - L.nAlto / 2, width: w, height: L.nAlto, rx: 7 }));
        g.appendChild(el('text', { x: x + 13, y: n.cy - 2, class: 'mi-nlabel' }, modo === 'op' ? n.label : (n.humano || n.label)));
        g.appendChild(el('text', { x: x + 13, y: n.cy + 12, class: 'mi-nsub' }, modo === 'op' ? (n.sub || '') : ''));
        g.onclick = () => abrirNodo(tipo, n.id);
        g.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); abrirNodo(tipo, n.id); } };
        g.onmouseenter = () => resaltar(n.rel);
        g.onmouseleave = () => resaltar(null);
        gN.appendChild(g);
      });
    });

    // ── Nodos de canal
    CANALES.forEach(c => {
      const d = datoDe(c.id), s = salud(c, d), col = COLOR[s];
      const g = el('g', { class: 'mi-node', tabindex: '0', role: 'button', 'data-nodo': c.id, 'aria-label': c.nom });
      g.appendChild(el('rect', { class: 'mi-nbox', x: L.cX, y: c.y, width: L.cW, height: L.alto, rx: 7 }));
      g.appendChild(el('rect', { x: L.cX, y: c.y, width: 3, height: L.alto, rx: 1.5, fill: col, opacity: s === 'idle' || s === 'nodata' ? .45 : 1 }));
      g.appendChild(el('circle', { cx: L.cX + 18, cy: c.cy, r: 3.6, fill: col, opacity: s === 'nodata' ? .4 : 1 }));
      g.appendChild(el('text', { x: L.cX + 31, y: c.cy + 4, class: 'mi-nlabel' }, modo === 'op' ? c.nom : c.hum));

      if (modo === 'op') {
        let der = '', dcol = 'var(--mi-muted)';
        if (!d.fresco) { der = 'sin datos'; }
        else if (d.estado && d.estado !== 'RUNNING' && d.estado !== 'STARTED') { der = d.estado.toLowerCase(); dcol = COLOR.bad; }
        else if ((d.cola || 0) > 0) { der = d.cola + ' en cola'; dcol = col; }
        else if ((d.trafico || 0) === 0) { der = 'sin tráfico'; }
        else { der = _fmtTrafico(d) + ' msg'; dcol = 'var(--mi-muted)'; }
        g.appendChild(el('text', { x: L.cX + L.cW - 13, y: c.cy + 4, 'text-anchor': 'end', class: 'mi-nsub', fill: dcol }, der));
      } else {
        const txt = s === 'bad' ? 'Detenido' : s === 'warn' ? 'Con demora' : s === 'nodata' ? 'Sin datos' : 'Operativo';
        g.appendChild(el('text', { x: L.cX + L.cW - 13, y: c.cy + 4, 'text-anchor': 'end', class: 'mi-nlead', fill: s === 'ok' || s === 'idle' ? 'var(--mi-muted)' : col }, txt));
      }
      g.onclick = () => abrirNodo('canal', c.id);
      g.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); abrirNodo('canal', c.id); } };
      g.onmouseenter = () => resaltar([c.id]);
      g.onmouseleave = () => resaltar(null);
      gN.appendChild(g);
    });

    svg.appendChild(gE); svg.appendChild(gN);
    _pintarResumen(); _pintarLista(); _pintarReloj(); _pintarNotaAcum();
  }

  function _dibujarArista(g, d, col, w, anim, s, cid, dir) {
    const cls = 'mi-edge ' + (s === 'nodata' ? 'mi-nodata' : (anim ? 'mi-flow' : 'mi-idle'));
    const p = el('path', { d, class: cls, stroke: col, 'stroke-width': w, 'data-ch': cid, 'data-dir': dir, opacity: s === 'nodata' ? .35 : (s === 'idle' ? .5 : .9) });
    g.appendChild(p);
    const hit = el('path', { d, class: 'mi-hit', 'data-ch': cid });
    hit.onmouseenter = () => resaltar([cid]);
    hit.onmouseleave = () => resaltar(null);
    hit.onclick = () => abrirNodo('canal', cid);
    g.appendChild(hit);
  }

  function resaltar(ids) {
    const svg = $('mi-mapa');
    if (!svg) return;
    if (!ids) { svg.querySelectorAll('.mi-edge,.mi-node').forEach(n => n.classList.remove('mi-dim', 'mi-hl')); return; }
    svg.querySelectorAll('.mi-edge').forEach(p => p.classList.toggle('mi-dim', !ids.includes(p.dataset.ch)));
    svg.querySelectorAll('.mi-node').forEach(n => {
      const id = n.dataset.nodo;
      const rel = ids.includes(id) || ids.some(cid => {
        const c = CANALES.find(x => x.id === cid);
        return c && (c.origen === id || c.destino === id || c.padre === id);
      });
      n.classList.toggle('mi-dim', !rel);
    });
  }

  // ── Resumen superior ──
  function _pintarResumen() {
    let ok = 0, warn = 0, bad = 0, nd = 0, colaTot = 0;
    CANALES.forEach(c => {
      const d = datoDe(c.id), s = salud(c, d);
      colaTot += (d.cola || 0);
      if (s === 'bad') bad++; else if (s === 'warn') warn++; else if (s === 'nodata') nd++; else ok++;
    });
    const html = [
      `<div class="mi-chip"><i class="mi-dotm mi-d-ok"></i><span class="mi-n">${ok}</span><span class="mi-l">en servicio</span></div>`,
      warn ? `<div class="mi-chip"><i class="mi-dotm mi-d-warn"></i><span class="mi-n">${warn}</span><span class="mi-l">con demora</span></div>` : '',
      bad ? `<div class="mi-chip"><i class="mi-dotm mi-d-bad"></i><span class="mi-n">${bad}</span><span class="mi-l">detenidos</span></div>` : '',
      nd ? `<div class="mi-chip"><i class="mi-dotm mi-d-none"></i><span class="mi-n">${nd}</span><span class="mi-l">sin datos</span></div>` : '',
      META.sin_clasificar ? `<div class="mi-chip"><i class="mi-dotm mi-d-warn"></i><span class="mi-n">${META.sin_clasificar}</span><span class="mi-l">sin clasificar</span></div>` : '',
      modo === 'op' ? `<div class="mi-chip"><span class="mi-n">${colaTot.toLocaleString('es-AR')}</span><span class="mi-l">mensajes en cola</span></div>` : '',
    ].join('');
    const s = $('mi-summary'); if (s) s.innerHTML = html;
    const sub = $('mi-head-sub');
    if (sub) sub.textContent = modo === 'op'
      ? 'Topología HL7 derivada de la definición de canales de Mirth Connect.'
      : 'Estado de las conexiones entre los sistemas de la sede.';
  }

  // ── Vista lista ──
  function _pintarLista() {
    const filas = CANALES.map(c => {
      const d = datoDe(c.id), s = salud(c, d);
      const badge = s === 'bad' ? 'mi-status-critical' : s === 'warn' ? 'mi-status-warning' : s === 'nodata' ? 'mi-status-muted' : 'mi-status-online';
      const txt = s === 'bad' ? (d.fresco ? d.estado : 'Sin datos') : s === 'warn' ? 'Con demora' : s === 'nodata' ? 'Sin datos' : 'Operativo';
      return `<tr data-id="${c.id}">
        <td class="mi-cn">${modo === 'op' ? c.nom : c.hum}</td>
        <td><span class="mi-status-badge ${badge}">${txt}</span></td>
        ${modo === 'op' ? `<td class="mi-num" style="color:${d.cola ? COLOR[s] : 'var(--mi-muted)'}">${d.cola || '—'}</td><td class="mi-num" style="color:var(--mi-muted)">${d.fresco ? _fmtTrafico(d) : '—'}</td>` : ''}
      </tr>`;
    }).join('');
    const lv = $('mi-listview');
    if (!lv) return;
    lv.innerHTML = `<table><thead><tr>
      <th>${modo === 'op' ? 'Canal' : 'Flujo'}</th><th>Estado</th>
      ${modo === 'op' ? `<th class="mi-num">En cola</th><th class="mi-num">Tráfico${acumulado ? ' · ' + ACUM_MIN + ' min' : ''}</th>` : ''}
    </tr></thead><tbody>${filas}</tbody></table>`;
    lv.querySelectorAll('tbody tr').forEach(tr => {
      tr.onclick = () => abrirNodo('canal', tr.dataset.id);
    });
  }

  // ── Reloj ──
  function _pintarReloj() {
    const snap = TL[paso];
    const ts = snap ? snap.ts : new Date();
    const tstampEl = $('mi-tstamp');
    if (tstampEl) tstampEl.textContent = ts.toLocaleTimeString('es-AR', { hour: '2-digit', minute: '2-digit' });
    const live = $('mi-tlive'), esUltimo = paso === PASOS - 1;
    if (live) {
      live.textContent = esUltimo ? 'En vivo' : 'Hace ' + ((PASOS - 1 - paso) * (META.paso_min || 5)) + ' min';
      live.classList.toggle('mi-tpast', !esUltimo);
    }
  }

  // ============================================================
  // PANEL DE DETALLE
  // ============================================================
  function abrirNodo(tipo, id) {
    let eyebrow = '', titulo = '', cuerpo = '';

    if (tipo === 'canal') {
      const c = CANALES.find(x => x.id === id);
      if (!c) return;
      const d = datoDe(c.id), s = salud(c, d);
      eyebrow = modo === 'op' ? 'Canal de Mirth' : 'Flujo de información';
      titulo = modo === 'op' ? c.nom : c.hum;
      const badge = s === 'bad' ? 'mi-status-critical' : s === 'warn' ? 'mi-status-warning' : s === 'nodata' ? 'mi-status-muted' : 'mi-status-online';
      const txt = s === 'bad' ? (d.fresco ? d.estado : 'Sin datos') : s === 'warn' ? 'Con demora' : s === 'nodata' ? 'Sin datos frescos' : 'Operativo';
      const org = c.origen ? ORIGENES.find(n => n.id === c.origen) : null;
      const dst = c.destino ? DESTINOS.find(n => n.id === c.destino) : null;
      const padre = c.padre ? CANALES.find(x => x.id === c.padre) : null;

      cuerpo = `<span class="mi-status-badge ${badge}">${txt}</span>`;
      if (modo === 'op' && c.hum && c.hum !== c.nom) cuerpo += `<span style="margin-left:8px;font-size:.78rem;color:var(--mi-muted)">${c.hum}</span>`;
      if (!c.clasificado) cuerpo += `<span style="margin-left:8px;font-size:.72rem;color:var(--mi-amber)">· sin clasificar en el panel</span>`;
      cuerpo += _alertaDe(c, d, s);

      if (modo === 'op') {
        cuerpo += `<div class="mi-mgrid">
          <div class="mi-mcell"><div class="mi-k">En cola</div><div class="mi-v" style="color:${d.cola ? COLOR[s] : 'inherit'}">${d.fresco ? (d.cola || 0) : '—'}</div></div>
          <div class="mi-mcell"><div class="mi-k">${acumulado ? `Tráfico · ${ACUM_MIN} min` : 'Tráfico / 5 min'}</div><div class="mi-v">${d.fresco ? _fmtTrafico(d) : '—'}</div></div>
          <div class="mi-mcell"><div class="mi-k">${acumulado ? `Errores · ${ACUM_MIN} min` : 'Errores'}</div><div class="mi-v" style="color:${d.err ? 'var(--mi-red)' : 'inherit'}">${d.fresco ? _fmtN(d.err) : '—'}</div></div>
          <div class="mi-mcell"><div class="mi-k">Criticidad</div><div class="mi-v" style="font-size:.9rem;text-transform:capitalize">${c.crit}</div></div>
        </div>
        ${acumulado ? _flujoAcumulado(d) : ''}
        <div class="mi-sec">Cola · línea de tiempo</div>${_sparkline(c.id)}
        <div class="mi-sec">Ruteo</div>
        <div class="mi-kv"><span class="mi-k">Origen</span><span class="mi-v">${padre ? padre.nom + ' (interno)' : org ? org.label + (org.sub ? ' · ' + org.sub : '') : '—'}</span></div>
        <div class="mi-kv"><span class="mi-k">Destino</span><span class="mi-v">${dst ? dst.label + (dst.sub ? ' · ' + dst.sub : '') : 'canales internos'}</span></div>
        <div class="mi-kv"><span class="mi-k">Umbral de cola</span><span class="mi-v">${(UMBRALES[c.crit] || {}).warn} / ${(UMBRALES[c.crit] || {}).crit}</span></div>
        <div class="mi-kv"><span class="mi-k">Identificador de alerta</span><span class="mi-v">${c.tipo_alerta}</span></div>`;
      } else {
        cuerpo += `<div class="mi-sec">Recorrido</div>
        <div class="mi-kv"><span class="mi-k">Desde</span><span class="mi-v" style="font-family:inherit">${padre ? padre.hum : org ? (org.humano || org.label) : '—'}</span></div>
        <div class="mi-kv"><span class="mi-k">Hacia</span><span class="mi-v" style="font-family:inherit">${dst ? (dst.humano || dst.label) : 'Otros flujos'}</span></div>`;
      }
    } else {
      const lista = tipo === 'origen' ? ORIGENES : DESTINOS;
      const n = lista.find(x => x.id === id);
      if (!n) return;
      eyebrow = tipo === 'origen' ? 'Sistema de origen' : 'Sistema de destino';
      titulo = modo === 'op' ? n.label : (n.humano || n.label);
      const rel = CANALES.filter(c => c[tipo] === id);
      const malos = rel.filter(c => ['bad', 'warn'].includes(salud(c, datoDe(c.id))));
      cuerpo = `<span class="mi-status-badge ${malos.length ? 'mi-status-warning' : 'mi-status-online'}">${malos.length ? malos.length + ' flujo(s) con problema' : 'Todo en orden'}</span>`;
      if (modo === 'op') {
        cuerpo += `<div class="mi-sec">Punto de conexión</div>
        <div class="mi-kv"><span class="mi-k">Endpoint</span><span class="mi-v">${n.sub || '—'}</span></div>`;
        if (n.auto) cuerpo += `<div class="mi-kv"><span class="mi-k">Origen del dato</span><span class="mi-v" style="font-family:inherit">Auto-detectado (sin curar)</span></div>`;
        const vmTxt = _vmInfo(n.vm);
        if (n.vm) cuerpo += `<div class="mi-kv"><span class="mi-k">Servidor monitoreado</span><span class="mi-v">${n.vm}</span></div>
          <div class="mi-kv"><span class="mi-k">Estado del servidor</span><span class="mi-v" style="color:var(--mi-green)">${vmTxt || 'sin datos'}</span></div>`;
      }
      cuerpo += `<div class="mi-sec">${rel.length} ${modo === 'op' ? 'canal(es) conectado(s)' : 'flujo(s)'}</div>`;
      cuerpo += rel.map(c => {
        const s = salud(c, datoDe(c.id));
        return `<div class="mi-kv"><span class="mi-k" style="color:var(--mi-text)">${modo === 'op' ? c.nom : c.hum}</span><span class="mi-v" style="color:${COLOR[s]};font-family:inherit">${s === 'bad' ? 'Detenido' : s === 'warn' ? 'Demora' : s === 'nodata' ? 'Sin datos' : 'OK'}</span></div>`;
      }).join('');
    }

    const eyEl = $('mi-deyebrow'), titEl = $('mi-dtitle'), bodyEl = $('mi-dbody'), drawerEl = $('mi-drawer'), scrimEl = $('mi-scrim');
    if (eyEl) eyEl.textContent = eyebrow;
    if (titEl) titEl.textContent = titulo;
    if (bodyEl) bodyEl.innerHTML = cuerpo;
    if (drawerEl) drawerEl.classList.add('mi-on');
    if (scrimEl) scrimEl.classList.add('mi-on');
    tocando = { tipo, id };
  }

  // Detalle del acumulado de un canal: recibidos y enviados por separado (el mapa muestra su suma).
  function _flujoAcumulado(d) {
    const v = d.fresco;
    return `<div class="mi-sec">Flujo · últimos ${ACUM_MIN} min</div>
      <div class="mi-kv"><span class="mi-k">Recibidos</span><span class="mi-v">${v ? _fmtN(d.rx) : '—'}</span></div>
      <div class="mi-kv"><span class="mi-k">Enviados</span><span class="mi-v">${v ? _fmtN(d.tx) : '—'}</span></div>
      ${v && d.parcial ? `<div class="mi-kv"><span class="mi-k">Sin lecturas desde hace</span><span class="mi-v">~${d.sinLectura * (META.paso_min || 5)} min *</span></div>` : ''}`;
  }

  // Aclaración bajo la barra temporal, solo en modo acumulado.
  function _pintarNotaAcum() {
    const nota = $('mi-acum-nota');
    if (!nota) return;
    if (!acumulado) { nota.style.display = 'none'; return; }
    const v = _tramosVentana(), pasoMin = META.paso_min || 5;
    const parciales = CANALES.filter(c => { const d = datoDe(c.id); return d.fresco && d.parcial; }).length;
    let txt = `Acumulado de los últimos ${v.cant * pasoMin} min hasta el momento de la barra. Estado y cola son los de ese momento.`;
    if (v.cant < v.pedidos) txt += ` Solo hay ${v.cant * pasoMin} min de historial hasta este punto.`;
    if (parciales) txt += ` * ${parciales} canal(es) sin lecturas recientes: su total no incluye lo posterior a la última lectura y podría ser mayor.`;
    nota.textContent = txt;
    nota.style.display = 'block';
  }

  function _alertaDe(c, d, s) {
    if (s === 'nodata') {
      const minutos = _minutosSinDatos(c.id);
      return `<div class="mi-alertbox mi-a-warn"><span class="mi-a-t">MIRTH_STALE</span>No llegan lecturas frescas de este canal${minutos ? ` desde hace ${minutos} min` : ''}. Puede haber sido eliminado o el agente perdió acceso.</div>`;
    }
    if (d.estado === 'ERROR') return `<div class="mi-alertbox mi-a-crit"><span class="mi-a-t">MIRTH_CH_DOWN · crítica</span>El canal está en error de forma sostenida y dejó de procesar. ${d.cola || 0} mensajes esperando.</div>`;
    if (d.estado === 'STOPPED') return `<div class="mi-alertbox mi-a-crit"><span class="mi-a-t">MIRTH_CH_DOWN · crítica</span>Canal detenido. Los mensajes se acumulan en el origen desde entonces.</div>`;
    if (s === 'warn') return `<div class="mi-alertbox mi-a-warn"><span class="mi-a-t">MIRTH_QUEUE_TREND · atención</span>La cola viene creciendo sin drenar. El destino está tardando o dejó de aceptar mensajes.</div>`;
    return `<div class="mi-alertbox mi-a-ok"><span class="mi-a-t">Sin incidentes</span>El canal procesa con normalidad.</div>`;
  }

  function _minutosSinDatos(cid) {
    const serie = TL.map(s => (s.ch[cid] || {}).fresco);
    let i = serie.length - 1, buckets = 0;
    while (i >= 0 && serie[i] === false) { buckets++; i--; }
    return buckets * (META.paso_min || 5);
  }

  function _sparkline(cid) {
    const vals = TL.map(s => (s.ch[cid] || {}).cola || 0), max = Math.max(10, ...vals);
    const W = 340, H = 54;
    const pts = vals.map((v, i) => [8 + i * (W - 16) / Math.max(1, PASOS - 1), H - 6 - (v / max) * (H - 16)]);
    const d = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ',' + p[1].toFixed(1)).join(' ');
    const area = d + ` L${pts[pts.length - 1][0].toFixed(1)},${H - 6} L8,${H - 6} Z`;
    const cx = (pts[paso] || pts[pts.length - 1] || [8, 8])[0].toFixed(1);
    const cy = (pts[paso] || pts[pts.length - 1] || [8, 8])[1].toFixed(1);
    return `<div class="mi-spark"><svg viewBox="0 0 ${W} ${H}" width="100%" height="${H}" aria-hidden="true">
      <path d="${area}" fill="rgba(255,169,64,.12)"/>
      <path d="${d}" fill="none" stroke="#FFA940" stroke-width="1.6" stroke-linejoin="round"/>
      <line x1="${cx}" y1="4" x2="${cx}" y2="${H - 6}" stroke="#4A8FFF" stroke-width="1" stroke-dasharray="2 3"/>
      <circle cx="${cx}" cy="${cy}" r="3" fill="#4A8FFF"/>
    </svg><div style="display:flex;justify-content:space-between;font-size:.66rem;color:var(--mi-muted2);margin-top:2px"><span>-${Math.round(PASOS * (META.paso_min || 5) / 60 * 10) / 10} h</span><span>máx ${max}</span><span>ahora</span></div></div>`;
  }

  function cerrarDrawer() {
    const d = $('mi-drawer'), s = $('mi-scrim');
    if (d) d.classList.remove('mi-on');
    if (s) s.classList.remove('mi-on');
    tocando = null;
  }

  // Solo cierra el drawer del mapa -- no le roba Escape a otros modales de index_beta.html.
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    const d = $('mi-drawer');
    if (d && d.classList.contains('mi-on')) cerrarDrawer();
  });

  // ============================================================
  // CONTROLES
  // ============================================================
  function setModo(m) {
    modo = m;
    const bOp = $('mi-m-op'), bEst = $('mi-m-est');
    if (bOp) bOp.classList.toggle('active', m === 'op');
    if (bEst) bEst.classList.toggle('active', m === 'est');
    if (CANALES.length) render();
    if (tocando) abrirNodo(tocando.tipo, tocando.id);
  }

  function setFormato(f) {
    formato = f;
    const tab = $('tab-mapa');
    if (tab) tab.classList.toggle('mi-aslist', f === 'lista');
    const bMap = $('mi-f-map'), bLst = $('mi-f-lst');
    if (bMap) bMap.classList.toggle('active', f === 'mapa');
    if (bLst) bLst.classList.toggle('active', f === 'lista');
  }

  function toggleAcumulado() {
    acumulado = !acumulado;
    const b = $('mi-acum');
    if (b) { b.classList.toggle('active', acumulado); b.setAttribute('aria-pressed', String(acumulado)); }
    if (CANALES.length) render();
    if (tocando) abrirNodo(tocando.tipo, tocando.id);
  }

  function togglePlay() {
    playing = !playing;
    const ico = $('mi-play-ico');
    if (ico) ico.innerHTML = playing
      ? '<rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/>'
      : '<polygon points="6,4 20,12 6,20"/>';
    if (playing) {
      if (paso >= PASOS - 1) paso = 0;
      timer = setInterval(() => {
        paso++;
        if (paso >= PASOS - 1) { paso = PASOS - 1; togglePlay(); }
        const scrubEl = $('mi-scrub'); if (scrubEl) scrubEl.value = String(paso);
        render();
        if (tocando) abrirNodo(tocando.tipo, tocando.id);
      }, 320);
    } else if (timer) { clearInterval(timer); timer = null; }
  }

  // El listener del scrub se ata una sola vez (el input vive siempre en el
  // DOM, dentro de #tab-mapa, aunque no haya datos cargados todavía).
  document.addEventListener('DOMContentLoaded', () => {
    const scrubEl = $('mi-scrub');
    if (scrubEl) {
      scrubEl.addEventListener('input', () => {
        paso = +scrubEl.value;
        if (CANALES.length) render();
        if (tocando) abrirNodo(tocando.tipo, tocando.id);
      });
    }
  });

  window.MapaIntegraciones = { cargar, destroy, setModo, setFormato, togglePlay, toggleAcumulado, abrirNodo, cerrarDrawer };
})();
