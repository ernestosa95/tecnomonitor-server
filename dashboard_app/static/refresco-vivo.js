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
  //
  // Antes, apenas se detectaba un db_timestamp nuevo se aplicaba el
  // re-render de una: renderizarDetalle() reconstruye el innerHTML de la
  // cabecera/tarjetas superiores, y eso colapsaba cualquier tarjeta
  // plegable que el usuario tuviera abierta -- molesto si estás mirando
  // algo puntual y el dato llega cada 30s solo.
  //
  // Ahora se separa: el poll SIEMPRE detecta si hay dato nuevo (comparando
  // db_timestamp), pero NO lo aplica -- lo deja en `dataPendiente` y
  // muestra el botón "Datos nuevos" en la tarjeta del hospital. Recién al
  // click se aplica (aplicarDatosPendientes), con el mismo código que
  // antes corría solo. Si llega un dato más nuevo todavía mientras el
  // botón ya está visible, se pisa `dataPendiente` con el más reciente --
  // nunca se apila, el click siempre aplica lo último disponible.
  var tsAplicado = null;   // último db_timestamp que está REALMENTE en pantalla
  var hospGate = null;     // hospital sobre el que aplica tsAplicado
  var dataPendiente = null; // último fetch con timestamp distinto al aplicado, o null
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

  function mostrarBotonPendiente(mostrar) {
    var btn = document.getElementById('btn-datos-nuevos');
    if (btn) btn.style.display = mostrar ? 'flex' : 'none';
  }

  function limpiarPendiente() {
    dataPendiente = null;
    mostrarBotonPendiente(false);
  }

  function refrescarDetalle(aplicarInmediato) {
    if (!detalleAbierto()) { tsAplicado = null; limpiarPendiente(); return; }
    var id = currentHospitalId;
    if (id !== hospGate) { hospGate = id; tsAplicado = null; limpiarPendiente(); } // cambió de hospital

    if (typeof authFetch !== 'function') return;
    authFetch('/api/hospital/' + encodeURIComponent(id))
      .then(function (r) { return r && r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data || data.error) return;
        if (!detalleAbierto() || currentHospitalId !== id) return; // el usuario cerró/cambió

        var ts = data.db_timestamp || null;
        if (!ts) return;

        // Primera observación tras entrar a un hospital (o tras reiniciar):
        // es la línea base, no un "dato nuevo". verDetalle() acaba de
        // renderizar este mismo dato -- marcarlo pendiente mostraba el
        // botón falsamente ~30s después de abrir cualquier hospital.
        if (tsAplicado === null) { tsAplicado = ts; return; }

        if (ts === tsAplicado) return; // nada nuevo respecto de lo que ya se ve

        // Dato nuevo: se guarda y se avisa, no se aplica solo -- salvo que
        // se pida explícitamente inmediato (ver más abajo: volver de la
        // pestaña oculta o reconexión del socket, donde la vista puede
        // llevar rato completamente congelada, no son los 30s normales).
        dataPendiente = data;
        if (aplicarInmediato) { aplicarDatosPendientes(); return; }
        mostrarBotonPendiente(true);
      })
      .catch(function () {});
  }

  // Aplica dataPendiente (llamado por el click del botón "Datos nuevos").
  // Mismo código que antes corría automático dentro de refrescarDetalle().
  function aplicarDatosPendientes() {
    if (!dataPendiente || !detalleAbierto()) { limpiarPendiente(); return; }
    var id = currentHospitalId;
    var data = dataPendiente;
    var ts = data.db_timestamp || null;

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
      } else if (tab === 'mapa') {
        if (window.MapaIntegraciones) window.MapaIntegraciones.cargar(id);
      } else {
        if (typeof cargarHistorial === 'function') cargarHistorial(currentRangeHours, id);
      }
    } catch (e) { console.warn('refresco tab ' + tab + ':', e); }

    tsAplicado = ts;
    limpiarPendiente();
  }
  window.aplicarDatosPendientes = aplicarDatosPendientes;

  // Llamado desde verDetalle() (script.js) al entrar a un hospital -- sin
  // esto, si quedaba un "Datos nuevos" pendiente del hospital anterior, se
  // veía un toque en el nuevo hasta el próximo poll (hasta 30s).
  window.reiniciarBotonDatosNuevos = function () {
    tsAplicado = null;
    hospGate = null;
    limpiarPendiente();
  };

  // ------------------------------------------------------------------- TIMERS
  setInterval(function () { if (visible() && !detalleAbierto()) refrescarHome(); }, CFG.homeMs);
  setInterval(function () { if (visible() && detalleAbierto()) refrescarDetalle(); }, CFG.detalleMs);

  // Refresco inmediato al volver a la pestaña (no esperar al próximo tick).
  // Acá sí se aplica directo (aplicarInmediato=true): la vista puede llevar
  // minutos congelada mientras la pestaña estuvo oculta (el timer de arriba
  // no corre si !visible()), así que no es el caso normal de cada 30s que
  // el botón "Datos nuevos" está pensado para no interrumpir. No hace falta
  // resetear tsAplicado: si el dato cambió mientras estuvo oculta, la
  // comparación normal ya lo detecta; si no cambió, no hay nada que aplicar.
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    if (detalleAbierto()) refrescarDetalle(true); else refrescarHome();
  });

  // Gancho para la reconexión del WebSocket (ver README al pie de este archivo)
  window.refrescarDatosAhora = function () {
    if (detalleAbierto()) refrescarDetalle(true); else refrescarHome();
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
