/* ============================================================================
 * refresco-vivo.js  —  Iteración final · Punto 1: refresco de datos en vivo
 * ----------------------------------------------------------------------------
 * Reemplaza los setInterval viejos que en index_beta NUNCA disparaban (estaban
 * condicionados a que la VISTA tuviera .active, cosa que el scroll continuo de
 * la beta no marca) y añade refresco de la vista de detalle de hospital, que
 * antes cargaba una sola vez y quedaba congelada.
 *
 * Cómo instalar:
 *   1) En script.js, BORRAR (o comentar) las dos líneas viejas del init:
 *          setInterval(() => { if(...view-dashboard...active) cargarDatos(); }, 30000);
 *          setInterval(() => { if(...view-mapa...active) cargarDatosMapa(); }, 60000);
 *   2) Pegar este archivo al FINAL de script.js  (o incluirlo como
 *          <script src="/static/refresco-vivo.js"></script>  DESPUÉS de script.js).
 *   3) En initWebSocket(), agregar el gancho de reconexión (ver README al pie).
 *
 * No toca el backend. Depende solo de funciones que ya existen en script.js:
 *   authFetch, cargarDatos, aplicarFiltros, renderizarDetalle, cargarHistorial,
 *   cargarHistorialKpiGlobal, cargarEstadoSoftware, actualizarGrafico.
 * Todas se invocan con guardas por si en algún template no existieran.
 * ========================================================================== */
