/* ============================================================================
 * revision-borrador-ia.js — Revisión/edición PAGINADA de borradores IA
 * ----------------------------------------------------------------------------
 * Reemplazo del modal de revisión de borrador. Se construye solo por JS (patrón
 * asegurarModalDetalleLog), matchea el tema oscuro de la beta y se cablea a:
 *
 *   GET  /api/informes-ia/reportes/{id}/json      → trae el borrador
 *   PUT  /api/informes-ia/reportes/{id}/json      → guarda  (body: {data:{...}})
 *   POST /api/informes-ia/reportes/{id}/aprobar   → aprueba (solo Admin)
 *   GET  /api/informes-ia/reportes/{id}/pdf       → PDF (solo aprobado)
 *
 * Instalar:
 *   1) Incluir este archivo DESPUÉS de script.js (o pegarlo al final).
 *   2) Ocultar/eliminar el modal de revisión anterior para no duplicar.
 *   3) Desde el historial, el botón "Revisar" llama:
 *          abrirRevisionBorrador(reportId, { hospital: 'P23', periodo: '01/07 → 31/07' });
 *
 * Nota: asume que authFetch(url, opciones) reenvía method/headers/body a fetch
 * (como fetch nativo con credenciales). Si tu authFetch solo hace GET, avisá y
 * se adapta el guardado/aprobación.
 * ========================================================================== */