(function () {
  'use strict';

  var CFG = {
    homeMs: 30000,      // cadencia del dashboard/resumen (el endpoint cachea 30s)
    detalleMs: 30000,   // cadencia del detalle de hospital abierto
    pausarOculto: true, // no refrescar si la pestaña no está visible
    refrescarCabecera: true // re-render de tarjetas superiores + VMs en el detalle
  };

  // ----------------------------------------------------------------- helpers
  function visible() {
    return !CFG.pausarOculto || document.visibilityState === 'visible';
  }

  // En beta el detalle es un modal: #detalle-backdrop.active. En el template
  // viejo no existe ese nodo, así que detalleAbierto() = false y el módulo
  // simplemente refresca el home (comportamiento equivalente al viejo).
  function detalleAbierto() {
    var bd = document.getElementById('detalle-backdrop');
    return !!(bd && bd.classList.contains('active')) &&
           typeof currentHospitalId !== 'undefined' && !!currentHospitalId;
  }

  function tabActivo() {
    var t = document.querySelector('#view-detalle .tab-content.active');
    return t ? (t.id || '').replace('tab-', '') : 'infra';
  }

  function valDe(elId) { var e = document.getElementById(elId); return e ? e.value : null; }
  function restaurar(elId, val) {
    if (val == null) return;
    var e = document.getElementById(elId);
    if (e && [].some.call(e.options || [], function (o) { return o.value === val; })) e.value = val;
  }

  // ------------------------------------------------------------- HOME/RESUMEN
  function refrescarHome() {
    if (typeof cargarDatos !== 'function') return;
    Promise.resolve(cargarDatos()).then(function () {
      // Reaplicar el filtro/búsqueda del usuario tras el re-render de la tabla
      if (typeof aplicarFiltros === 'function') { try { aplicarFiltros(); } catch (e) {} }
      // Re-render de marcadores del mapa resumen (si la función existe)
      if (typeof renderizarMarcadoresDash === 'function') { try { renderizarMarcadoresDash(); } catch (e) {} }
    }).catch(function () {});
  }

  // ------------------------------------------------------- DETALLE (gated TS)
  var ultimoTs = null;     // último db_timestamp renderizado
  var hospGate = null;     // hospital sobre el que aplica ultimoTs
  var nombreCache = {};    // hospital_id -> nombre (para no perder el nombre en el re-render)

  function nombreDe(id) {
    if (nombreCache[id]) return Promise.resolve(nombreCache[id]);
    if (typeof authFetch !== 'function') return Promise.resolve(null);
    return authFetch('/api/hospitales-metadata')
      .then(function (r) { return r && r.ok ? r.json() : []; })
      .then(function (list) {
        (list || []).forEach(function (h) { if (h.hospital_id) nombreCache[h.hospital_id] = h.nombre; });
        return nombreCache[id] || null;
      })
      .catch(function () { return null; });
  }

  function refrescarDetalle() {
    if (!detalleAbierto()) { ultimoTs = null; return; }
    var id = currentHospitalId;
    if (id !== hospGate) { hospGate = id; ultimoTs = null; } // cambió de hospital

    if (typeof authFetch !== 'function') return;
    authFetch('/api/hospital/' + encodeURIComponent(id))
      .then(function (r) { return r && r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || data.error) return;
        if (!detalleAbierto() || currentHospitalId !== id) return; // el usuario cerró/cambió

        var ts = data.db_timestamp || null;
        if (ts && ts === ultimoTs) return; // no hay dato nuevo → no redibujar nada
        ultimoTs = ts;

        // 1) Cabecera + tarjetas superiores + VMs (el "ahora": CPU/RAM/temp/estado)
        if (CFG.refrescarCabecera && typeof renderizarDetalle === 'function') {
          var src = valDe('chart-source'), met = valDe('chart-metric'); // preservar selección
          nombreDe(id).then(function (nombre) {
            if (currentHospitalId !== id) return;
            if (nombre) data.nombre_real = nombre;
            try { renderizarDetalle(data, id); } catch (e) { console.warn('refresco cabecera:', e); }
            restaurar('chart-source', src);
            restaurar('chart-metric', met);
            if (typeof actualizarGrafico === 'function') { try { actualizarGrafico(); } catch (e) {} }
          });
        }

        // 2) Solo la pestaña activa, con el rango YA elegido por el usuario
        var tab = tabActivo();
        try {
          if (tab === 'logs') {
            if (typeof cargarEstadoSoftware === 'function') cargarEstadoSoftware(id);
          } else if (tab === 'kpis') {
            if (typeof cargarHistorialKpiGlobal === 'function') cargarHistorialKpiGlobal(currentKpiRangeHours, id);
          } else {
            if (typeof cargarHistorial === 'function') cargarHistorial(currentRangeHours, id);
          }
        } catch (e) { console.warn('refresco tab ' + tab + ':', e); }
      })
      .catch(function () {});
  }

  // ------------------------------------------------------------------- TIMERS
  setInterval(function () { if (visible() && !detalleAbierto()) refrescarHome(); }, CFG.homeMs);
  setInterval(function () { if (visible() && detalleAbierto()) refrescarDetalle(); }, CFG.detalleMs);

  // Refresco inmediato al volver a la pestaña (no esperar al próximo tick)
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    ultimoTs = null;
    if (detalleAbierto()) refrescarDetalle(); else refrescarHome();
  });

  // Gancho para la reconexión del WebSocket (ver README al pie de este archivo)
  window.refrescarDatosAhora = function () {
    ultimoTs = null;
    if (detalleAbierto()) refrescarDetalle(); else refrescarHome();
  };

  console.log('[refresco-vivo] activo — home ' + CFG.homeMs + 'ms, detalle ' + CFG.detalleMs + 'ms');
})();

/* ============================================================================
 * README — enganche del WebSocket (paso 3)
 * ----------------------------------------------------------------------------
 * En script.js, dentro de initWebSocket(), justo después de crear wsAlertas,
 * agregar:
 *
 *     wsAlertas.onopen = function () {
 *         if (typeof window.refrescarDatosAhora === 'function') window.refrescarDatosAhora();
 *     };
 *
 * Así, cada vez que el socket (re)conecta tras una caída de internet, se fuerza
 * un resync de lo que el usuario esté viewndo (home o detalle). Es idempotente:
 * en la primera conexión solo provoca un refresco inmediato inofensivo.
 * ========================================================================== */