(function () {
  'use strict';

  var API = '/api/informes-ia/reportes';

  // ── CONFIG DE PÁGINAS ──────────────────────────────────────────────────
  // Reagrupar secciones = editar SOLO este array. Cada campo:
  //   path : ruta dentro del JSON ("a.b")
  //   label: etiqueta visible
  //   ro   : true = solo lectura (telemetría, no editable)
  //   tipo : 'textarea' (default) | 'texto'
  //   rows : alto del textarea
  var PAGINAS = [
    { titulo: 'Resumen ejecutivo', campos: [
      { path: 'resumen.uptime', label: 'Uptime', ro: true, tipo: 'texto' },
      { path: 'resumen.texto',  label: 'Resumen', tipo: 'textarea', rows: 9 }
    ]},
    { titulo: 'Infraestructura', campos: [
      { path: 'infraestructura.energia', label: 'Energía',  tipo: 'textarea', rows: 3 },
      { path: 'infraestructura.termica', label: 'Térmica',  tipo: 'textarea', rows: 3 },
      { path: 'infraestructura.mensaje', label: 'Mensaje',  tipo: 'textarea', rows: 5 }
    ]},
    { titulo: 'Incidencias', campos: [
      { path: 'incidencias.externas', label: 'Incidencias externas', ro: true, tipo: 'texto' },
      { path: 'incidencias.internas', label: 'Incidencias internas', ro: true, tipo: 'texto' },
      { path: 'incidencias.analisis', label: 'Análisis de incidencias', tipo: 'textarea', rows: 9 }
    ]},
    { titulo: 'Calidad', campos: [
      { path: 'calidad.estabilidad',    label: 'Estabilidad',    tipo: 'textarea', rows: 4 },
      { path: 'calidad.caso_destacado', label: 'Caso destacado', tipo: 'textarea', rows: 4 }
    ]},
    { titulo: 'Recomendación', campos: [
      { path: 'recomendacion', label: 'Recomendación', tipo: 'textarea', rows: 9 }
    ]}
  ];

  // ── estado en memoria ───────────────────────────────────────────────────
  var S = { reportId: null, data: null, meta: null, pagina: 0, guardando: false, aprobado: false };

  // ── helpers de path ─────────────────────────────────────────────────────
  function getPath(obj, path) {
    return path.split('.').reduce(function (o, k) { return (o == null ? undefined : o[k]); }, obj);
  }
  function setPath(obj, path, val) {
    var ks = path.split('.'), o = obj;
    for (var i = 0; i < ks.length - 1; i++) {
      if (o[ks[i]] == null || typeof o[ks[i]] !== 'object') o[ks[i]] = {};
      o = o[ks[i]];
    }
    o[ks[ks.length - 1]] = val;
  }
  function esc(s) { return (s == null ? '' : String(s)).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

  // ── construcción del modal (una vez) ────────────────────────────────────
  function asegurarModal() {
    if (document.getElementById('modal-revision-ia')) return;
    var m = document.createElement('div');
    m.id = 'modal-revision-ia';
    m.className = 'modal-overlay';
    m.style.cssText = 'display:none;z-index:5200';
    m.innerHTML =
      '<div class="modal-content" style="max-width:640px;width:92%;display:flex;flex-direction:column;gap:0;padding:0;overflow:hidden">' +
        '<div style="display:flex;align-items:center;justify-content:space-between;padding:18px 22px;border-bottom:1px solid var(--border)">' +
          '<h3 style="margin:0;color:var(--text)">Revisión de borrador IA</h3>' +
          '<button onclick="cerrarRevisionBorrador()" style="background:none;border:none;color:var(--muted);font-size:1.4em;cursor:pointer;line-height:1">&times;</button>' +
        '</div>' +
        '<div id="rev-subhead" style="padding:12px 22px;border-bottom:1px solid var(--border);font-size:.85em;color:var(--muted);display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap"></div>' +
        '<div id="rev-progress" style="padding:14px 22px 4px"></div>' +
        '<div id="rev-body" style="padding:8px 22px 18px;overflow-y:auto;max-height:52vh"></div>' +
        '<div id="rev-footer" style="display:flex;align-items:center;gap:10px;padding:16px 22px;border-top:1px solid var(--border)"></div>' +
      '</div>';
    document.body.appendChild(m);
    m.addEventListener('click', function (e) { if (e.target === m) cerrarRevisionBorrador(); });
  }

  function mostrar() { document.getElementById('modal-revision-ia').style.display = 'flex'; }

  // ── API pública ─────────────────────────────────────────────────────────
  window.abrirRevisionBorrador = function (reportId, meta) {
    S = { reportId: reportId, data: null, meta: meta || {}, pagina: 0, guardando: false, aprobado: false };
    asegurarModal();
    renderSubhead();
    document.getElementById('rev-progress').innerHTML = '';
    document.getElementById('rev-footer').innerHTML = '';
    document.getElementById('rev-body').innerHTML = '<div style="padding:40px 0;text-align:center;color:var(--muted)">Cargando borrador…</div>';
    mostrar();

    authFetch(API + '/' + encodeURIComponent(reportId) + '/json')
      .then(function (res) {
        if (res.status === 409) throw new Error('El reporte todavía no es un borrador editable (¿sigue procesándose o quedó en error?).');
        if (res.status === 404) throw new Error('No se encontró el reporte.');
        if (!res.ok) throw new Error('No se pudo cargar el borrador (HTTP ' + res.status + ').');
        return res.json();
      })
      .then(function (data) { S.data = data || {}; S.pagina = 0; render(); })
      .catch(function (e) { renderError(e.message || String(e)); });
  };

  window.cerrarRevisionBorrador = function () {
    var m = document.getElementById('modal-revision-ia');
    if (m) m.style.display = 'none';
  };

  // ── render ──────────────────────────────────────────────────────────────
  function renderSubhead() {
    var sh = document.getElementById('rev-subhead');
    var hosp = (S.meta && S.meta.hospital) || (S.data && S.data.meta && S.data.meta.hospital) || '—';
    var per = (S.meta && S.meta.periodo) || '—';
    sh.innerHTML =
      '<span><strong style="color:var(--text)">Hospital:</strong> ' + esc(hosp) + '</span>' +
      '<span><strong style="color:var(--text)">Periodo:</strong> ' + esc(per) + '</span>';
  }

  function renderError(msg) {
    document.getElementById('rev-body').innerHTML =
      '<div style="padding:32px 8px;text-align:center;color:var(--red,#ff5c5c)">' + esc(msg) + '</div>';
    document.getElementById('rev-progress').innerHTML = '';
    document.getElementById('rev-footer').innerHTML =
      '<button class="btn-action" onclick="cerrarRevisionBorrador()" style="background:var(--muted);flex:1">Cerrar</button>';
  }

  function render() {
    renderSubhead();
    renderProgress();
    renderBody();
    renderFooter();
  }

  function renderProgress() {
    var cont = document.getElementById('rev-progress');
    var total = PAGINAS.length, i = S.pagina;
    var dots = '';
    for (var k = 0; k < total; k++) {
      var on = k === i, done = k < i;
      var bg = on ? 'var(--purple)' : (done ? 'var(--purple)' : 'var(--surface2)');
      var op = on ? '1' : (done ? '.5' : '1');
      dots += '<span style="flex:1;height:4px;border-radius:2px;background:' + bg + ';opacity:' + op + '"></span>';
    }
    cont.innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">' +
        '<span style="font-size:.78em;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)">Paso ' + (i + 1) + ' de ' + total + '</span>' +
        '<span style="font-weight:700;color:var(--text)">' + esc(PAGINAS[i].titulo) + '</span>' +
      '</div>' +
      '<div style="display:flex;gap:6px">' + dots + '</div>';
  }

  function renderBody() {
    if (S.pagina === 0) {
      var nota = '<div style="background:var(--surface2);border:1px solid var(--border);border-radius:var(--radius2,10px);padding:12px 14px;margin-bottom:16px;font-size:.85em;color:var(--muted);line-height:1.5">' +
        '<strong style="color:var(--text)">Este es un BORRADOR generado por IA.</strong> Revisá y editá los textos antes de aprobar. ' +
        'Los datos numéricos vienen calculados de la telemetría y no son editables.</div>';
    } else { nota = ''; }

    var campos = PAGINAS[S.pagina].campos.map(function (c) {
      var val = getPath(S.data, c.path);
      if (c.ro) {
        return '<div class="input-group" style="margin-bottom:16px">' +
          '<label>' + esc(c.label) + ' <span style="color:var(--muted);font-weight:400;font-size:.85em">(solo lectura)</span></label>' +
          '<input type="text" value="' + esc(val) + '" disabled ' +
            'style="width:100%;opacity:.75;cursor:not-allowed"></div>';
      }
      if (c.tipo === 'texto') {
        return '<div class="input-group" style="margin-bottom:16px">' +
          '<label>' + esc(c.label) + '</label>' +
          '<input type="text" data-path="' + c.path + '" value="' + esc(val) + '" oninput="revOnInput(this)"></div>';
      }
      return '<div class="input-group" style="margin-bottom:16px">' +
        '<label>' + esc(c.label) + '</label>' +
        '<textarea data-path="' + c.path + '" rows="' + (c.rows || 5) + '" oninput="revOnInput(this)" ' +
          'style="resize:vertical">' + esc(val) + '</textarea></div>';
    }).join('');

    document.getElementById('rev-body').innerHTML = nota + campos;
  }

  // guarda el edit en memoria a medida que se escribe (no se pierde al navegar)
  window.revOnInput = function (el) {
    var p = el.getAttribute('data-path');
    if (p) setPath(S.data, p, el.value);
  };

  function renderFooter() {
    var f = document.getElementById('rev-footer');
    if (S.aprobado) {
      f.innerHTML =
        '<span style="flex:1;color:var(--green,#39d98a);font-weight:600">✅ Aprobado</span>' +
        '<button class="btn-action" onclick="revDescargarPdf()" style="background:var(--purple);width:auto">Descargar PDF</button>' +
        '<button class="btn-action" onclick="cerrarRevisionBorrador()" style="background:var(--muted);width:auto">Cerrar</button>';
      return;
    }
    var esUltima = S.pagina === PAGINAS.length - 1;
    var btnPrev = '<button class="btn-action" onclick="revIr(-1)" ' + (S.pagina === 0 ? 'disabled style="opacity:.4;background:var(--surface2);width:auto"' : 'style="background:var(--surface2);width:auto"') + '>← Anterior</button>';
    var btnNextOAprobar = esUltima
      ? '<button class="btn-action" onclick="revAprobar()" style="background:var(--purple);width:auto;flex:1">✅ Aprobar (irreversible)</button>'
      : '<button class="btn-action" onclick="revIr(1)" style="background:var(--purple);width:auto">Siguiente →</button>';
    f.innerHTML =
      '<button class="btn-action" onclick="revGuardar()" style="background:var(--surface2);width:auto">Guardar cambios</button>' +
      '<span id="rev-msg" style="flex:1;font-size:.82em;color:var(--muted)"></span>' +
      btnPrev + btnNextOAprobar;
  }

  window.revIr = function (delta) {
    var next = S.pagina + delta;
    if (next < 0 || next >= PAGINAS.length) return;
    S.pagina = next;
    render();
    document.getElementById('rev-body').scrollTop = 0;
  };

  function msg(txt, color) {
    var el = document.getElementById('rev-msg');
    if (el) { el.textContent = txt; el.style.color = color || 'var(--muted)'; }
  }

  // ── guardar (PUT {data:{...}}) ──────────────────────────────────────────
  function putJson() {
    return authFetch(API + '/' + encodeURIComponent(S.reportId) + '/json', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ data: S.data })
    });
  }

  window.revGuardar = function () {
    if (S.guardando) return;
    S.guardando = true; msg('Guardando…');
    putJson()
      .then(function (r) {
        if (r.status === 409) throw new Error('No se puede editar: el reporte ya no es un borrador.');
        if (!r.ok) throw new Error('Error al guardar (HTTP ' + r.status + ').');
        msg('Guardado ✓', 'var(--green,#39d98a)');
      })
      .catch(function (e) { msg(e.message || 'Error al guardar', 'var(--red,#ff5c5c)'); })
      .finally(function () { S.guardando = false; });
  };

  // ── aprobar (guarda + POST aprobar) ─────────────────────────────────────
  window.revAprobar = function () {
    if (S.guardando) return;
    if (!confirm('Aprobar es IRREVERSIBLE: el informe queda inmutable y recién ahí se genera el PDF.\n\n¿Confirmás la aprobación?')) return;
    S.guardando = true; msg('Guardando y aprobando…');
    putJson()
      .then(function (r) {
        if (!r.ok && r.status !== 409) throw new Error('Error al guardar antes de aprobar (HTTP ' + r.status + ').');
        return authFetch(API + '/' + encodeURIComponent(S.reportId) + '/aprobar', { method: 'POST' });
      })
      .then(function (r) {
        if (r.status === 403) throw new Error('Solo un usuario Admin puede aprobar informes.');
        if (r.status === 409) throw new Error('El reporte no está en estado borrador (¿ya aprobado o en error?).');
        if (!r.ok) throw new Error('Error al aprobar (HTTP ' + r.status + ').');
        S.aprobado = true;
        renderFooter();
        msg('');
      })
      .catch(function (e) { msg(e.message || 'Error al aprobar', 'var(--red,#ff5c5c)'); })
      .finally(function () { S.guardando = false; });
  };

  // ── PDF (solo aprobado) ─────────────────────────────────────────────────
  window.revDescargarPdf = function () {
    authFetch(API + '/' + encodeURIComponent(S.reportId) + '/pdf')
      .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.blob(); })
      .then(function (blob) {
        var url = URL.createObjectURL(blob);
        window.open(url, '_blank');
        setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
      })
      .catch(function () { msg('No se pudo obtener el PDF.', 'var(--red,#ff5c5c)'); });
  };

})();