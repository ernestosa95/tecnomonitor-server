// ==========================================
// --- CONSTANTES Y GLOBALES (PRIMERAS LÍNEAS) ---
// ==========================================
const EXCLUDED_AETS = ['CLIENT', 'WADO', 'PACS' ];
const EXCLUDED_MODS = ['DOC'];

let mapDashInstance = null;
let mapDashMarkers = null;
let currentDashMapFilter = 'all';
let currentSoftwareMinutes = 0;

// --- FUNCIÓN DE PETICIONES AUTENTICADAS (V2 - Cookies HttpOnly) ---
async function authFetch(url, options = {}) {
    // 1. Preparamos las opciones de la petición
    const fetchOptions = {
        ...options,
        // 🛡️ CRÍTICO: 'include' obliga al navegador a adjuntar la cookie 
        // automáticamente en cada petición, incluso si cambia de subdominio.
        credentials: 'include', 
        headers: {
            'Content-Type': 'application/json',
            ...(options.headers || {})
        }
    };

    try {
        // 2. Ejecutamos la petición al backend
        const response = await fetch(url, fetchOptions);

        // 3. Control de Seguridad Centralizado
        // Si el backend devuelve un 401 (Unauthorized), significa que la 
        // cookie expiró, fue alterada, o el usuario fue desactivado en la BD.
        if (response.status === 401) {
            console.warn("Acceso denegado o sesión expirada. Redirigiendo...");
            sessionStorage.clear(); // Limpiamos datos visuales (nombre, rol)
            window.location.href = '/'; // Expulsamos al usuario al login
            throw new Error('Sesión expirada.');
        }

        // 4. Si todo está OK, devolvemos la respuesta normal
        return response;

    } catch (error) {
        console.error("Error de red en authFetch:", error);
        throw error; // Propagamos el error para que la vista lo maneje (ej: mostrar un toast)
    }
}

let currentHospitalId = null;
let currentHistoryData = [];      // Datos de Infraestructura
let currentKpiHistoryData = [];   // Datos de Software/KPIs
let currentRangeHours = 24; 
let currentKpiRangeHours = 168;
let limitOfflineMinutes = 10; 

// Referencias a Gráficos
let myChart = null;        
let kpiRisChart = null;    
let kpiDonutChart = null;  

// Variables Mapa
let mapInstance = null;
let mapMarkers = null; 
let mapData = [];
let tourInterval = null;
let currentMapFilter = 'all';
let listaHospitalesCache = [];

let wsAlertas;

const HTML_MODAL_PDF_ORIGINAL = `
    <h3 style="margin-top:0; color:#2c3e50; border-bottom: 1px solid #eee; padding-bottom: 10px;">Configurar Reporte PDF</h3>
    
    <div style="background: #f8f9fa; padding: 10px 15px; border-radius: 8px; display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; font-size: 0.9em; border: 1px solid #e1e4e8;">
        <div>
            <strong style="color: #7f8c8d;">Hospital:</strong> 
            <span id="modal-pdf-hospital" style="color: #2c3e50; font-weight: bold; font-size: 1.1em;">---</span>
        </div>
        <div>
            <strong style="color: #7f8c8d;">Periodo:</strong> 
            <span id="modal-pdf-periodo" style="color: #2c3e50;">---</span>
        </div>
    </div>

    <div class="input-group" style="margin-bottom: 15px;">
        <label style="font-weight: 600; color: #7f8c8d; display: block; margin-bottom: 8px;">Tipo de Reporte</label>
        <div style="display: flex; flex-direction: column; gap: 8px;">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; background: #f8f9fa; padding: 10px 15px; border-radius: 6px; border: 1px solid #e1e4e8;">
                <input type="radio" name="pdf_tipo" id="pdf-type-clinico" value="clinico" checked style="width: 16px; height: 16px; accent-color: #e74c3c; cursor: pointer; margin: 0;" onchange="document.getElementById('pdf-scope-container').style.display='block'">
                <span style="font-weight: 600; color: #2c3e50; font-size: 0.9em;">📊 Reporte de Uso Clínico (RIS/PACS)</span>
            </label>
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; background: #f8f9fa; padding: 10px 15px; border-radius: 6px; border: 1px solid #e1e4e8;">
                <input type="radio" name="pdf_tipo" id="pdf-type-infra" value="infra" style="width: 16px; height: 16px; accent-color: #e74c3c; cursor: pointer; margin: 0;" onchange="document.getElementById('pdf-scope-container').style.display='none'">
                <span style="font-weight: 600; color: #2c3e50; font-size: 0.9em;">🖥️ Reporte de Salud de Infraestructura (IT)</span>
            </label>
        </div>
    </div>

    <div id="pdf-scope-container" class="input-group" style="margin-bottom: 15px;">
        <label style="font-weight: 600; color: #7f8c8d; display: block; margin-bottom: 8px;">Alcance del Reporte</label>
        <div style="display: flex; gap: 15px;">
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; background: #f8f9fa; padding: 10px 15px; border-radius: 6px; border: 1px solid #e1e4e8; flex: 1;">
                <input type="checkbox" id="pdf-scope-ris" checked style="width: 16px; height: 16px; accent-color: #e74c3c; cursor: pointer; margin: 0;">
                <span style="font-weight: 600; color: #2c3e50; font-size: 0.9em;">RIS (Órdenes e Informes)</span>
            </label>
            <label style="display: flex; align-items: center; gap: 8px; cursor: pointer; background: #f8f9fa; padding: 10px 15px; border-radius: 6px; border: 1px solid #e1e4e8; flex: 1;">
                <input type="checkbox" id="pdf-scope-pacs" checked style="width: 16px; height: 16px; accent-color: #e74c3c; cursor: pointer; margin: 0;">
                <span style="font-weight: 600; color: #2c3e50; font-size: 0.9em;">PACS (Imágenes)</span>
            </label>
        </div>
    </div>

    <div class="input-group" style="margin-bottom: 20px;">
        <label style="font-weight: 600; color: #7f8c8d;">ID de Tarea en Asana (Destino)</label>
        <input type="text" id="pdf-asana-task" placeholder="Ej: 1205248667269451" style="padding: 10px; border: 1px solid #ddd; border-radius: 6px; width: 100%; font-size: 1em; font-family: monospace;">
        <small style="color: #95a5a6; margin-top: 5px; display: block;">El PDF se adjuntará automáticamente a esta tarea.</small>
    </div>

    <div style="display:flex; gap:10px; margin-top:20px;">
        <button class="btn-action" onclick="cerrarModalPDF()" style="background:#95a5a6; width:auto; flex: 1;">Cancelar</button>
        <button class="btn-action" onclick="ejecutarGeneracionPDF()" style="flex: 2; background: linear-gradient(135deg, #e74c3c, #c0392b);">
            📄 Generar y Adjuntar
        </button>
    </div>
`;

// ==========================================
// --- INICIALIZACIÓN ---
// ==========================================
document.addEventListener('DOMContentLoaded', () => {
    // 1. Estado Sidebar
    const sidebarEstado = localStorage.getItem('sidebarEstado');
    if (sidebarEstado === 'cerrado') document.getElementById('sidebar').classList.add('collapsed');
    
    // 2. Recuperar Tema (Modo Oscuro)
    const temaGuardado = localStorage.getItem('temaUI');
    if (temaGuardado === 'oscuro') {
        document.body.classList.add('dark-theme');
        const icon = document.getElementById('theme-icon');
        const iconMob = document.getElementById('theme-icon-mobile');
        if(icon) icon.innerText = '☀️';
        if(iconMob) iconMob.innerText = '☀️';
        Chart.defaults.color = '#94a3b8';
    } else {
        Chart.defaults.color = '#666';
    }

    // Limpieza agresiva del buscador por si el navegador lo autocompleta
    const searchInput = document.getElementById('filter-hospital');
    if (searchInput) {
        searchInput.value = '';
        setTimeout(() => { searchInput.value = ''; }, 500);
    }

    // 3. Carga Inicial y Ruteo
    cargarConfiguracionGlobal().then(() => { 
        const params = new URLSearchParams(window.location.search);
        const hospId = params.get('hospital');
        if (hospId) verDetalle(hospId); else cargarDatos(); 
        
        // INICIALIZAR EL NUEVO MAPA DEL DASHBOARD
        initMapaDashboard();
    });

    cargarListaHospitalesIA();
    setTimeout(() => {
        cambiarTipoProcesamiento('pdf');
    }, 50);
    
    // 4. Intervalos de Refresco
    setInterval(() => { if(document.getElementById('view-dashboard').classList.contains('active')) cargarDatos(); }, 30000);
    setInterval(() => { if(document.getElementById('view-mapa').classList.contains('active')) cargarDatosMapa(); }, 60000);
    
    initWebSocket();
    chequearAlertasBackground();

    // 5. Filtros Listeners
    document.querySelectorAll('.dash-toggle-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.dash-toggle-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            aplicarFiltros(); 
        });
    });

    document.querySelectorAll('.map-toggle-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.map-toggle-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            currentMapFilter = this.getAttribute('data-map-status');
            renderizarMarcadores(); 
        });
    });

    // NUEVO: Listeners para el mapa del Resumen Ejecutivo
    document.querySelectorAll('.dash-map-toggle-btn').forEach(btn => {
        btn.addEventListener('click', function() {
            document.querySelectorAll('.dash-map-toggle-btn').forEach(b => b.classList.remove('active'));
            this.classList.add('active');
            currentDashMapFilter = this.getAttribute('data-map-status');
            renderizarMarcadoresDash(); 
        });
    });
});

// --- GESTIÓN DE ALERTAS EN SEGUNDO PLANO ---
async function chequearAlertasBackground() {
    try {
        const res = await authFetch('/api/alertas');
        const data = await res.json();
        const hayAlertasActivas = data.activas && data.activas.length > 0;
        actualizarBotonAlertas(hayAlertasActivas);
    } catch(e) {
        console.error("Error chequeando alertas en background:", e);
    }
}

function initWebSocket() {
    // Construimos la URL del WS dinámicamente según el entorno (http/https)
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    wsAlertas = new WebSocket(`${protocol}//${window.location.host}/ws/alertas`);
    
    wsAlertas.onmessage = function(event) {
        const data = JSON.parse(event.data);
        
        if (data.type === 'ALERTA_UPDATE') {
            console.log("Notificación push recibida: Actualizando alertas...");
            
            // 1. Actualizamos la campanita roja en el menú
            chequearAlertasBackground(); 
            
            // 2. Si el usuario está justo en la pestaña de alertas, recargamos la tabla para que vea el cambio
            if (document.getElementById('view-alertas').classList.contains('active')) {
                cargarAlertas();
            }
        }
    };
    
    wsAlertas.onclose = function(e) {
        console.log("WebSocket cerrado. Reconectando en 5 segundos...");
        setTimeout(initWebSocket, 5000); // Reconexión automática si se corta internet
    };
}

function actualizarBotonAlertas(activar) {
    const btnDesktop = document.getElementById('btn-alert');
    const btnMobile = document.getElementById('btn-alert-mobile'); 
    
    if (activar) {
        if(btnDesktop) btnDesktop.classList.add('nav-alert-active');
        if(btnMobile) btnMobile.classList.add('nav-alert-active');
    } else {
        if(btnDesktop) btnDesktop.classList.remove('nav-alert-active');
        if(btnMobile) btnMobile.classList.remove('nav-alert-active');
    }
}

// --- NAVEGACIÓN ---
function toggleSidebar() { 
    const sidebar = document.getElementById('sidebar');
    sidebar.classList.toggle('collapsed'); 
    
    // Guardamos la preferencia en el navegador
    if (sidebar.classList.contains('collapsed')) {
        localStorage.setItem('sidebarEstado', 'cerrado');
    } else {
        localStorage.setItem('sidebarEstado', 'abierto');
    }
}

// ==========================================
// --- SISTEMA DE NAVEGACIÓN ---
// ==========================================
function navegar(idVista, idBoton, btnMobile) {
    // 1. Visibilidad de Vistas
    document.querySelectorAll('.content').forEach(el => el.classList.remove('active'));
    const vistaDestino = document.getElementById(idVista);
    if (vistaDestino) vistaDestino.classList.add('active');

    // 2. Persistencia de URL
    const url = new URL(window.location);
    if (idVista === 'view-detalle' && currentHospitalId) url.searchParams.set('hospital', currentHospitalId);
    else url.searchParams.delete('hospital');
    window.history.pushState({}, '', url);

    // 3. Resaltar Botones
    if (idBoton) {
        document.querySelectorAll('.sidebar-right .nav-btn').forEach(btn => btn.classList.remove('active'));
        const btn = document.getElementById(idBoton);
        if(btn) btn.classList.add('active');
    }
    if (btnMobile) {
        document.querySelectorAll('.bottom-nav .nav-item').forEach(btn => btn.classList.remove('active'));
        btnMobile.classList.add('active');
    }

    // 4. Fixes específicos de redimensionamiento de mapas (Leaflet)
    if (idVista === 'view-mapa' && typeof mapInstance !== 'undefined' && mapInstance) {
        setTimeout(() => mapInstance.invalidateSize(), 200);
    }
    if (idVista === 'view-resumen' && typeof mapDashInstance !== 'undefined' && mapDashInstance) {
        setTimeout(() => mapDashInstance.invalidateSize(), 200);
    }
    
    // Otros fixes de vistas específicas
    if (idVista === 'view-ia') renderizarHistorial();

    // 5. APAGADO DE RECORRIDOS (Tours)
    // Detener el tour del mapa principal si salimos de su vista
    if (idVista !== 'view-mapa' && typeof tourInterval !== 'undefined' && tourInterval) {
        clearInterval(tourInterval);
        tourInterval = null;
        const btnTour = document.getElementById('btn-tour');
        if (btnTour) {
            btnTour.innerHTML = "▶ INICIAR RECORRIDO";
            btnTour.classList.remove('active');
        }
    }

    // Detener el tour del mapa del Dashboard si salimos de su vista
    if (idVista !== 'view-resumen' && typeof tourDashInterval !== 'undefined' && tourDashInterval) {
        clearInterval(tourDashInterval);
        tourDashInterval = null;
        const btnTourDash = document.getElementById('btn-tour-dash');
        if (btnTourDash) {
            btnTourDash.innerHTML = "▶ INICIAR RECORRIDO";
            btnTourDash.classList.remove('active');
        }
    }

    // 6. Auto-cierre de Sidebar en Mobile
    const sidebar = document.getElementById('sidebar');
    if (window.innerWidth <= 768 && sidebar) sidebar.classList.add('collapsed');
    
    window.scrollTo(0, 0);
}

function volverAlDashboard() {
    currentHospitalId = null; // Limpiamos el ID actual
    navegar('view-dashboard');
    cargarDatos(); // Refrescamos el dashboard
}

// --- CONFIGURACIÓN GLOBAL ---
async function cargarConfiguracionGlobal() {
    try {
        const res = await authFetch('/api/config');
        const data = await res.json();
        limitOfflineMinutes = parseInt(data.offline_minutes);
    } catch (e) { console.error(e); }
}

async function cargarConfigUI() {
    try {
        const res = await authFetch('/api/config');
        const data = await res.json();
        
        document.getElementById('conf-offline').value = data.offline_minutes;
        document.getElementById('conf-disk').value = data.disk_threshold;
        
        document.getElementById('conf-temp-amb').value = data.temp_amb_max;
        document.getElementById('conf-temp-cpu').value = data.temp_cpu_max;
        document.getElementById('conf-cpu-host').value = data.cpu_host_max; 
        document.getElementById('conf-ram-host').value = data.ram_host_max; 
        
        document.getElementById('conf-cpu-vm').value = data.cpu_vm_max; 
        document.getElementById('conf-ram-vm').value = data.ram_vm_max; 
        
        document.getElementById('check-fans').checked = data.enable_fans;
        document.getElementById('check-power').checked = data.enable_power;
        document.getElementById('check-raid').checked = data.enable_raid;
        
        listarHospitalesConfig();
    } catch (e) { console.error(e); }
}

async function guardarConfig() {
    const payload = {
        offline_minutes: parseInt(document.getElementById('conf-offline').value),
        disk_threshold: parseInt(document.getElementById('conf-disk').value),
        
        temp_amb_max: parseInt(document.getElementById('conf-temp-amb').value),
        temp_cpu_max: parseInt(document.getElementById('conf-temp-cpu').value),
        cpu_host_max: parseInt(document.getElementById('conf-cpu-host').value),
        ram_host_max: parseInt(document.getElementById('conf-ram-host').value),
        
        cpu_vm_max: parseInt(document.getElementById('conf-cpu-vm').value),
        ram_vm_max: parseInt(document.getElementById('conf-ram-vm').value),
        
        enable_fans: document.getElementById('check-fans').checked,
        enable_power: document.getElementById('check-power').checked,
        enable_raid: document.getElementById('check-raid').checked
    };
    try {
        await authFetch('/api/config', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload) });
        limitOfflineMinutes = payload.offline_minutes; 
        alert("✅ Configuración guardada correctamente");
    } catch (e) { alert("Error guardando configuración"); }
}

// --- DASHBOARD (MONITOR GLOBAL) ---
async function cargarDatos() {
    const tbody = document.getElementById('dashboard-body');
    
    // Mostramos la animación SOLO si la tabla está vacía (carga inicial)
    // Revisamos si ya existe alguna etiqueta de hospital renderizada
    if (tbody && !tbody.querySelector('.hospital-tag')) {
        tbody.innerHTML = `
            <tr>
                <td colspan="5" style="text-align: center; padding: 80px 0;">
                    <div style="display: flex; flex-direction: column; align-items: center; justify-content: center;">
                        <style>@keyframes spin-loader { 100% { transform: rotate(360deg); } }</style>
                        <svg width="50" height="50" viewBox="0 0 50 50" style="animation: spin-loader 1s linear infinite;">
                            <circle cx="25" cy="25" r="20" fill="none" stroke="#f1f2f6" stroke-width="5"></circle>
                            <circle cx="25" cy="25" r="20" fill="none" stroke="#3498db" stroke-width="5" stroke-dasharray="31.4 100" stroke-linecap="round"></circle>
                        </svg>
                        <span style="margin-top: 15px; color: #2c3e50; font-weight: 600; font-size: 1.1em;">
                            Sincronizando estado de los hospitales...
                        </span>
                        <span style="color: #7f8c8d; font-size: 0.9em; margin-top: 5px;">
                            Aguarde un momento mientras preparamos la informacion
                        </span>
                    </div>
                </td>
            </tr>
        `;
    }

    try {
        const response = await authFetch('/api/resumen-hospitales');
        const data = await response.json();
        
        // Una vez que llegan los datos, renderizarTabla borra el loader y dibuja los resultados
        renderizarTabla(data);
        
    } catch (error) { 
        console.error("Error datos:", error); 
        if (tbody) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 30px; color:#e74c3c; font-weight:bold;">⚠️ Error de conexión al obtener los datos de la red</td></tr>`;
        }
    }
}

function renderizarTabla(data) {
    const tbody = document.getElementById('dashboard-body');
    tbody.innerHTML = '';
    const ahora = new Date();

    actualizarResumenDashboard(data);

    data.forEach(h => {
        const diffMinutos = Math.floor((ahora - new Date(h.timestamp)) / 60000); 
        let fechaVisual = h.timestamp; 
        try { 
            const p = h.timestamp.split(' '); 
            if(p.length === 2) { 
                const [f,time] = p; 
                const [y,m,d] = f.split('-'); 
                fechaVisual = `${d}/${m}/${y} ${time.substring(0,5)}`; 
            } 
        } catch(e){}

        // --- LÓGICA ESTRICTA BINARIA (Solo tiempo) ---
        let estadoClass = '', estadoTexto = '', rowColorClass = '';
        
        // Si el reporte entró dentro del tiempo permitido (y no es NaN por un error de fecha)
        if (!isNaN(diffMinutos) && diffMinutos <= limitOfflineMinutes) { 
            estadoClass = 'status-online'; 
            estadoTexto = 'Online'; 
            rowColorClass = 'row-status-success';
        } else {
            // Si superó el tiempo (ej: más de 10 min) o la fecha es inválida (NaN)
            estadoClass = 'status-offline'; 
            estadoTexto = 'Offline'; 
            rowColorClass = 'row-status-danger';
        }
        // ---------------------------------------------

        let etiquetasHtml = '';
        if (h.elements && Array.isArray(h.elements)) {
            h.elements.forEach(elem => {
                let c = 'status-offline';
                if (elem.state === 'success' || elem.is_ok === true) c = 'status-online';
                if (elem.state === 'warning') c = 'status-warning';
                etiquetasHtml += `<span class="status-badge ${c}" style="font-size:0.75em; margin-right:5px; margin-bottom:4px; padding:2px 8px; display:inline-block;">${elem.label}</span>`;
            });
        }

        // Textos y colores seguros por si diffMinutos es NaN
        const textoHaceMinutos = isNaN(diffMinutos) ? 'Sin conexión' : `hace ${diffMinutos} min`;
        const colorHaceMinutos = (isNaN(diffMinutos) || diffMinutos > limitOfflineMinutes) ? '#dc3545' : '#7f8c8d';

        const tr = document.createElement('tr');
        tr.className = rowColorClass;
        tr.onclick = () => verDetalle(h.raw_id);
        
        tr.innerHTML = `
            <td>
                <span class="hospital-tag">${h.id}</span>
                <div style="margin-top: 5px; font-size: 0.8em; color: #6c757d; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 150px;">
                    ${h.name}
                </div>
            </td>
            <td><span class="status-badge ${estadoClass}">${estadoTexto}</span></td>
            <td>${etiquetasHtml}</td>
            <td>
                <span style="color: #2c3e50; font-weight: 600;">${fechaVisual}</span><br>
                <small style="color: ${colorHaceMinutos}; font-weight: 500;">${textoHaceMinutos}</small>
            </td>
            <td style="text-align: right;">
                <svg class="chevron-icon" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                    <polyline points="9 18 15 12 9 6"></polyline>
                </svg>
            </td>
        `;
        tbody.appendChild(tr);
    });
    
    aplicarFiltros();
}

// --- DASHBOARD (VISTA) ---
function aplicarFiltros() {
    const inputTexto = document.getElementById('filter-hospital');
    const texto = inputTexto ? inputTexto.value.toLowerCase().trim() : '';
    
    // CORRECCIÓN: Buscar específicamente el botón activo del DASHBOARD
    const btnActivo = document.querySelector('.dash-toggle-btn.active');
    const estadoFiltro = btnActivo ? btnActivo.getAttribute('data-status') : 'all'; 

    const filas = document.querySelectorAll('#dashboard-body tr');

    filas.forEach(fila => {
        const celdaID = fila.cells[0].textContent.toLowerCase(); 
        const badge = fila.querySelector('.status-badge'); 
        const estadoTexto = badge ? badge.textContent.trim().toLowerCase() : '';

        const cumpleTexto = celdaID.includes(texto);
        
        let cumpleEstado = false;
        if (estadoFiltro === 'all') {
            cumpleEstado = true;
        } else if (estadoFiltro === 'online') {
            cumpleEstado = estadoTexto === 'online' || estadoTexto === 'parcial' || estadoTexto === 'solo vms';
        } else if (estadoFiltro === 'offline') {
            cumpleEstado = estadoTexto === 'offline' || estadoTexto === 'warning';
        }

        fila.style.display = (cumpleTexto && cumpleEstado) ? '' : 'none';
    });
}

// Opcional pero recomendado: que el buscador de texto filtre en tiempo real al escribir
document.getElementById('filter-hospital')?.addEventListener('input', aplicarFiltros);

// --- VISTA DETALLE (CORREGIDA V4) ---
async function verDetalle(hospitalId) {
    currentHospitalId = hospitalId;
    currentHistoryData = [];
    currentRangeHours = 24; 
    currentKpiRangeHours = 24; // Reset del rango global de software a 24H

    // Resetear Software a Total (0) al entrar a un nuevo hospital
    currentSoftwareMinutes = 0;
    document.querySelectorAll('.sw-time-btn').forEach(b => b.classList.remove('active'));
    
    // Seleccionamos el último botón (Total)
    const botones = document.querySelectorAll('.sw-time-btn');
    const btnTotal = botones[botones.length - 1]; 
    if(btnTotal) btnTotal.classList.add('active');
    
    // Auto-colapsar barra lateral al ver detalles (si estás en PC)
    if (window.innerWidth > 768) { 
        const sidebar = document.getElementById('sidebar');
        if (sidebar && !sidebar.classList.contains('collapsed')) {
            sidebar.classList.add('collapsed');
            localStorage.setItem('sidebarEstado', 'cerrado');
        }
    }

    document.getElementById('top-cards-container').innerHTML = ""; 
    document.getElementById('vms-container').innerHTML = "";
    
    // 1. Limpiamos absolutamente todos los botones primero
    document.querySelectorAll('.chart-btn').forEach(b => b.classList.remove('active'));
    
    // 2. Activamos el botón de 24H de Infraestructura
    const btn24Infra = document.querySelector('#tab-infra .chart-btn') || document.querySelector('.chart-btn'); 
    if(btn24Infra) btn24Infra.classList.add('active');

    // 3. Activamos el botón Global de Tiempo para KPIs (el primero: 24H)
    const btn24KpiGlobal = document.querySelector('.kpi-global-time-btn');
    if(btn24KpiGlobal) btn24KpiGlobal.classList.add('active');

    // 4. Forzamos botones y LIMPIAMOS LA MEMORIA del gráfico de Evolución Temporal
    const btnRis = document.getElementById('btn-modo-ris');
    if(btnRis) btnRis.classList.add('active');
    const inputKpiModo = document.getElementById('kpi-modo');
    if(inputKpiModo) inputKpiModo.value = 'ris'; // <--- FIX: Memoria reseteada

    // Forzamos el botón Total por defecto
    const btnTotalKpi = document.getElementById('btn-agrup-total');
    if(btnTotalKpi) btnTotalKpi.classList.add('active');
    const inputKpiAgrup = document.getElementById('kpi-agrupacion');
    if(inputKpiAgrup) inputKpiAgrup.value = 'total'; // <--- FIX: Memoria reseteada
    
    // 5. Forzamos botones y LIMPIAMOS LA MEMORIA de la Dona
    const btnDonutRis = document.querySelector('.donut-modo-btn'); 
    if(btnDonutRis) btnDonutRis.classList.add('active');
    const inputDonutModo = document.getElementById('donut-modo');
    if(inputDonutModo) inputDonutModo.value = 'ris'; // <--- FIX: Memoria reseteada

    const btnDonutEquipos = document.querySelector('.donut-agrup-btn'); 
    if(btnDonutEquipos) btnDonutEquipos.classList.add('active');
    const inputDonutAgrup = document.getElementById('donut-agrupacion');
    if(inputDonutAgrup) inputDonutAgrup.value = 'equipo'; // <--- FIX: Memoria reseteada

    const selSource = document.getElementById('chart-source');
    if(selSource) { selSource.innerHTML = '<option value="global">Host Físico (Global)</option>'; selSource.value = 'global'; }
    actualizarOpcionesMetricas();

    navegar('view-detalle');

    try {
        const response = await authFetch(`/api/hospital/${hospitalId}`);
        if (currentHospitalId !== hospitalId) return; 
        const data = await response.json();
        if (data.error) { alert(data.error); return; }

        let nombreReal = "Hospital Desconocido";
        try {
            const metaRes = await authFetch('/api/hospitales-metadata');
            const metaList = await metaRes.json();
            const metaObj = metaList.find(h => h.hospital_id === hospitalId);
            if (metaObj && metaObj.nombre) nombreReal = metaObj.nombre;
        } catch (err) { console.warn("Error obteniendo metadata", err); }
        
        data.nombre_real = nombreReal;
        renderizarDetalle(data, hospitalId);
        
        // UNA SOLA LLAMADA PARA TODO EL SOFTWARE (Solicita 24hs por defecto)
        await Promise.all([
            cargarHistorial(24, hospitalId),
            cargarHistorialKpiGlobal(168, hospitalId),
            cargarEstadoSoftware(hospitalId) // <--- ESTA ES LA LÍNEA NUEVA
        ]);
    } catch (e) { console.error("Error detalle:", e); }
}

// --- RENDERIZAR DETALLE (UNIFICADA Y CORREGIDA V4) ---
function renderizarDetalle(data, id) {
    // 1. POBLAR TARJETA SUPERIOR
    let fechaVisual = data.db_timestamp || new Date().toLocaleString();
    try { 
        const p = data.db_timestamp.split(' '); 
        if(p.length === 2) { 
            const [f, h] = p; 
            const [a, m, d] = f.split('-'); 
            fechaVisual = `${d}/${m}/${a} ${h}`; 
        } 
    } catch(e){}

    let nombreHospital = data.nombre_real || data.name || data.nombre || "Hospital Desconocido";

    const elRow1 = document.getElementById('hosp-card-row1');
    const elRow2 = document.getElementById('hosp-card-row2');
    if(elRow1) elRow1.innerHTML = `<span class="hospital-tag" style="background:white; margin-right:5px; padding: 2px 6px; border-radius: 4px; border: 1px solid #ccc;">${id}</span> ${nombreHospital}`;
    if(elRow2) elRow2.innerText = `${fechaVisual}`;

    // 2. ADAPTADOR DE DATOS (Normaliza V2 -> V3/V4)
    const phy = data.physical_layer || data.physical_host || {};
    const hostInfo = phy.host_info || phy || {}; 
    const sensors = phy.sensors || (data.environment ? data.environment.thermal : {}) || {};
    const power = (phy.sensors ? phy.sensors.power : null) || (data.environment ? data.environment.power : {}) || {};
    const vmsRaw = data.virtual_layer || []; 
    
    let vmsList = [];
    if (Array.isArray(vmsRaw)) {
        vmsList = vmsRaw; 
    } else if (data.vms) {
        Object.entries(data.vms).forEach(([k, v]) => {
            const metricas = v.metrics || {};
            let storageV3 = [];
            if (metricas.discos) {
                Object.entries(metricas.discos).forEach(([mnt, dsk]) => {
                    storageV3.push({
                        mount_point: mnt,
                        free_gb: dsk.libre_gb,
                        usage_percent: dsk.percent_used,
                        performance: { latency_ms: dsk.latency_ms || 0, status: dsk.latency_status || "OK" }
                    });
                });
            }
            let servicesV3 = [];
            if (metricas.servicios) {
                Object.entries(metricas.servicios).forEach(([sName, sData]) => {
                    let sState = typeof sData === 'string' ? sData : sData.status;
                    let sVitals = typeof sData === 'object' ? sData : null;
                    servicesV3.push({ name: sName, state: sState, vital_signs: sVitals });
                });
            }
            vmsList.push({
                id: k,
                type: 'vm',
                state: v.status,
                telemetry: {
                    cpu: { usage_percent: metricas.cpu_load_percent },
                    ram: { usage_percent: metricas.ram ? metricas.ram.percent : 0 },
                    uptime_seconds: metricas.uptime_seconds
                },
                storage: storageV3,
                application_layer: { services: servicesV3 }
            });
        });
    }

    // --- ICONO CHEVRON ---
    const chevronSvg = `<svg class="card-chevron" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="3" style="transition: transform 0.3s ease; margin-left:10px;"><polyline points="6 9 12 15 18 9"></polyline></svg>`;

    // 3. RENDERIZADO DEL HOST
    const telemetry = phy.telemetry || {};
    const cpuVal = telemetry.cpu && telemetry.cpu.usage_percent !== undefined ? telemetry.cpu.usage_percent : (phy.cpu_usage_percent || 0);
    const ramObj = telemetry.ram || {};
    const ramUsed = (ramObj.used_gb || phy.ram_usage_gb || 0).toFixed(1);
    const ramTotal = (ramObj.total_gb || phy.ram_total_gb || 1).toFixed(1);
    
    const hostUptimeRaw = hostInfo.uptime_seconds || phy.uptime_seconds;
    const hostUptimeStr = typeof formatearUptime === 'function' ? formatearUptime(hostUptimeRaw) : "0m";
    const uptimeDisplay = hostUptimeStr ? `⏱ ${hostUptimeStr}` : '-';

    let fansHtml = '<div style="margin-top:15px; color:#ccc; font-style:italic; font-size:0.85em;">Sin datos</div>';
    const fansList = sensors.fans || [];
    if (fansList.length > 0) {
        fansHtml = '<div class="fans-grid">';
        fansList.forEach(f => {
            const isOk = f.status === 'OK';
            const statusClass = isOk ? 'ok' : 'fail';
            fansHtml += `<div class="fan-chip ${statusClass}"><span class="fan-name">${f.name}</span><span class="fan-rpm">${f.value || f.speed_rpm} ${f.unit || 'RPM'}</span></div>`;
        });
        fansHtml += '</div>';
    }

    const cardHost = `
        <div class="detail-card">
            <div class="detail-card-header" style="background: #3498db; cursor:pointer;" onclick="toggleCard(this.parentElement)">
                <span>Host Físico (${hostInfo.type || 'Server'})</span>
                <div style="display:flex; align-items:center;">
                    <span style="opacity:0.8">${hostInfo.model || 'Modelo Desc.'}</span>
                    ${chevronSvg}
                </div>
            </div>
            <div class="detail-card-body">
                <div class="grid-2-col">
                    <div class="input-group"><label>Uso CPU</label><input type="text" readonly value="${cpuVal} %" style="font-weight:bold; color:#2c3e50;"></div>
                    <div class="input-group"><label>RAM (GB)</label><input type="text" readonly value="${ramUsed} / ${ramTotal}"></div>
                </div>
                <div class="input-group" style="margin-top:10px;"><label>Tiempo de Actividad</label><input type="text" readonly value="${uptimeDisplay}" style="font-family:monospace; font-weight:bold; color:#27ae60;"></div>
                <label class="sensor-section-title">VENTILADORES</label>${fansHtml}
            </div>
        </div>`;

    let tempsHtml = '';
    const tempsList = sensors.temperatures || (sensors.cpu_temps || []);
    if (tempsList.length > 0) {
        tempsHtml = '<div class="sensor-grid">';
        tempsList.forEach(t => { 
            const val = t.value !== undefined ? t.value : t.temp_c;
            const name = t.name || t.sensor;
            tempsHtml += `<div class="sensor-chip ok"><span class="sensor-label">${name}</span> <span class="sensor-value">${val}°C</span></div>`; 
        });
        tempsHtml += '</div>';
    }

    let psuHtml = '';
    const suppliesList = power.supplies || power.power_supplies || [];
    if (suppliesList.length > 0) {
        psuHtml = '<div class="sensor-section-title">FUENTES DE PODER</div><div class="sensor-grid">';
        suppliesList.forEach(ps => {
            const isOk = ps.status === 'OK'; const sClass = isOk ? 'ok' : 'fail';
            const val = ps.watts !== undefined ? ps.watts : ps.output_watts;
            psuHtml += `<div class="sensor-chip ${sClass}"><span class="sensor-label">${ps.name}</span> <span class="sensor-value">${val}W</span></div>`;
        });
        psuHtml += '</div>';
    }

    const wattsCurrent = power.watts_current !== undefined ? power.watts_current : (power.watts_consumed || 0);
    const cardEnv = `
        <div class="detail-card">
            <div class="detail-card-header" style="background: #27ae60; cursor:pointer;" onclick="toggleCard(this.parentElement)">
                <span>Ambiente & Energía</span>
                <div style="display:flex; align-items:center;">
                    <span style="opacity:0.8">Sensores</span>
                    ${chevronSvg}
                </div>
            </div>
            <div class="detail-card-body">
                <div class="grid-2-col">
                    <div class="input-group"><label>Consumo Total</label><input type="text" readonly value="${wattsCurrent} Watts"></div>
                    <div class="input-group"><label>Estado Sensores</label><input type="text" readonly value="${sensors.status || 'OK'}"></div>
                </div>
                ${tempsHtml ? '<div class="sensor-section-title">TEMPERATURAS</div>' + tempsHtml : ''}
                ${psuHtml}
            </div>
        </div>`;

    let cardRaid = '';
    const storageLayer = phy.storage_layer || data.storage_layer || null; 
    if (storageLayer && ((storageLayer.logical_volumes && storageLayer.logical_volumes.length > 0) || (storageLayer.physical_drives && storageLayer.physical_drives.length > 0))) {
        let volsHtml = '';
        if (storageLayer.logical_volumes && storageLayer.logical_volumes.length > 0) {
            volsHtml = storageLayer.logical_volumes.map(v => {
                const isOk = ['OK', 'Online'].includes(v.status);
                const color = isOk ? '#27ae60' : '#e74c3c';
                const bg = isOk ? '#eafaf1' : '#fadbd8';
                const sizeStr = v.size_gb >= 1000 ? (v.size_gb / 1024).toFixed(1) + ' TB' : Math.round(v.size_gb) + ' GB';
                return `<div style="display:flex; justify-content:space-between; align-items:center; padding: 8px 0; border-bottom: 1px solid #f1f1f1;"><div style="display:flex; align-items:center; gap:8px;"><span style="font-size:1.2em;">💿</span><div><div style="color:#2c3e50; font-weight:bold; font-size:0.95em;">${v.name}</div><div style="font-size:0.75em; color:#7f8c8d;">${v.raid_level || 'Unknown'}</div></div></div><div style="text-align:right;"><div style="font-weight:bold; font-size:0.95em; color:#2c3e50;">${sizeStr}</div><span style="color:${color}; font-weight:bold; font-size:0.8em; background:${bg}; padding:2px 6px; border-radius:4px;">${v.status || 'N/A'}</span></div></div>`;
            }).join('');
        }
        let disksHtml = '';
        if (storageLayer.physical_drives && storageLayer.physical_drives.length > 0) {
            disksHtml = '<div style="display:flex; flex-wrap:wrap; gap:6px; margin-top:10px;">';
            storageLayer.physical_drives.forEach(d => {
                const isOk = ['OK', 'Online'].includes(d.status);
                const color = isOk ? '#27ae60' : '#e74c3c';
                const bg = isOk ? '#f0fcf4' : '#fdedec';
                let slotNum = "N/A";
                const match = (d.slot || "").match(/Bay\.(\d+)/i);
                if (match) slotNum = match[1]; else slotNum = (d.slot || "").split(':')[0]; 
                disksHtml += `<div title="Slot: ${d.slot}\nModelo: ${d.model}\nTamaño: ${Math.round(d.size_gb)} GB\nEstado: ${d.status}" style="background:${bg}; border-left: 3px solid ${color}; padding: 4px 8px; border-radius: 4px; font-size: 0.85em; color:#2c3e50; font-weight:600; display:flex; align-items:center; gap:5px; cursor:help;"><span>Bay ${slotNum}</span> <span style="opacity:0.6; font-weight:normal; font-size:0.9em;">${d.media_type || 'HDD'}</span></div>`;
            });
            disksHtml += '</div>';
        }

        cardRaid = `<div class="detail-card" style="grid-column: 1 / -1; margin-top: 10px;">
            <div class="detail-card-header" style="background: #8e44ad; cursor:pointer; display:flex; justify-content:space-between; align-items:center;" onclick="toggleCard(this.parentElement)">
                <span>Almacenamiento Físico (RAID)</span>
                <div style="display:flex; align-items:center;">
                    <span style="opacity:0.8">Controladoras & Discos</span>
                    ${chevronSvg}
                </div>
            </div>
            <div class="detail-card-body">
                <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px;">
                    <div><label class="sensor-section-title" style="color:#8e44ad; border-bottom:1px solid #e1b1ff;">VOLÚMENES LÓGICOS</label><div style="margin-top:10px;">${volsHtml || '<div style="color:#7f8c8d; font-size:0.85em; font-style:italic;">Sin volúmenes configurados</div>'}</div></div>
                    <div><label class="sensor-section-title" style="color:#8e44ad; border-bottom:1px solid #e1b1ff;">DISCOS FÍSICOS (${(storageLayer.physical_drives || []).length})</label>${disksHtml || '<div style="color:#7f8c8d; font-size:0.85em; font-style:italic;">Sin discos físicos reportados</div>'}</div>
                </div>
            </div>
        </div>`;
    }

    document.getElementById('top-cards-container').innerHTML = cardHost + cardEnv + cardRaid;

    // 4. RENDERIZADO DE VMs (Y EQUIPOS) - VERSIÓN BLINDADA CONTRA "OFFLINE"
    const vCont = document.getElementById('vms-container');
    if(vCont) vCont.innerHTML = '';

    vmsList.forEach(vm => {
        // SEGURIDAD: Si telemetry es nulo o vacío (pasa cuando está Offline), asignamos {}
        const m = vm.telemetry || {}; 
        const rawState = vm.state || 'Unknown';
        const isOnline = ['online', 'running'].includes(rawState.toLowerCase());
        const headerColor = isOnline ? '#27ae60' : '#c0392b';
        
        // Formateo seguro de Uptime
        const vmUptimeStr = typeof formatearUptime === 'function' && m.uptime_seconds ? formatearUptime(m.uptime_seconds) : null;
        const uptimeBadge = vmUptimeStr ? `<span style="background:rgba(0,0,0,0.2); padding:2px 8px; border-radius:4px; font-size:0.8em; margin-right:8px; display:inline-flex; align-items:center; gap:4px;">⏱ ${vmUptimeStr}</span>` : '';

        // --- LÓGICA DE ICONOS A PRUEBA DE BALAS ---
        const tipoRaw = (vm.type || '').toLowerCase().trim();
        const idNombre = (vm.id || '').toUpperCase();
        
        let iconoTipo = '📦'; // Default
        
        if (tipoRaw === 'vm') {
            iconoTipo = '🖥️'; // Máquina Virtual
        } else if (tipoRaw === 'eq' || idNombre.includes('RX') || idNombre.includes('CR') || idNombre.includes('MAMO')) {
            iconoTipo = '🩻'; // Equipo Médico
        }

        // Render Seguro de Discos
        let disksHtml = (vm.storage || []).map(disk => {
            const w = disk.usage_percent || 0; 
            const color = w > 90 ? '#e74c3c' : '#3498db';
            let latHtml = disk.performance?.latency_ms !== undefined ? `<span style="font-size:0.75em; ${disk.performance.latency_ms > 20 ? 'color:#e74c3c; font-weight:bold;' : 'color:#f39c12;'} margin-left:8px;" title="Latencia">⚡ ${disk.performance.latency_ms.toFixed(1)}ms</span>` : '';
            return `<div class="disk-row"><div class="disk-info"><span>${disk.mount_point}</span><span>${(disk.free_gb||0).toFixed(1)} GB Libres${latHtml}</span></div><div class="progress-track"><div class="progress-fill" style="width:${w}%; background:${color};"></div></div></div>`;
        }).join('');

        // Render Seguro de Servicios
        let servicesHtml = (vm.application_layer?.services || []).map(svc => {
            const isRun = (svc.state || '').toString().toLowerCase() === 'running';
            const tooltipText = svc.vital_signs ? `${svc.name}\nPID: ${svc.vital_signs.pid}\nCPU: ${svc.vital_signs.cpu_percent}%` : svc.name;
            return `<span class="service-chip ${isRun?'running':'stopped'}" title="${tooltipText}">${isRun?'●':'✖'} ${svc.name}</span>`;
        }).join('');

        // EXTRACCIÓN SEGURA DE CPU/RAM CON FALLBACK PARA EQUIPOS APAGADOS
        const cpuText = m.cpu && m.cpu.usage_percent !== undefined ? `${m.cpu.usage_percent}%` : 'N/A';
        const ramText = m.ram && m.ram.usage_percent !== undefined ? `${m.ram.usage_percent}%` : 'N/A';

        const card = document.createElement('div');
        card.className = 'vm-card'; 
        card.innerHTML = `
            <div class="vm-header" style="background:${headerColor}; padding:10px 15px; color:white; display:flex; justify-content:space-between; align-items:center; font-weight:bold; cursor:pointer;" onclick="toggleCard(this.parentElement)">
                <div style="display:flex; align-items:center; gap:8px;">
                    <span style="font-size:1.2em;">${iconoTipo}</span>
                    <span>${vm.id}</span>
                    ${chevronSvg}
                </div>
                <div style="display:flex; align-items:center;">${uptimeBadge}<span style="background:rgba(255,255,255,0.2); padding:2px 8px; border-radius:10px; font-size:0.8em;">${vm.state}</span></div>
            </div>
            <div class="vm-body" style="padding:15px;">
                <div style="display:flex; gap:10px; margin-bottom:15px;">
                    <div style="flex:1"><label style="font-size:0.75em; color:#7f8c8d;">CPU</label><div style="font-weight:bold; color:#2c3e50;">${cpuText}</div></div>
                    <div style="flex:1"><label style="font-size:0.75em; color:#7f8c8d;">RAM</label><div style="font-weight:bold; color:#2c3e50;">${ramText}</div></div>
                </div>
                <div style="margin-bottom:15px;"><label class="sensor-section-title">ALMACENAMIENTO & LATENCIA</label>${disksHtml || '<small style="color:#ccc; font-style:italic;">Sin datos</small>'}</div>
                <div><label class="sensor-section-title">SERVICIOS</label><div style="display:flex; flex-wrap:wrap;">${servicesHtml || '<small style="color:#ccc; font-style:italic;">Sin servicios</small>'}</div></div>
            </div>`;
        vCont.appendChild(card);
    });
}

// --- FUNCIÓN AUXILIAR: Formatear Segundos a Días/Horas ---
function formatearUptime(segundos) {
    if (!segundos || segundos <= 0) return null;
    const dias = Math.floor(segundos / 86400);
    const horas = Math.floor((segundos % 86400) / 3600);
    const minutos = Math.floor((segundos % 3600) / 60);
    
    if (dias > 0) return `${dias}d ${horas}h`;
    if (horas > 0) return `${horas}h ${minutos}m`;
    return `${minutos}m`;
}

function toggleCard(el) {
    el.classList.toggle('collapsed');
}

// --- GRÁFICOS ---
function llenarSelectores() {
    const sourceSelect = document.getElementById('chart-source');
    if(!sourceSelect) return;
    
    const prevVal = sourceSelect.value;
    sourceSelect.innerHTML = '<option value="global">Host Físico (Global)</option>';
    
    if (currentHistoryData.length > 0) {
        const lastRecord = currentHistoryData[currentHistoryData.length - 1];
        Object.keys(lastRecord.vms || {}).forEach(vm => {
            sourceSelect.add(new Option(`VM: ${vm}`, vm));
        });
    }
    
    if ([...sourceSelect.options].some(o => o.value === prevVal)) {
        sourceSelect.value = prevVal;
    } else {
        sourceSelect.value = 'global';
    }
}

function actualizarOpcionesMetricas() {
    const source = document.getElementById('chart-source').value;
    const metricSelect = document.getElementById('chart-metric');
    if(!metricSelect) return;
    
    const prevMetric = metricSelect.value;
    metricSelect.innerHTML = ''; 
    
    if (source === 'global') {
        metricSelect.add(new Option('Uso CPU Host (%)', 'cpu_host'));
        metricSelect.add(new Option('Temperaturas (Ambiente + CPUs)', 'thermal_combined'));
    } else {
        metricSelect.add(new Option('Rendimiento (CPU + RAM)', 'performance'));
    }
    
    if ([...metricSelect.options].some(o => o.value === prevMetric)) {
        metricSelect.value = prevMetric;
    } else {
        metricSelect.selectedIndex = 0;
    }
}

const elSource = document.getElementById('chart-source');
if(elSource) elSource.addEventListener('change', () => { actualizarOpcionesMetricas(); actualizarGrafico(); });
const elMetric = document.getElementById('chart-metric');
if(elMetric) elMetric.addEventListener('change', actualizarGrafico);

const elEquipoRis = document.getElementById('kpi-ris-equipo');
if(elEquipoRis) elEquipoRis.addEventListener('change', actualizarGraficoKpiTemporal);

function cambiarRango(horas, btn) {
    document.querySelectorAll('.chart-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentRangeHours = horas; 
    cargarHistorial(horas, currentHospitalId);
}

async function cargarHistorial(horas, idSolicitado) {
    if (currentHospitalId !== idSolicitado) return;
    const loader = document.getElementById('chart-loader');
    if(loader) loader.style.display = 'flex';
    try {
        const response = await authFetch(`/api/hospital/${idSolicitado}/history?horas=${horas}`);
        if (currentHospitalId !== idSolicitado) return;
        currentHistoryData = await response.json();
        
        // Gráficos de Infraestructura
        llenarSelectores();          
        actualizarOpcionesMetricas(); 
        actualizarGrafico();          

        // --- NUEVO V4: Gráficos de KPIs de Software ---
        //llenarSelectoresKpi();
        //actualizarGraficoKpiRis();

    } catch (e) { console.error("Error historial:", e); } 
    finally { if (currentHospitalId === idSolicitado && loader) loader.style.display = 'none'; }
}

function actualizarGrafico() {
    const canvas = document.getElementById('historyChart');
    if(!canvas || !currentHistoryData || currentHistoryData.length === 0) return;
    const ctx = canvas.getContext('2d');
    const source = document.getElementById('chart-source').value;
    const metric = document.getElementById('chart-metric').value;

    const labels = currentHistoryData.map(d => {
        const dateObj = new Date(d.timestamp);
        const timeStr = dateObj.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
        return currentRangeHours > 24 ? `${dateObj.toLocaleDateString([], {day:'2-digit', month:'2-digit'})} ${timeStr}` : timeStr;
    });

    function getSafeVMData(d, sourceId) {
        let vm = (d.vms && d.vms[sourceId]) || (d.virtual_layer && d.virtual_layer.find(v => v.id === sourceId));
        if (!vm) return { valid: false, cpu: null, ram: null };
        let cpu = vm.telemetry?.cpu?.usage_percent ?? vm.cpu;
        let ram = vm.telemetry?.ram?.usage_percent ?? vm.ram;
        if ((vm.state || '').toLowerCase() === 'offline' || (cpu === 0 && ram === 0)) return { valid: false, cpu: null, ram: null };
        return { valid: true, cpu, ram };
    }

    // 1. DETECTAR HUECOS
    let gapRanges = [], inGap = false, gapStart = 0;
    currentHistoryData.forEach((d, i) => {
        let hasData = false;

        // RESTAURADO: Lógica correcta para validar si hay datos de temperatura
        if (source === 'global') {
            if (metric === 'thermal_combined') {
                hasData = d.global && (d.global.temp_amb != null || (d.global.cpu_sensors && Object.keys(d.global.cpu_sensors).length > 0));
            } else {
                hasData = d.global && d.global[metric] != null;
            }
        } else {
            hasData = getSafeVMData(d, source).valid;
        }

        if (!hasData) { if (!inGap) { inGap = true; gapStart = i; } } 
        else { if (inGap) { inGap = false; gapRanges.push({ start: gapStart, end: i }); } }
    });
    if (inGap) gapRanges.push({ start: gapStart, end: currentHistoryData.length - 1 });

    // 2. CONFIGURAR DATASETS
    let datasets = [];
    const common = { borderWidth: 2, tension: 0.3, pointRadius: 0, spanGaps: false };

    // RESTAURADO: El bloque que lee los sensores y arma las líneas de temperatura
    if (metric === 'thermal_combined' && source === 'global') {
        const envData = currentHistoryData.map(d => (d.global && d.global.temp_amb != null) ? d.global.temp_amb : null);
        
        // Solo agrega la línea de ambiente si hay datos reales
        if (envData.some(val => val !== null)) {
            datasets.push({ ...common, label: 'Ambiente (°C)', data: envData, borderColor: '#2ecc71' });
        }
        
        const sensors = new Set();
        currentHistoryData.forEach(d => { if(d.global && d.global.cpu_sensors) Object.keys(d.global.cpu_sensors).forEach(k => sensors.add(k)); });
        
        let ci = 0; const colors = ['#e74c3c', '#e67e22', '#d35400', '#8e44ad', '#c0392b'];
        sensors.forEach(s => {
            const sData = currentHistoryData.map(d => (d.global && d.global.cpu_sensors && d.global.cpu_sensors[s] != null) ? d.global.cpu_sensors[s] : null);
            datasets.push({ ...common, label: s, data: sData, borderColor: colors[ci++%colors.length], borderDash: [5,5] });
        });

    } else if (metric === 'performance') {
        datasets.push({ ...common, label: 'CPU (%)', data: currentHistoryData.map(d => getSafeVMData(d, source).cpu), borderColor: '#3498db' });
        datasets.push({ ...common, label: 'RAM (%)', data: currentHistoryData.map(d => getSafeVMData(d, source).ram), borderColor: '#9b59b6' });
    } else {
        datasets.push({ ...common, label: 'Uso CPU Host (%)', data: currentHistoryData.map(d => (d.global && d.global[metric] != null) ? d.global[metric] : null), borderColor: '#3498db', fill: true, backgroundColor: 'rgba(52,152,219,0.05)' });
    }

    // 3. PLUGIN DE SOMBREADO (ACTUALIZADO: GRIS, BORDE Y AJUSTE DE LÍMITES)
    const gapShadingPlugin = {
        id: 'gapShadingPlugin',
        beforeDraw: chart => {
            if (gapRanges.length === 0) return;
            const { ctx, chartArea, scales: { x } } = chart;
            if (!chartArea || !x) return;
            
            ctx.save();
            const isDark = document.body.classList.contains('dark-theme');
            
            // Colores Gris neutro para el fondo, el borde y el texto
            const bgColor = isDark ? 'rgba(149, 165, 166, 0.12)' : 'rgba(189, 195, 199, 0.25)';
            const borderColor = isDark ? 'rgba(149, 165, 166, 0.4)' : 'rgba(149, 165, 166, 0.8)';
            const textColor = isDark ? 'rgba(189, 195, 199, 0.8)' : 'rgba(127, 140, 141, 0.9)';
            
            gapRanges.forEach(gap => {
                // Ajuste de límites: inicia y termina exactamente en los puntos vacíos (evita solapar la línea azul)
                let sX = gap.start === 0 ? chartArea.left : x.getPixelForValue(gap.start);
                let eX = gap.end === currentHistoryData.length - 1 ? chartArea.right : x.getPixelForValue(gap.end);
                
                if (isNaN(sX)) sX = chartArea.left;
                if (isNaN(eX)) eX = chartArea.right;
                
                let width = eX - sX;
                
                // Si el hueco es de un solo registro, le damos un ancho visual mínimo centrado
                if (width < 5 && gap.start !== 0 && gap.end !== currentHistoryData.length - 1) {
                    const tickWidth = x.getPixelForValue(1) - x.getPixelForValue(0);
                    sX = sX - (tickWidth / 2);
                    width = tickWidth;
                }

                if (width > 2) {
                    // 1. Relleno gris
                    ctx.fillStyle = bgColor;
                    ctx.fillRect(sX, chartArea.top, width, chartArea.bottom - chartArea.top);
                    
                    // 2. Borde gris ultra-fino
                    ctx.strokeStyle = borderColor;
                    ctx.lineWidth = 1;
                    ctx.strokeRect(sX, chartArea.top, width, chartArea.bottom - chartArea.top);
                    
                    // 3. Texto centrado
                    ctx.fillStyle = textColor;
                    ctx.font = 'bold 11px sans-serif'; 
                    ctx.textAlign = 'center';
                    ctx.textBaseline = 'middle';
                    
                    if (width > 80) ctx.fillText('SIN DATOS', sX + width/2, chartArea.top + 25);
                    else if (width > 30) ctx.fillText('🔌', sX + width/2, chartArea.top + 25);
                }
            });
            ctx.restore();
        }
    };

    if (myChart) myChart.destroy();
    myChart = new Chart(ctx, { 
        type: 'line', 
        data: { labels, datasets }, 
        options: { 
            responsive: true, 
            maintainAspectRatio: false, 
            interaction: { intersect: false, mode: 'index' }, 
            scales: { y: { beginAtZero: true } },
            animation: { duration: 0 } 
        }, 
        plugins: [gapShadingPlugin] 
    });
}

// --- ALERTAS (VISTA) ---
async function cargarAlertas() {
    try { const res = await authFetch('/api/alertas'); renderizarAlertas(await res.json()); } catch (e) { console.error("Error alertas:", e); }
}

// --- ALERTAS (VISTA) ---
function renderizarAlertas(data) {
    // 1. Renderizar Alertas Activas
    const tbodyA = document.getElementById('alertas-activas-body');
    const msg = document.getElementById('no-activas');
    if (tbodyA) tbodyA.innerHTML = '';
    
    if (data.activas.length === 0) { 
        if(msg) msg.style.display = 'block'; 
    } else {
        if(msg) msg.style.display = 'none';
        data.activas.forEach(a => {
            const min = Math.floor((new Date() - new Date(a.start_time))/60000);
            
            // Lógica de Semáforo para la Etiqueta
            let badgeClass = 'status-critical'; // Rojo por defecto
            if (a.mensaje.includes('[NOTICE]')) {
                badgeClass = 'status-notice';   // Amarillo
            } else if (a.mensaje.includes('[WARNING]')) {
                badgeClass = 'status-warning';  // Naranja
            }

            // Botón de Asana con enlace directo (Focus Mode - Nueva API Asana)
            let asanaBtn = '';
            if (a.asana_task_gid) {
                // Usamos el ID de tu Workspace y el ID del Tablero General
                const workspaceId = '1165430292217894';
                const tableroGeneralId = '1209783009881570';
                const asanaLink = `https://app.asana.com/1/${workspaceId}/project/${tableroGeneralId}/task/${a.asana_task_gid}?focus=true`;
                
                asanaBtn = `
                <a href="${asanaLink}" target="_blank" 
                   style="text-decoration:none; background:#2c3e50; color:white; padding:3px 10px; border-radius:12px; font-size:0.75em; font-weight:600; display:inline-flex; align-items:center; gap:5px; margin-left:10px; transition:0.2s;" 
                   onmouseover="this.style.background='#f06a6a'" 
                   onmouseout="this.style.background='#2c3e50'" 
                   title="Abrir tarea directo en Asana">
                    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path><polyline points="15 3 21 3 21 9"></polyline><line x1="10" y1="14" x2="21" y2="3"></line>
                    </svg>
                    Ver
                </a>`;
            }

            tbodyA.innerHTML += `
                <tr>
                    <td style="font-weight:bold;"><span class="hospital-tag">${a.hospital_id}</span></td>
                    <td>
                        <span class="status-badge ${badgeClass}" style="font-size:0.75em; margin-right:5px; color:#000;">${a.tipo}</span> 
                        <span style="color:#2c3e50;">${a.mensaje}</span>
                        ${asanaBtn}
                    </td>
                    <td style="font-size:0.9em; color:#555;">${new Date(a.start_time).toLocaleString()}</td>
                    <td style="color:#c0392b; font-weight:bold;">hace ${min} min</td>
                </tr>`;
        });
    }

    // 2. Renderizar Historial de Alertas
    const tbodyH = document.getElementById('alertas-historial-body');
    if (tbodyH) {
        tbodyH.innerHTML = '';
        data.historial.forEach(a => {
            
            // Botón directo para el historial
            let asanaBtnH = '';
            if (a.asana_task_gid) {
                const workspaceId = '1165430292217894';
                const tableroGeneralId = '1209783009881570';
                const asanaLinkH = `https://app.asana.com/1/${workspaceId}/project/${tableroGeneralId}/task/${a.asana_task_gid}?focus=true`;
                
                asanaBtnH = `<a href="${asanaLinkH}" target="_blank" style="text-decoration:none; color:#3498db; font-size:0.85em; margin-left:10px; font-weight:bold;" title="Ver registro histórico directo en Asana">Ver en Asana ↗</a>`;
            }

            tbodyH.innerHTML += `
                <tr>
                    <td><span class="hospital-tag">${a.hospital_id}</span></td>
                    <td>
                        <span class="status-badge status-offline" style="font-size:0.75em; color:#000;">${a.tipo}</span> 
                        ${asanaBtnH}
                    </td>
                    <td style="font-size:0.9em; color:#555;">${new Date(a.start_time).toLocaleString()}</td>
                    <td style="font-size:0.9em; color:#555;">${new Date(a.end_time).toLocaleString()}</td>
                    <td><span class="status-badge status-online" style="padding:4px 8px; border-radius:12px; font-size:0.8em; color:white; background:#28a745;">Resuelto</span></td>
                </tr>`;
        });
    }
}

// --- GESTIÓN DE HOSPITALES (CRUD) ---
let isEditing = false;

async function listarHospitalesConfig() {
    const tbody = document.getElementById('lista-hospitales-body');
    tbody.innerHTML = '<tr><td colspan="5">Cargando...</td></tr>';
    
    try {
        const res = await authFetch('/api/hospitales-metadata');
        const lista = await res.json();
        
        tbody.innerHTML = '';
        if(lista.length === 0) {
            tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:#999;">No hay hospitales configurados. El dashboard estará vacío.</td></tr>';
            return;
        }

        lista.forEach(h => {
            const tr = document.createElement('tr');
            
            // Icono Visibilidad (Ojo)
            const isVis = h.is_visible !== false; 
            const eyeIcon = isVis 
                ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>'
                : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';
            const btnClassVis = isVis ? 'btn-view' : 'btn-hide';

            // Icono Alertas (Campana)
            const alertsOn = h.alerts_enabled !== false; 
            const bellIcon = alertsOn
                ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"></path><path d="M13.73 21a2 2 0 0 1-3.46 0"></path></svg>'
                : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M13.73 21a2 2 0 0 1-3.46 0"></path><path d="M18.63 13A17.89 17.89 0 0 1 18 8"></path><path d="M6.26 6.26A5.86 5.86 0 0 0 6 8c0 7-3 9-3 9h14"></path><path d="M18 8a6 6 0 0 0-9.33-5"></path><line x1="1" y1="1" x2="23" y2="23"></line></svg>';
            const btnClassBell = alertsOn ? 'btn-view' : 'btn-hide';

            const opacityStyle = isVis ? '' : 'opacity: 0.6; background: #f8f9fa;'; 

            tr.style = opacityStyle;
            tr.innerHTML = `
                <td><span class="hospital-tag">${h.hospital_id}</span></td>
                <td style="font-weight:600;">${h.nombre}</td>
                <td>${h.provincia || '-'}</td>
                <td style="font-family:monospace; font-size:0.9em;">${h.asana_project_id || '-'}</td>
                <td style="text-align:right;">
                    <button class="btn-small ${btnClassBell}" onclick="toggleAlertas('${h.hospital_id}')" title="Alertas ON/OFF" style="margin-right:5px; background-color:${alertsOn?'#e67e22':'#95a5a6'};">${bellIcon}</button>
                    <button class="btn-small ${btnClassVis}" onclick="toggleVisibilidad('${h.hospital_id}')" title="Mostrar/Ocultar Dashboard">${eyeIcon}</button>
                    <button class="btn-small btn-edit" onclick='editarHospital(${JSON.stringify(h)})'>✏️</button>
                    <button class="btn-small btn-delete" onclick="eliminarHospital('${h.hospital_id}')">🗑️</button>
                </td>
            `;
            tbody.appendChild(tr);
        });
    } catch (e) { console.error("Error listando hospitales:", e); }
}

async function toggleAlertas(id) {
    try {
        const res = await authFetch(`/api/hospitales-metadata/${id}/toggle-alerts`, { method: 'PATCH' });
        if (res.ok) {
            listarHospitalesConfig(); 
        } else {
            alert("Error al cambiar estado de alertas");
        }
    } catch (e) { console.error(e); }
}

// Filtro Buscador
function filtrarHospitalesConfig() {
    const input = document.getElementById('filter-config-hospital');
    const filter = input.value.toLowerCase().trim();
    const rows = document.querySelectorAll('#lista-hospitales-body tr');

    rows.forEach(row => {
        const text = row.textContent.toLowerCase();
        if (text.includes(filter)) {
            row.style.display = ''; 
        } else {
            row.style.display = 'none';
        }
    });
}

async function toggleVisibilidad(id) {
    try {
        const res = await authFetch(`/api/hospitales-metadata/${id}/toggle`, { method: 'PATCH' });
        if (res.ok) {
            listarHospitalesConfig(); 
        } else {
            alert("Error al cambiar visibilidad");
        }
    } catch (e) { console.error(e); }
}

function abrirModalHospital() {
    isEditing = false;
    document.getElementById('modal-title').innerText = "Nuevo Hospital";
    document.getElementById('hosp-id').value = "";
    document.getElementById('hosp-id').disabled = false; 
    document.getElementById('hosp-nombre').value = "";
    document.getElementById('hosp-provincia').value = "";
    document.getElementById('hosp-asana').value = "";
    document.getElementById('hosp-lat').value = "";
    document.getElementById('hosp-lon').value = "";
    document.getElementById('modal-hospital').style.display = 'flex';
}

function editarHospital(obj) {
    isEditing = true;
    window.currentEditingVis = obj.is_visible; 
    document.getElementById('modal-title').innerText = "Editar Hospital";
    document.getElementById('hosp-id').value = obj.hospital_id;
    document.getElementById('hosp-id').disabled = true; 
    document.getElementById('hosp-nombre').value = obj.nombre;
    document.getElementById('hosp-provincia').value = obj.provincia || "";
    document.getElementById('hosp-asana').value = obj.asana_project_id || "";
    document.getElementById('hosp-lat').value = obj.latitud || "";
    document.getElementById('hosp-lon').value = obj.longitud || "";
    document.getElementById('modal-hospital').style.display = 'flex';
}

function cerrarModalHospital() {
    document.getElementById('modal-hospital').style.display = 'none';
}

async function guardarHospital() {
    const id = document.getElementById('hosp-id').value.trim();
    if(!id) return alert("El ID es obligatorio");

    const payload = {
        hospital_id: id,
        nombre: document.getElementById('hosp-nombre').value,
        provincia: document.getElementById('hosp-provincia').value,
        asana_project_id: document.getElementById('hosp-asana').value,
        latitud: document.getElementById('hosp-lat').value,
        longitud: document.getElementById('hosp-lon').value,
        is_visible: true 
    };
    
    if (isEditing && window.currentEditingVis !== undefined) {
        payload.is_visible = window.currentEditingVis;
    }

    const url = isEditing ? `/api/hospitales-metadata/${id}` : '/api/hospitales-metadata';
    const method = isEditing ? 'PUT' : 'POST';

    try {
        const res = await authFetch(url, {
            method: method,
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(payload)
        });
        
        if(res.ok) {
            cerrarModalHospital();
            listarHospitalesConfig(); 
        } else {
            const err = await res.json();
            alert("Error: " + err.detail);
        }
    } catch(e) { alert("Error de conexión"); }
}

async function eliminarHospital(id) {
    if(!confirm(`¿Seguro que deseas eliminar la ficha de ${id}?`)) return;
    
    try {
        const res = await authFetch(`/api/hospitales-metadata/${id}`, { method: 'DELETE' });
        if(res.ok) listarHospitalesConfig();
        else alert("Error eliminando");
    } catch(e) { alert("Error de conexión"); }
}

// --- NAVEGACIÓN DIRECTA A SECCIONES RESTRINGIDAS ---
// El control de acceso está manejado por el JWT en el backend y por
// aplicarRestriccionesUI() que oculta los botones según el rol en el frontend.
// Ya no se necesita un modal de código adicional.
function verificarAccesoConfig(viewId, idBoton, btnMobile) {
    navegar(viewId, idBoton, btnMobile);
    if (viewId === 'view-config') cargarConfigUI();
}

// --- MÓDULO MAPA (LEAFLET) ---
function initMapa() {
    if (mapInstance) {
        cargarDatosMapa();
        return;
    }

    // Inicializar mapa (Vista Argentina)
    mapInstance = L.map('map-container', { zoomControl: false }).setView([-38.4161, -63.6167], 4);
    
    // Capa visual (CartoDB Light para que combine con el dashboard)
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; OpenStreetMap &copy; CARTO',
        subdomains: 'abcd',
        maxZoom: 19
    }).addTo(mapInstance);

    mapMarkers = L.layerGroup().addTo(mapInstance);
    
    // Forzamos el calculo de tamaño apenas se crea
    setTimeout(() => mapInstance.invalidateSize(), 100);
    
    cargarDatosMapa();
}

async function cargarDatosMapa() {
    try {
        const res = await authFetch('/api/mapa-data');
        mapData = await res.json();
        
        // Ordenar de Norte a Sur para el tour
        mapData.sort((a, b) => b.lat - a.lat);

        renderizarMarcadores();
    } catch (e) { console.error("Error mapa:", e); }
}

// --- RENDERIZAR MARCADORES (MODIFICADO CON FILTRO) ---
function renderizarMarcadores() {
    if (!mapInstance || !mapMarkers) return;
    mapMarkers.clearLayers(); // Limpiamos los puntos viejos

    mapData.forEach(h => {
        const color = h.status === 'Online' ? '#2ecc71' : '#e74c3c';
        
        // --- NUEVA LÓGICA DE FILTRADO ---
        if (currentMapFilter === 'online' && h.status !== 'Online') return; // Salta los rojos
        if (currentMapFilter === 'offline' && h.status === 'Online') return; // Salta los verdes
        // -------------------------------

        const marker = L.circleMarker([h.lat, h.lng], {
            radius: 8,
            fillColor: color,
            color: "#fff",
            weight: 2,
            opacity: 1,
            fillOpacity: 0.9
        });

        // Popup con botón para ir al detalle
        marker.bindPopup(`
            <div style="text-align:center; font-family:sans-serif; min-width: 120px;">
                <b style="color:#2c3e50; font-size:1.1em;">${h.nombre}</b><br>
                <small style="color:#7f8c8d; font-weight:bold;">${h.id}</small><br>
                <div style="margin-top:5px; margin-bottom:8px;">
                    <span style="background:${color}20; color:${color}; padding:2px 8px; border-radius:10px; font-weight:bold; font-size:0.85em;">● ${h.status}</span>
                </div>
                <button onclick="verDetalle('${h.id}')" style="background:#3498db; color:white; border:none; border-radius:4px; padding:6px 12px; cursor:pointer; font-size:0.85em; width:100%;">Ver Detalles</button>
            </div>
        `);

        mapMarkers.addLayer(marker);
    });
}

function siguienteDestino() {
    if (mapData.length === 0) return;

    const h = mapData[tourIndex];
    
    // Vuelo suave (Zoom 9 para ver jurisdicción)
    mapInstance.flyTo([h.lat, h.lng], 9, {
        animate: true,
        duration: 4 
    });

    // Abrir popup al llegar
    setTimeout(() => {
        mapMarkers.eachLayer(layer => {
            const latlng = layer.getLatLng();
            if (Math.abs(latlng.lat - h.lat) < 0.0001 && Math.abs(latlng.lng - h.lng) < 0.0001) {
                layer.openPopup();
            }
        });
    }, 4500);

    tourIndex = (tourIndex + 1) % mapData.length;
}

function toggleTour() {
    const btn = document.getElementById('btn-tour');
    
    if (tourInterval) {
        clearInterval(tourInterval);
        tourInterval = null;
        btn.innerHTML = "▶ INICIAR RECORRIDO";
        btn.classList.remove('active');
        // Restaurar vista general
        mapInstance.flyTo([-38.4161, -63.6167], 4, { duration: 2 });
    } else {
        tourIndex = 0;
        siguienteDestino(); 
        tourInterval = setInterval(siguienteDestino, 10000); // 10 segundos por hospital
        btn.innerHTML = "■ DETENER";
        btn.classList.add('active');
    }
}

/* --- MÓDULO REPORTES E IA (FRONTEND MOCKUP) --- */
const historialIAMock = [
    // Agregamos la propiedad "tipo" a los datos falsos para probar el diseño
    { id: 'H42', tipo: 'ia_cliente', desde: '2025-01-01', hasta: '2025-01-31', generado: 'Hoy, 10:30', estado: 'ready' },
    { id: 'H06', tipo: 'kpi_excel', desde: '2025-01-15', hasta: '2025-01-20', generado: 'Ayer, 18:45', estado: 'ready' },
    { id: 'P03', tipo: 'ia_interno', desde: '2024-12-01', hasta: '2024-12-31', generado: '01/02/2026', estado: 'error' },
];

let tipoInformeSeleccionado = 'cliente'; // Estado local del modal

async function renderizarHistorial() {
    const tbody = document.getElementById('tabla-historial-body');
    if (!tbody) return;
    
    try {
        const response = await authFetch('/api/informes/historial');
        const historial = await response.json();
        
        if (historial.length === 0) {
            tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; padding: 20px; color: #95a5a6;">No hay reportes generados recientemente.</td></tr>`;
            return;
        }

        tbody.innerHTML = historial.map(rep => {
            let colorEstado = rep.estado === 'Completado' ? '#2ecc71' : (rep.estado === 'Error' ? '#e74c3c' : '#f39c12');
            let bgEstado = rep.estado === 'Completado' ? '#eafaf1' : (rep.estado === 'Error' ? '#fdedec' : '#fef5e7');
            
            let botonAccion = rep.asana_url 
                ? `<a href="${rep.asana_url}" target="_blank" style="background: #3498db; color: white; padding: 6px 12px; border-radius: 4px; text-decoration: none; font-size: 0.9em; display: inline-block;">📄 Ver</a>`
                : `<span style="background: #bdc3c7; color: white; padding: 6px 12px; border-radius: 4px; font-size: 0.9em; display: inline-block; cursor: not-allowed;">⬇️ Local</span>`;

            return `
            <tr style="border-bottom: 1px solid #eee;">
                <td style="padding: 15px 10px;">
                    <span style="background: #ecf0f1; padding: 4px 8px; border-radius: 4px; font-weight: bold; color: #2c3e50;">${rep.hospital_id}</span>
                </td>
                <td style="padding: 15px 10px; color: #34495e;">📄 ${rep.tipo_reporte}<br><small style="color: #7f8c8d;">${rep.periodo}</small></td>
                <td style="padding: 15px 10px; color: #7f8c8d;">${rep.fecha_generacion}</td>
                <td style="padding: 15px 10px;">
                    <span style="background: ${bgEstado}; color: ${colorEstado}; padding: 4px 8px; border-radius: 12px; font-size: 0.85em; font-weight: bold;">
                        ${rep.estado}
                    </span>
                </td>
                <td style="padding: 15px 10px; text-align: center;">
                    ${botonAccion}
                </td>
            </tr>
            `;
        }).join('');
    } catch (error) {
        console.error("Error al cargar historial:", error);
    }
}

// Ejecutar al cargar la página
document.addEventListener('DOMContentLoaded', renderizarHistorial);

// --- AGREGAR ESTA NUEVA FUNCIÓN ---
function abrirReporteDemo(idHospital) {
    // 1. Feedback visual inmediato
    // (Opcional: podrías mostrar un loader pequeño si quisieras)
    
    // 2. Abrir el PDF en una pestaña nueva
    // Usamos un pequeño truco de query param (?v=...) para que el navegador no use caché viejo si cambias el archivo.
    const url = `/static/reporte_demo.pdf?h=${idHospital}&v=${new Date().getTime()}`;
    
    // Abrimos en nueva pestaña
    window.open(url, '_blank');
}

function formatoFechaSimple(fechaStr) {
    if(!fechaStr) return "-";
    const [y, m, d] = fechaStr.split('-');
    return `${d}/${m}`;
}

// 1. ABRIR MODAL (Validación previa)
// --- LÓGICA DE FORMULARIO DINÁMICO DE REPORTES ---
function cambiarTipoProcesamiento(tipo) {
    // 1. Actualizar el valor del campo oculto
    const inputTipo = document.getElementById('proc-tipo');
    if (inputTipo) inputTipo.value = tipo;
    
    // 2. Gestionar estado visual de las pestañas
    const btnIA = document.getElementById('btn-tipo-ia');
    const btnPDF = document.getElementById('btn-tipo-pdf');

    // Reseteamos colores primero
    if (btnIA) {
        btnIA.classList.remove('active');
        btnIA.style.backgroundColor = '';
        btnIA.style.color = '';
    }
    if (btnPDF) {
        btnPDF.classList.remove('active');
        btnPDF.style.backgroundColor = '';
        btnPDF.style.color = '';
    }

    // Pintamos el activo con su color de "marca"
    const targetBtn = document.getElementById('btn-tipo-' + tipo);
    if (targetBtn) {
        targetBtn.classList.add('active');
        if (tipo === 'pdf') {
            targetBtn.style.backgroundColor = '#e74c3c'; // Rojo PDF
            targetBtn.style.color = 'white';
        } else if (tipo === 'ia') {
            targetBtn.style.backgroundColor = '#9b59b6'; // Violeta IA
            targetBtn.style.color = 'white';
        }
    }

    // 3. Referencias a elementos de la interfaz
    const extraDivKpi = document.getElementById('opciones-kpi-extra');
    const btnAction = document.getElementById('btn-procesar-accion');
    const btnTexto = document.getElementById('btn-procesar-texto');
    const icono = document.getElementById('icono-procesar');
    const tarjeta = document.getElementById('tarjeta-procesamiento');
    
    // 4. Configuración específica por tipo
    if (tipo === 'kpi') {
        if (extraDivKpi) extraDivKpi.style.display = 'flex';
        if (tarjeta) tarjeta.style.borderLeftColor = '#27ae60'; // Verde Excel
        
        if (btnTexto) btnTexto.innerText = 'Descargar Excel (CSV)';
        if (btnAction) {
            btnAction.style.background = 'linear-gradient(135deg, #27ae60, #2ecc71)';
            btnAction.style.boxShadow = '0 4px 6px rgba(39, 174, 96, 0.3)';
        }
        if (icono) {
            icono.innerHTML = '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line>'; 
        }
    
    } else if (tipo === 'pdf') {
        if (extraDivKpi) extraDivKpi.style.display = 'none';
        if (tarjeta) tarjeta.style.borderLeftColor = '#e74c3c'; // Rojo PDF
        
        if (btnTexto) btnTexto.innerText = 'Configurar Reporte PDF';
        if (btnAction) {
            btnAction.style.background = 'linear-gradient(135deg, #e74c3c, #c0392b)';
            btnAction.style.boxShadow = '0 4px 6px rgba(231, 76, 60, 0.3)';
        }
        if (icono) {
            icono.innerHTML = '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path><polyline points="14 2 14 8 20 8"></polyline><line x1="16" y1="13" x2="8" y2="13"></line><line x1="16" y1="17" x2="8" y2="17"></line><polyline points="10 9 9 9 8 9"></polyline>'; 
        }
    
    } else { // Caso 'ia'
        if (extraDivKpi) extraDivKpi.style.display = 'none';
        if (tarjeta) tarjeta.style.borderLeftColor = '#9b59b6'; // Violeta IA
        
        if (btnTexto) btnTexto.innerText = 'Configurar Informe IA';
        if (btnAction) {
            btnAction.style.background = 'linear-gradient(135deg, #9b59b6, #8e44ad)';
            btnAction.style.boxShadow = '0 4px 6px rgba(142, 68, 173, 0.3)';
        }
        if (icono) {
            icono.innerHTML = '<path d="M12 2L14.4 7.6L20 10L14.4 12.4L12 18L9.6 12.4L4 10L9.6 7.6L12 2Z"></path>'; 
        }
    }
}

// --- FUNCION PARA BOTONES DE FECHAS RÁPIDAS ---
function setFechasRapidas(meses, btn) {
    // 1. Lógica Visual: Pintar el botón activo y despintar el resto
    if (btn) {
        document.querySelectorAll('.quick-date-btn').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
    }

    // 2. Lógica de Fechas
    const hoy = new Date();
    // Ajuste para la zona horaria local (evita que cambie de día por UTC)
    const localHoy = new Date(hoy.getTime() - (hoy.getTimezoneOffset() * 60000));
    const fHasta = localHoy.toISOString().split('T')[0];

    // Clonamos la fecha y restamos los meses
    const fDesdeObj = new Date(localHoy);
    fDesdeObj.setMonth(fDesdeObj.getMonth() - meses);
    const fDesde = fDesdeObj.toISOString().split('T')[0];

    // Asignamos a los inputs
    document.getElementById('ia-date-from').value = fDesde;
    document.getElementById('ia-date-to').value = fHasta;
}

// Interceptor del botón principal
function iniciarProcesamiento() {
    const tipo = document.getElementById('proc-tipo').value;
    const rawIdInput = document.getElementById('ia-input-id').value.trim(); // "H05 - H. Ernesto Campos"
    const id = rawIdInput.split(' - ')[0].toUpperCase(); // "H05"
    const f1 = document.getElementById('ia-date-from').value;
    const f2 = document.getElementById('ia-date-to').value;

    if (!id || !f1 || !f2) {
        alert("⚠️ Por favor completa el ID del Hospital y las fechas de inicio y fin.");
        return;
    }

    if (tipo === 'ia') {
        abrirModalIA(id, f1, f2); 
    } else if (tipo === 'kpi') {
        ejecutarExportacionExcel(id, f1, f2);
    } else if (tipo === 'pdf') {
        // Le pasamos el texto completo para que el modal se vea mejor
        abrirModalPDF(rawIdInput, f1, f2); 
    }
}

function abrirModalPDF(hospitalCompleto, f1, f2) {
    // Siempre nos aseguramos de que el modal tenga el contenido limpio al abrir
    const modalBody = document.querySelector('#modal-pdf-options .modal-content');
    modalBody.innerHTML = HTML_MODAL_PDF_ORIGINAL;

    const spanHospital = document.getElementById('modal-pdf-hospital');
    const spanPeriodo = document.getElementById('modal-pdf-periodo');
    
    if (spanHospital) spanHospital.innerText = hospitalCompleto;
    if (spanPeriodo) spanPeriodo.innerText = `${formatoFechaSimple(f1)} al ${formatoFechaSimple(f2)}`;
    
    document.getElementById('modal-pdf-options').style.display = 'flex';
}

function cerrarModalPDF() {
    document.getElementById('modal-pdf-options').style.display = 'none';
}

async function ejecutarGeneracionPDF() {
    const rawIdInput = document.getElementById('ia-input-id').value.trim();
    const id = rawIdInput.split(' - ')[0].toUpperCase();
    const f1 = document.getElementById('ia-date-from').value;
    const f2 = document.getElementById('ia-date-to').value;
    const taskId = document.getElementById('pdf-asana-task').value.trim();

    // --- 1. DETERMINAR TIPO DE REPORTE ---
    const isInfra = document.getElementById('pdf-type-infra').checked;
    const tipoReporte = isInfra ? 'infra' : 'clinico';

    // --- 2. DETERMINAR ALCANCE (Solo relevante para Clínico) ---
    const chkRis = document.getElementById('pdf-scope-ris').checked;
    const chkPacs = document.getElementById('pdf-scope-pacs').checked;
    
    // Validamos que haya al menos uno seleccionado si el reporte es Clínico
    if (!isInfra && !chkRis && !chkPacs) {
        alert("⚠️ Por favor selecciona al menos un alcance para el reporte (RIS o PACS).");
        return;
    }
    
    let scopeValue = 'total';
    if (chkRis && !chkPacs) scopeValue = 'ris';
    else if (!chkRis && chkPacs) scopeValue = 'pacs';

    // --- 3. VALIDACIONES GENERALES ---
    if (!id || !f1 || !f2) {
        alert("⚠️ Por favor completa el hospital y el rango de fechas.");
        return;
    }

    if (!taskId) {
        alert("⚠️ Por favor ingresa el ID de la tarea de Asana destino.");
        document.getElementById('pdf-asana-task').focus();
        return;
    }

    // --- 4. PREPARAR PAYLOAD ---
    const payload = {
        hospital_id: id,
        fecha_desde: f1,
        fecha_hasta: f2,
        alcance: scopeValue,
        tipo_reporte: tipoReporte, // Nuevo parámetro para el backend
        asana_task_id: taskId
    };

    // UI Feedback: Desactivar botón y mostrar carga
    const btn = document.querySelector('#modal-pdf-options .btn-action:last-child');
    const btnOriginalText = btn.innerHTML;
    btn.innerHTML = '⏳ Generando...';
    btn.disabled = true;

    try {
        const response = await authFetch('/api/informes/pdf', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });

        if (response.ok) {
            const contentType = response.headers.get("content-type");
            
            if (contentType && contentType.includes("application/pdf")) {
                // FALLBACK: Si el backend devuelve el archivo directo (ej. fallo subida Asana)
                const blob = await response.blob();
                const url = window.URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `Reporte_${tipoReporte.toUpperCase()}_${id}.pdf`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                
                alert("⚠️ El reporte se descargó localmente porque no pudo adjuntarse a Asana automáticamente.");
                cerrarModalPDF();
                
            } else {
                // ÉXITO: Se adjuntó a Asana
                const data = await response.json();
                
                // Mostrar pantalla de éxito dentro del modal
                const modalBody = document.querySelector('#modal-pdf-options .modal-content');
                modalBody.innerHTML = `
                    <div style="text-align: center; padding: 20px;">
                        <div style="font-size: 3em; margin-bottom: 10px;">✅</div>
                        <h3 style="color: #27ae60; margin-top: 0;">¡Reporte Generado!</h3>
                        <p style="color: #7f8c8d; margin-bottom: 25px;">El PDF de ${tipoReporte === 'infra' ? 'Infraestructura' : 'Uso Clínico'} fue creado y adjuntado a Asana.</p>
                        <a href="${data.asana_url}" target="_blank" class="btn-action" style="background: #3498db; text-decoration: none; padding: 12px 25px; display: inline-block; color: white; border-radius: 6px; font-weight: bold; box-shadow: 0 4px 6px rgba(52, 152, 219, 0.3);">
                            Abrir Tarea en Asana ↗
                        </a>
                        <button onclick="finalizarProcesoPDF()" style="display: block; margin: 20px auto 0; background: transparent; border: none; color: #95a5a6; cursor: pointer; text-decoration: underline;">Cerrar y volver</button>
                    </div>
                `;
            }
        } else {
            const err = await response.json();
            alert("Error del servidor: " + (err.detail || "Desconocido"));
        }
    } catch (error) {
        console.error("Error generating PDF:", error);
        alert("Error de conexión al generar el reporte.");
    } finally {
        // Restaurar botón si el modal no se transformó en pantalla de éxito
        if (document.querySelector('#modal-pdf-options .btn-action:last-child')) {
            btn.innerHTML = btnOriginalText;
            btn.disabled = false;
        }
    }
}

function finalizarProcesoPDF() {
    // 1. Cerramos el modal
    cerrarModalPDF();
    
    // 2. Restauramos el formulario original para la próxima vez
    const modalBody = document.querySelector('#modal-pdf-options .modal-content');
    if (modalBody) {
        modalBody.innerHTML = HTML_MODAL_PDF_ORIGINAL;
    }
    
    // 3. Refrescamos la tabla de historial
    renderizarHistorial();
    
    // 4. Limpiamos los campos del buscador de la pestaña principal
    document.getElementById('ia-input-id').value = '';
    document.getElementById('ia-date-from').value = '';
    document.getElementById('ia-date-to').value = '';
}

// 1. ABRIR MODAL IA
function abrirModalIA(id, f1, f2) {
    document.getElementById('modal-ia-hospital').innerText = id;
    document.getElementById('modal-ia-periodo').innerText = `${formatoFechaSimple(f1)} al ${formatoFechaSimple(f2)}`;
    document.getElementById('ia-obs').value = '';
    seleccionarTipoInforme('cliente'); // Default
    document.getElementById('modal-ia-options').style.display = 'flex';
}

// 2. INICIAR EXPORTACIÓN EXCEL (MOCKUP)
function ejecutarExportacionExcel(id, f1, f2) {
    const fuente = document.getElementById('kpi-export-source').options[document.getElementById('kpi-export-source').selectedIndex].text;
    const agrupacion = document.getElementById('kpi-export-group').options[document.getElementById('kpi-export-group').selectedIndex].text;
    
    alert(`🚧 Preparando Excel...\n\nHospital: ${id}\nPeriodo: ${formatoFechaSimple(f1)} a ${formatoFechaSimple(f2)}\nFuente: ${fuente}\nAgrupado por: ${agrupacion}\n\n(Pronto programaremos la descarga real del CSV)`);
}

// 3. NUEVO: INICIAR EXPORTACIÓN PDF COMPLETO (MOCKUP)
function ejecutarExportacionPDFCompleto(id, f1, f2) {
    // Leemos el valor del nuevo selector
    const scopeElement = document.getElementById('pdf-export-scope');
    const scopeText = scopeElement.options[scopeElement.selectedIndex].text;
    const scopeValue = scopeElement.value; // 'total', 'ris', o 'pacs'

    alert(`🚧 Preparando Reporte Oficial PDF...\n\nHospital: ${id}\nPeriodo: ${formatoFechaSimple(f1)} a ${formatoFechaSimple(f2)}\nAlcance: ${scopeText}\n\n(Dato interno para backend: ${scopeValue})`);
}

function filtrarSugerenciasIA() {
    const input = document.getElementById('ia-input-id');
    const resContainer = document.getElementById('ia-search-results');
    const valor = input.value.trim().toLowerCase();

    // REGLA: Si tiene menos de 2 letras, ocultar todo
    if (valor.length < 2) {
        resContainer.style.display = 'none';
        return;
    }

    // Filtrar la lista cacheada
    const coincidencias = listaHospitalesCache.filter(h => 
        h.hospital_id.toLowerCase().includes(valor) || 
        h.nombre.toLowerCase().includes(valor)
    );

    if (coincidencias.length > 0) {
        resContainer.innerHTML = coincidencias.map(h => `
            <div class="search-item" onclick="seleccionarHospitalIA('${h.hospital_id} - ${h.nombre}')" 
                 style="padding:10px 15px; cursor:pointer; border-bottom:1px solid #f1f1f1; transition:0.2s;">
                <span style="font-weight:bold; color:var(--primary); font-family:monospace;">${h.hospital_id}</span> 
                <span style="color:#2c3e50; margin-left:8px;">${h.nombre}</span>
            </div>
        `).join('');
        resContainer.style.display = 'block';
    } else {
        resContainer.style.display = 'none';
    }
}

function seleccionarHospitalIA(textoCompleto) {
    document.getElementById('ia-input-id').value = textoCompleto;
    document.getElementById('ia-search-results').style.display = 'none';
}

// --- CARGAR LISTA DE HOSPITALES PARA EL BUSCADOR DE REPORTES ---
async function cargarListaHospitalesIA() {
    try {
        const res = await authFetch('/api/hospitales-metadata');
        const lista = await res.json();
        listaHospitalesCache = lista.filter(h => h.is_visible !== false);
        
        // Configuramos el evento de escucha en el input
        const input = document.getElementById('ia-input-id');
        if(input) {
            input.addEventListener('input', filtrarSugerenciasIA);
            // Cerrar al hacer clic afuera
            document.addEventListener('click', (e) => {
                if (!input.contains(e.target)) document.getElementById('ia-search-results').style.display = 'none';
            });
        }
    } catch (e) { console.error(e); }
}

// 1. ABRIR MODAL IA (Recibe los parámetros validados)
function abrirModalIA(id, f1, f2) {
    document.getElementById('modal-ia-hospital').innerText = id;
    document.getElementById('modal-ia-periodo').innerText = `${formatoFechaSimple(f1)} al ${formatoFechaSimple(f2)}`;
    document.getElementById('ia-obs').value = '';
    seleccionarTipoInforme('cliente'); // Default
    document.getElementById('modal-ia-options').style.display = 'flex';
}

// 2. INICIAR EXPORTACIÓN EXCEL (MOCKUP)
function ejecutarExportacionExcel(id, f1, f2) {
    const fuente = document.getElementById('kpi-export-source').options[document.getElementById('kpi-export-source').selectedIndex].text;
    const agrupacion = document.getElementById('kpi-export-group').options[document.getElementById('kpi-export-group').selectedIndex].text;
    
    alert(`🚧 Preparando Excel...\n\nHospital: ${id}\nPeriodo: ${formatoFechaSimple(f1)} a ${formatoFechaSimple(f2)}\nFuente: ${fuente}\nAgrupado por: ${agrupacion}\n\n(Pronto programaremos la descarga real del CSV)`);
}

// 2. LÓGICA DE SELECCIÓN VISUAL (Cards)
function seleccionarTipoInforme(tipo) {
    tipoInformeSeleccionado = tipo;
    
    // Estilos Cliente
    const cardC = document.getElementById('opt-cliente');
    // Estilos Interno
    const cardI = document.getElementById('opt-interno');

    if (tipo === 'cliente') {
        cardC.style.border = '2px solid #9b59b6';
        cardC.style.background = '#fdfaea';
        
        cardI.style.border = '1px solid #ddd';
        cardI.style.background = '#fff';
    } else {
        cardI.style.border = '2px solid #9b59b6';
        cardI.style.background = '#fdfaea';
        
        cardC.style.border = '1px solid #ddd';
        cardC.style.background = '#fff';
    }
}

function cerrarModalIA() {
    document.getElementById('modal-ia-options').style.display = 'none';
}

// 3. EJECUCIÓN FINAL
function ejecutarIA() {
    // Aquí iría la llamada al backend real
    alert("🚧 Funcionalidad en desarrollo\n\nEl motor de IA está procesando los parámetros seleccionados.");
    
    // Simular cierre
    cerrarModalIA();
}

// ==========================================
// --- MOTOR GLOBAL DE KPIs Y SOFTWARE (V4) ---
// ==========================================

// Variable Maestra (Asegurate de que esté declarada arriba de tu script.js, o dejala acá si no lo está)
// let currentKpiHistoryData = []; 
// let currentKpiRangeHours = 24;

function cambiarRangoGlobalKpi(horas, btn) {
    document.querySelectorAll('.kpi-global-time-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentKpiRangeHours = horas; 
    cargarHistorialKpiGlobal(horas, currentHospitalId);
}

async function cargarHistorialKpiGlobal(horas, idSolicitado) {
    if (currentHospitalId !== idSolicitado) return;
    const loader = document.getElementById('kpi-chart-loader');
    if(loader) loader.style.display = 'flex';
    
    try {
        const response = await authFetch(`/api/hospital/${idSolicitado}/kpi-history?horas=${horas}`);
        if (currentHospitalId !== idSolicitado) return;
        currentKpiHistoryData = await response.json();
        
        llenarSelectoresKpi();
        actualizarGraficoKpiTemporal();
        actualizarGraficoDonut();
        renderizarTablaUsuarios();
        actualizarTarjetasKpiSuperiores();
        
        try { actualizarLeyendaKpis(); } catch (e) { console.warn("Fallo leyenda:", e); }

    } catch (e) { console.error("Error historial KPI:", e); } 
    finally { if (currentHospitalId === idSolicitado && loader) loader.style.display = 'none'; }
}

// --- 1. GRÁFICO DE EVOLUCIÓN (Líneas/Barras) ---
function seleccionarModoKpi(modo, btn) {
    document.querySelectorAll('.kpi-modo-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const inputModo = document.getElementById('kpi-modo');
    if (inputModo) inputModo.value = modo;
    llenarSelectoresKpi();          
    actualizarGraficoKpiTemporal(); 
}

function seleccionarAgrupacionKpi(agrupacion, btn) {
    document.querySelectorAll('.kpi-agrup-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const inputAgrup = document.getElementById('kpi-agrupacion');
    if (inputAgrup) inputAgrup.value = agrupacion;
    llenarSelectoresKpi();          
    actualizarGraficoKpiTemporal(); 
}

function llenarSelectoresKpi() {
    const modo = document.getElementById('kpi-modo')?.value || 'ris';
    const agrupacion = document.getElementById('kpi-agrupacion')?.value || 'equipo';
    const selectNodo = document.getElementById('kpi-nodo');
    if (!selectNodo) return;

    // --- ¡EL FIX PARA EL BOTÓN TOTAL! ---
    if (agrupacion === 'total') {
        selectNodo.innerHTML = '<option value="todos">Resumen General (Todos)</option>';
        selectNodo.disabled = true; // Lo bloqueamos visualmente
        return; // Salimos de la función para que no busque equipos
    } else {
        selectNodo.disabled = false; // Lo volvemos a habilitar si elige Equipo/Mod
    }
    // ------------------------------------

    const prevVal = selectNodo.value;
    selectNodo.innerHTML = '';
    const opciones = new Set();
    
    if (Array.isArray(currentKpiHistoryData)) {
        currentKpiHistoryData.forEach(d => {
            const metrics = d.application_metrics;
            if (!metrics) return;
            const targetList = (modo === 'ris') ? metrics.ris : metrics.pacs;
            
            if (Array.isArray(targetList)) {
                targetList.forEach(item => {
                    // --- FILTRO GLOBAL: EXCLUIR AET Y MODALIDAD ---
                    const esExcluido = EXCLUDED_AETS.includes(item.aet || item.equipo) || EXCLUDED_MODS.includes(item.mod);
                    if (esExcluido) return;

                    if (agrupacion === 'equipo') {
                        const key = (modo === 'ris') ? item.equipo : item.aet;
                        if (key) opciones.add(key);
                    } else if (agrupacion === 'modalidad' && item.mod) {
                        opciones.add(item.mod);
                    }
                });
            }
        });
    }

    if (opciones.size === 0) {
        selectNodo.add(new Option('Sin datos', ''));
        selectNodo.disabled = true;
        return;
    }

    selectNodo.disabled = false;
    opciones.forEach(o => selectNodo.add(new Option(o, o)));
    if ([...selectNodo.options].some(o => o.value === prevVal)) selectNodo.value = prevVal;
    else selectNodo.selectedIndex = 0; 
}

function actualizarGraficoKpiTemporal() {
    const canvas = document.getElementById('kpiRisChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    
    const modo = document.getElementById('kpi-modo')?.value || 'ris';
    const agrupacion = document.getElementById('kpi-agrupacion')?.value || 'equipo';
    const nodo = document.getElementById('kpi-nodo')?.value;

    // 1. Verificación inicial
    if (!nodo || !Array.isArray(currentKpiHistoryData) || currentKpiHistoryData.length === 0) {
        if (kpiRisChart) kpiRisChart.destroy();
        return;
    }

    const labels = [];
    const dCit = [], dAdm = [], dEje = [], dImg = [], dBor = [], dDef = [], dSus = [], dAlm = [];

    // --- MOTOR DE AGREGACIÓN (TIME BUCKETING) ---
    const aggregatedData = new Map();

    // 2. Iterar, clasificar y sumar en cajas
    currentKpiHistoryData.forEach(d => {
        // EXTRACCIÓN SEGURA AL INICIO
        const metrics = d.application_metrics || {};
        
        // --- LÓGICA DE FECHA REAL DE EXTRACCIÓN ---
        const rawDate = metrics.start_time_extraction || d.timestamp || d.created_at;
        const dateObj = new Date(rawDate || new Date());

        let bucketKey = "";     
        let displayLabel = "";  

        // A. LÓGICA DE AGRUPACIÓN SEGÚN EL RANGO
        if (currentKpiRangeHours <= 168) { 
            // 24H y 7D (Reporte exacto, sin agrupar)
            bucketKey = dateObj.getTime().toString();
            const timeStr = dateObj.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'});
            const dateStr = dateObj.toLocaleDateString([], {day: '2-digit', month: '2-digit'});
            displayLabel = currentKpiRangeHours > 24 ? `${dateStr} ${timeStr}` : timeStr;
            
        } else if (currentKpiRangeHours <= 2160) { 
            // 1M y 3M (Agrupación por DÍA)
            const dayStart = new Date(dateObj.getFullYear(), dateObj.getMonth(), dateObj.getDate());
            bucketKey = dayStart.getTime().toString();
            displayLabel = dayStart.toLocaleDateString([], {day: '2-digit', month: '2-digit'});
            
        } else if (currentKpiRangeHours <= 8760) { 
            // 1A (Agrupación por SEMANA, comenzando en Lunes)
            const day = dateObj.getDay() || 7; 
            const monday = new Date(dateObj.getFullYear(), dateObj.getMonth(), dateObj.getDate() - day + 1);
            bucketKey = monday.getTime().toString();
            displayLabel = monday.toLocaleDateString([], {day: '2-digit', month: '2-digit', year: '2-digit'});
            
        } else { 
            // 5A (Agrupación por MES)
            const monthStart = new Date(dateObj.getFullYear(), dateObj.getMonth(), 1);
            bucketKey = monthStart.getTime().toString();
            displayLabel = monthStart.toLocaleDateString([], {month: 'short', year: 'numeric'}).toUpperCase(); 
        }

        // B. CREAR LA CAJA SI NO EXISTE
        if (!aggregatedData.has(bucketKey)) {
            aggregatedData.set(bucketKey, {
                label: displayLabel,
                timestamp: parseInt(bucketKey),
                sCit: 0, sAdm: 0, sEje: 0, sImg: 0, sBor: 0, sDef: 0, sSus: 0, sAlm: 0,
                hasData: false
            });
        }

        const bucket = aggregatedData.get(bucketKey);

        // C. SUMATORIA DE DATOS EN LA CAJA
        if (Object.keys(metrics).length > 0) {
            const list = (modo === 'ris') ? metrics.ris : metrics.pacs;
            
            if (Array.isArray(list)) {
                list.forEach(item => {
                    // Filtro Global
                    if (EXCLUDED_AETS.includes(item.aet || item.equipo) || EXCLUDED_MODS.includes(item.mod)) return;

                    // NUEVA LÓGICA: Si es "total", siempre da true. Si no, filtra por equipo o modalidad.
                    const isMatch = (agrupacion === 'total') || 
                                    (agrupacion === 'equipo' && (item.equipo === nodo || item.aet === nodo)) || 
                                    (agrupacion === 'modalidad' && item.mod === nodo);
                    
                    if (isMatch) {
                        if (modo === 'ris') {
                            bucket.sCit += (item.citados || 0); 
                            bucket.sAdm += (item.admitidos || 0); 
                            bucket.sEje += (item.ejecutados || 0);
                            bucket.sImg += (item.con_imagen || 0); 
                            bucket.sBor += (item.borradores || 0); 
                            bucket.sDef += (item.definitivos || 0);
                            bucket.sSus += (item.suspendidos || 0);
                        } else {
                            bucket.sAlm += (item.almacenados || 0);
                        }
                        bucket.hasData = true; 
                    }
                });
            }
        }
    });

    // 3. Ordenar cronológicamente y pasar a los arrays de Chart.js
    const sortedBuckets = Array.from(aggregatedData.values()).sort((a, b) => a.timestamp - b.timestamp);

    sortedBuckets.forEach(b => {
        labels.push(b.label);
        dCit.push(b.hasData ? b.sCit : 0); 
        dAdm.push(b.hasData ? b.sAdm : 0); 
        dEje.push(b.hasData ? b.sEje : 0);
        dImg.push(b.hasData ? b.sImg : 0); 
        dBor.push(b.hasData ? b.sBor : 0); 
        dDef.push(b.hasData ? b.sDef : 0);
        dSus.push(b.hasData ? b.sSus : 0); 
        dAlm.push(b.hasData ? b.sAlm : 0);
    });

    // 4. Dibujar el Gráfico
    if (kpiRisChart) kpiRisChart.destroy();
    
    // 4. Dibujar el Gráfico
    if (kpiRisChart) kpiRisChart.destroy();
    
    const datasets = (modo === 'ris') ? [
        { label: 'Citados', data: dCit, backgroundColor: '#cce5ff', stack: 's0' },
        { label: 'Admitidos', data: dAdm, backgroundColor: '#99ccff', stack: 's0' },
        { label: 'Ejecutados', data: dEje, backgroundColor: '#66b2ff', stack: 's0' },
        { label: 'Asociados', data: dImg, backgroundColor: '#3399ff', stack: 's0' }, // <--- CAMBIO DE NOMBRE AQUÍ
        { label: 'Borradores', data: dBor, backgroundColor: '#0080ff', stack: 's0' },
        { label: 'Definitivos', data: dDef, backgroundColor: '#0066cc', stack: 's0' },
        { label: 'Suspendidos', data: dSus, backgroundColor: '#004c99', stack: 's0' }
    ] : [
        { label: 'Almacenados', data: dAlm, backgroundColor: '#004c99', borderRadius: 4 }
    ];

    // --- NUEVO: PLUGIN VISUAL PARA MARCAR CONSULTAS EN CERO ---
    const zeroActivityPlugin = {
        id: 'zeroActivityPlugin',
        afterDatasetsDraw: (chart) => {
            const { ctx, scales: { x, y } } = chart;
            ctx.save();
            ctx.textAlign = 'center';
            ctx.textBaseline = 'bottom';
            const isDark = document.body.classList.contains('dark-theme');
            ctx.fillStyle = isDark ? 'rgba(149, 165, 166, 0.6)' : 'rgba(149, 165, 166, 0.9)'; // Gris discreto
            ctx.font = 'bold 12px sans-serif';

            const datasetCount = chart.data.datasets.length;
            const meta = chart.getDatasetMeta(0);
            if (!meta || !meta.data) {
                ctx.restore();
                return;
            }

            // Recorremos todas las columnas (etiquetas)
            for (let i = 0; i < chart.data.labels.length; i++) {
                let total = 0;
                for (let j = 0; j < datasetCount; j++) {
                    total += chart.data.datasets[j].data[i] || 0;
                }

                // Si la suma de todo es 0, dibujamos un indicador
                if (total === 0 && meta.data[i]) {
                    const xPos = meta.data[i].x;
                    const yPos = y.getPixelForValue(0);
                    // Dibuja un "0" sutil justo arriba de la línea de base
                    ctx.fillText('0', xPos, yPos - 2);
                }
            }
            ctx.restore();
        }
    };

    kpiRisChart = new Chart(ctx, {
        type: 'bar', 
        data: { labels, datasets },
        options: {
            responsive: true, 
            maintainAspectRatio: false,
            interaction: { intersect: false, mode: 'index' },
            scales: { 
                x: { stacked: true, grid: { display: false } }, 
                y: { stacked: true, beginAtZero: true, ticks: { precision: 0 } } 
            },
            plugins: { 
                legend: { position: 'top', labels: { boxWidth: 12, usePointStyle: true } } 
            },
            animation: { duration: 400 }
        },
        plugins: [zeroActivityPlugin] // <-- INYECTAMOS EL NUEVO PLUGIN AQUÍ
    });
}

// --- 2. GRÁFICO DE DONA (Distribución Total) ---
function seleccionarModoDonut(modo, btn) {
    document.querySelectorAll('.donut-modo-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const inputModo = document.getElementById('donut-modo');
    if (inputModo) inputModo.value = modo;
    actualizarGraficoDonut(); 
}

function seleccionarAgrupacionDonut(agrupacion, btn) {
    document.querySelectorAll('.donut-agrup-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const inputAgrup = document.getElementById('donut-agrupacion');
    if (inputAgrup) inputAgrup.value = agrupacion;
    actualizarGraficoDonut(); 
}

function actualizarGraficoDonut() {
    const canvas = document.getElementById('kpiDonutChart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const modo = document.getElementById('donut-modo')?.value || 'ris';
    const agrupacion = document.getElementById('donut-agrupacion')?.value || 'equipo';
    const titulo = document.getElementById('titulo-dona');
    if (titulo) titulo.innerText = (modo === 'ris') ? '📊 Órdenes Creadas' : '📊 Estudios Almacenados';

    if (!Array.isArray(currentKpiHistoryData) || currentKpiHistoryData.length === 0) {
        if (kpiDonutChart) kpiDonutChart.destroy();
        return;
    }

    const isDark = document.body.classList.contains('dark-theme');
    const colorSeparador = isDark ? '#233043' : '#ffffff';

    const acumulador = {}; let granTotal = 0;
    currentKpiHistoryData.forEach(d => {
        const metrics = d.application_metrics;
        if (!metrics) return;
        const list = (modo === 'ris') ? metrics.ris : metrics.pacs;
        if (Array.isArray(list)) {
            list.forEach(item => {
                // --- FILTRO GLOBAL ---
                if (EXCLUDED_AETS.includes(item.aet || item.equipo) || EXCLUDED_MODS.includes(item.mod)) return;

                const key = (agrupacion === 'equipo') ? (item.equipo || item.aet || 'Desc') : (item.mod || 'Desc');
                const val = (modo === 'ris') ? (item.totales || 0) : (item.almacenados || 0);
                acumulador[key] = (acumulador[key] || 0) + val;
                granTotal += val;
            });
        }
    });

    const labels = Object.keys(acumulador);
    const data = Object.values(acumulador);
    const bgColors = ['#004c99', '#0066cc', '#0080ff', '#3399ff', '#66b2ff', '#99ccff', '#cce5ff', '#003366', '#1a8cff', '#4da6ff', '#0059b3', '#e6f2ff'];

    if (kpiDonutChart) kpiDonutChart.destroy();
    kpiDonutChart = new Chart(ctx, {
        type: 'doughnut',
        data: { labels, datasets: [{ data, backgroundColor: bgColors.slice(0, labels.length), borderWidth: 2, borderColor: colorSeparador, hoverOffset: 6 }] },
        options: {
            responsive: true, maintainAspectRatio: false, cutout: '74%',
            plugins: {
                legend: { position: 'right', labels: { boxWidth: 12, font: { size: 11 }, usePointStyle: true } },
                tooltip: { callbacks: { label: (ctx) => `${ctx.label}: ${ctx.raw.toLocaleString('es-AR')} (${granTotal>0?((ctx.raw/granTotal)*100).toFixed(1):0}%)` } }
            }
        },
        plugins: [{
            id: 'textoCentro',
            beforeDraw: (chart) => {
                const {ctx, width, height} = chart; const centerX = chart.getDatasetMeta(0).data[0]?.x, centerY = chart.getDatasetMeta(0).data[0]?.y;
                if(!centerX) return;
                ctx.save(); ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
                const isDark = document.body.classList.contains('dark-theme');
                ctx.font = 'bold 12px sans-serif'; ctx.fillStyle = '#7f8c8d'; ctx.fillText('TOTAL', centerX, centerY - 15);
                ctx.font = '800 32px sans-serif'; ctx.fillStyle = isDark ? '#f8fafc' : '#2c3e50';
                ctx.fillText(granTotal.toLocaleString('es-AR'), centerX, centerY + 15); ctx.restore();
            }
        }]
    });
}

// --- 3. PANEL DE USUARIOS ---
function obtenerIconoRol(rol) {
    const r = (rol || '').toLowerCase();
    if (r === 'administrator' || r === 'admin') return '👑';
    if (r.includes('administrative')) return '💼';
    if (r.includes('nurse')) return '🩺';
    if (r.includes('reporter') || r.includes('informe')) return '📝';
    if (r.includes('requesting') || r.includes('solicitante')) return '📥';
    if (r.includes('technician') || r.includes('tecnico')) return '⚙️';
    if (r.includes('physician') || r.includes('medico')) return '⚕️';
    return '👤'; 
}

function renderizarTablaUsuarios() {
    const tbody = document.getElementById('tabla-usuarios-body');
    if (!tbody) return;
    tbody.innerHTML = '';

    if (currentKpiHistoryData.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding: 20px; color: #7f8c8d; font-style: italic;">Sin datos en este período</td></tr>';
        return;
    }

    const acumulador = {};
    
    currentKpiHistoryData.forEach(d => {
        if (d.application_metrics && d.application_metrics.users) {
            d.application_metrics.users.forEach(u => {
                const rol = u.rol || 'Desconocido';
                
                // Agregamos 'count' para saber por cuánto dividir después
                if (!acumulador[rol]) {
                    acumulador[rol] = { unicos_sum: 0, logueos: 0, count: 0 };
                }
                
                acumulador[rol].unicos_sum += (u.usuarios_unicos || 0);
                acumulador[rol].logueos += (u.inicios_sesion || 0);
                acumulador[rol].count += 1;
            });
        }
    });

    const roles = Object.keys(acumulador).sort((a, b) => acumulador[b].logueos - acumulador[a].logueos);

    if (roles.length === 0) {
        tbody.innerHTML = '<tr><td colspan="3" style="text-align:center; padding: 20px; color: #7f8c8d; font-style: italic;">Sin actividad de usuarios</td></tr>';
        return;
    }

    roles.forEach(rol => {
        const datos = acumulador[rol];
        const icono = obtenerIconoRol(rol);
        
        // LÓGICA V4: Calculamos el promedio y lo redondeamos a número entero
        const promedioUnicos = Math.round(datos.unicos_sum / datos.count);

        const tr = document.createElement('tr');
        tr.style.borderBottom = "1px solid #f1f5f8";
        tr.innerHTML = `
            <td style="padding: 12px 10px; display: flex; align-items: center; gap: 10px;">
                <span style="font-size: 1.3em; background: #f8f9fa; border-radius: 50%; width: 30px; height: 30px; display: flex; align-items: center; justify-content: center;">${icono}</span>
                <span style="font-weight: 600; color: #2c3e50;">${rol}</span>
            </td>
            <td style="padding: 12px 10px; text-align: right; font-weight: 500; color: #7f8c8d;">${promedioUnicos.toLocaleString('es-AR')}</td>
            <td style="padding: 12px 10px; text-align: right; font-weight: bold; color: #3498db;">${datos.logueos.toLocaleString('es-AR')}</td>
        `;
        tbody.appendChild(tr);
    });
}

// ==========================================
// --- MODO OSCURO (TOGGLE) ---
// ==========================================
function toggleTema() {
    const body = document.body;
    body.classList.toggle('dark-theme');
    const esOscuro = body.classList.contains('dark-theme');
    localStorage.setItem('temaUI', esOscuro ? 'oscuro' : 'claro');
    
    const icon = document.getElementById('theme-icon');
    const iconMob = document.getElementById('theme-icon-mobile');
    if(icon) icon.innerText = esOscuro ? '☀️' : '🌙';
    if(iconMob) iconMob.innerText = esOscuro ? '☀️' : '🌙';

    Chart.defaults.color = esOscuro ? '#94a3b8' : '#666';
    
    if (currentHospitalId) {
        if(myChart) myChart.update();
        if(kpiRisChart) kpiRisChart.update();
        if(kpiDonutChart) kpiDonutChart.update();
    } else if(document.getElementById('view-dashboard').classList.contains('active')) {
        aplicarFiltros(); 
    }
}

function actualizarLeyendaKpis() {
    const legendEl = document.getElementById('kpi-legend');
    if (!legendEl) return;

    if (!currentKpiHistoryData || currentKpiHistoryData.length === 0) {
        legendEl.innerText = "Sincronizando información de uso...";
        return;
    }

    // 1. Buscar intervalo (con seguridad para evitar el 'null')
    let intervalo = 1; 
    const registroConDato = [...currentKpiHistoryData].reverse().find(d => d.application_metrics && d.application_metrics.extraction_interval_hours);
    
    if (registroConDato && registroConDato.application_metrics.extraction_interval_hours) {
        intervalo = registroConDato.application_metrics.extraction_interval_hours;
    }

    // 2. Obtener fecha del primer registro basada en la extracción real
    const primerReg = currentKpiHistoryData[0];
    const metrics = primerReg.application_metrics || {};
    const timestamp = metrics.start_time_extraction || primerReg.timestamp || primerReg.created_at;
    
    let fechaStr = "---";
    if (timestamp) {
        const d = new Date(timestamp);
        // ACÁ AGREGAMOS EL AÑO ('numeric') Y USAMOS toLocaleString
        fechaStr = d.toLocaleString('es-AR', { 
            day: '2-digit', 
            month: '2-digit', 
            year: 'numeric', 
            hour: '2-digit', 
            minute: '2-digit' 
        });
    }

    const textoHoras = intervalo === 1 ? "hora" : "horas";
    legendEl.innerHTML = `Datos tomados desde el <b style="color:var(--primary)">${fechaStr} hs</b> cada <b style="color:var(--primary)">${intervalo} ${textoHoras}</b>`;
}

// --- LÓGICA DE PESTAÑAS (VISTA DETALLE) ---
function switchTab(tabId, btn) {
    // 1. Quitar la clase 'active' de todos los botones de las pestañas
    const tabBtns = document.querySelectorAll('#view-detalle .tab-btn');
    tabBtns.forEach(b => b.classList.remove('active'));
    
    // 2. Darle la clase 'active' al botón que el usuario acaba de clickear
    if (btn) btn.classList.add('active');
    
    // 3. Ocultar todos los contenedores de contenido
    const tabContents = document.querySelectorAll('#view-detalle .tab-content');
    tabContents.forEach(c => c.classList.remove('active'));
    
    // 4. Mostrar solo el contenedor que corresponde al botón clickeado
    const targetTab = document.getElementById(`tab-${tabId}`);
    if (targetTab) targetTab.classList.add('active');

    // 5. FIX: Forzar redibujado de Chart.js al volver a hacer visible el contenedor
    setTimeout(() => {
        if (tabId === 'infra' && typeof myChart !== 'undefined' && myChart) {
            myChart.resize();
            myChart.update();
        } else if (tabId === 'kpis') {
            if (typeof kpiRisChart !== 'undefined' && kpiRisChart) {
                kpiRisChart.resize();
                kpiRisChart.update();
            }
            if (typeof kpiDonutChart !== 'undefined' && kpiDonutChart) {
                kpiDonutChart.resize();
                kpiDonutChart.update();
            }
        }
    }, 50); // Un delay minúsculo para asegurar que el DOM ya aplicó el display: block
}

function abrirModalPassword() {
    document.getElementById('modal-password').style.display = 'flex';
}

function cerrarModalPassword() {
    document.getElementById('modal-password').style.display = 'none';
    document.getElementById('pw-actual').value = '';
    document.getElementById('pw-nueva').value = '';
}

async function ejecutarCambioPassword() {
    const userData = sessionStorage.getItem('tecnomonitor_user');
    const user = userData ? JSON.parse(userData) : null;
    
    const current_password = document.getElementById('pw-actual').value;
    const new_password = document.getElementById('pw-nueva').value;

    // Validación básica de seguridad
    if (!user || !user.email) {
        alert("⚠️ Error de sesión. Por favor, cierra sesión y vuelve a entrar.");
        return;
    }

    if (!current_password || !new_password) {
        alert("Por favor, completa ambos campos.");
        return;
    }

    // LOG DE DEPURACIÓN: Pulsa F12 en tu navegador para ver esto
    console.log("Enviando cambio para:", user.email);

    try {
        const response = await authFetch('/api/user/change-password', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                email: user.email,             // Debe coincidir con el Backend
                current_password: current_password, // Debe coincidir con el Backend
                new_password: new_password        // Debe coincidir con el Backend
            })
        });

        if (response.ok) {
            alert("✅ ¡Contraseña cambiada! Debes volver a iniciar sesión.");
            logout(); 
        } else {
            const errorData = await response.json();
            // El error 422 detallado aparecerá aquí
            console.error("Error 422 detalles:", errorData);
            alert("❌ Error: " + (errorData.detail?.[0]?.msg || "Datos inválidos"));
        }
    } catch (e) {
        alert("❌ Error de red.");
    }
}

async function logout() {
    // 🛡️ Llamamos al servidor para que destruya la cookie
    await fetch('/api/logout', { method: 'POST' });
    sessionStorage.clear();
    window.location.href = '/';
}

function actualizarResumenDashboard(data) {
    let total = data.length;
    let online = 0;
    let offline = 0;
    let nodos = 0;
    const ahora = new Date();

    data.forEach(h => {
        // Calculamos el tiempo para cada hospital igual que en la tabla
        const diffMinutos = Math.floor((ahora - new Date(h.timestamp)) / 60000); 

        // Lógica Binaria: Solo cuenta como Online si está dentro del tiempo límite
        if (!isNaN(diffMinutos) && diffMinutos <= limitOfflineMinutes) {
            online++;
        } else {
            offline++;
        }
        
        // Sumamos los elementos (VMs + Server)
        if (h.elements && Array.isArray(h.elements)) {
            nodos += h.elements.filter(e => !e.label.startsWith('+')).length;
        }
    });

    // Actualizamos el HTML de las tarjetas
    const elTotal = document.getElementById('sum-total');
    const elOnline = document.getElementById('sum-online');
    const elOffline = document.getElementById('sum-offline');
    const elNodos = document.getElementById('sum-elementos');

    if(elTotal) elTotal.innerText = total;
    if(elOnline) elOnline.innerText = online;
    if(elOffline) elOffline.innerText = offline;
    if(elNodos) elNodos.innerText = nodos;
}

// ==========================================
// --- MÓDULO MAPA (RESUMEN EJECUTIVO) ---
// ==========================================

function initMapaDashboard() {
    if (mapDashInstance) {
        cargarDatosMapaDashboard();
        return;
    }

    // Inicializar mapa (Vista Argentina)
    mapDashInstance = L.map('map-dashboard-container', { zoomControl: true }).setView([-38.4161, -63.6167], 4);
    
    // Capa visual
    L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; OpenStreetMap &copy; CARTO',
        subdomains: 'abcd',
        maxZoom: 19
    }).addTo(mapDashInstance);

    mapDashMarkers = L.layerGroup().addTo(mapDashInstance);
    
    setTimeout(() => mapDashInstance.invalidateSize(), 100);
    cargarDatosMapaDashboard();
}

async function cargarDatosMapaDashboard() {
    try {
        const res = await authFetch('/api/mapa-data');
        mapData = await res.json(); 
        
        // Reutilizamos mapData que es global y ordenamos
        mapData.sort((a, b) => b.lat - a.lat);
        renderizarMarcadoresDash();
    } catch (e) { console.error("Error mapa dashboard:", e); }
}

function renderizarMarcadoresDash() {
    if (!mapDashInstance || !mapDashMarkers) return;
    mapDashMarkers.clearLayers(); 

    mapData.forEach(h => {
        const color = h.status === 'Online' ? '#2ecc71' : '#e74c3c';
        
        // Filtro de estados
        if (currentDashMapFilter === 'online' && h.status !== 'Online') return; 
        if (currentDashMapFilter === 'offline' && h.status === 'Online') return;

        const marker = L.circleMarker([h.lat, h.lng], {
            radius: 8,
            fillColor: color,
            color: "#fff",
            weight: 2,
            opacity: 1,
            fillOpacity: 0.9
        });

        // Popup idéntico al mapa original
        marker.bindPopup(`
            <div style="text-align:center; font-family:sans-serif; min-width: 120px;">
                <b style="color:#2c3e50; font-size:1.1em;">${h.nombre}</b><br>
                <small style="color:#7f8c8d; font-weight:bold;">${h.id}</small><br>
                <div style="margin-top:5px; margin-bottom:8px;">
                    <span style="background:${color}20; color:${color}; padding:2px 8px; border-radius:10px; font-weight:bold; font-size:0.85em;">● ${h.status}</span>
                </div>
                <button onclick="verDetalle('${h.id}')" style="background:#3498db; color:white; border:none; border-radius:4px; padding:6px 12px; cursor:pointer; font-size:0.85em; width:100%;">Ver Detalles</button>
            </div>
        `);

        mapDashMarkers.addLayer(marker);
    });
}

// Variables para el recorrido del Dashboard
let tourDashInterval = null;
let tourDashIndex = 0;

function siguienteDestinoDash() {
    // Filtramos los datos igual que en los marcadores para no visitar hospitales ocultos
    const filteredData = mapData.filter(h => {
        if (currentDashMapFilter === 'online' && h.status !== 'Online') return false;
        if (currentDashMapFilter === 'offline' && h.status === 'Online') return false;
        return true;
    });

    if (filteredData.length === 0) return;

    // Aseguramos que el índice no se salga del array filtrado
    tourDashIndex = tourDashIndex % filteredData.length;
    const h = filteredData[tourDashIndex];
    
    // Vuelo suave al destino
    mapDashInstance.flyTo([h.lat, h.lng], 9, {
        animate: true,
        duration: 4 
    });

    // Abrir el popup exacto al llegar
    setTimeout(() => {
        mapDashMarkers.eachLayer(layer => {
            const latlng = layer.getLatLng();
            if (Math.abs(latlng.lat - h.lat) < 0.0001 && Math.abs(latlng.lng - h.lng) < 0.0001) {
                layer.openPopup();
            }
        });
    }, 4500);

    tourDashIndex++;
}

function toggleTourDash() {
    const btn = document.getElementById('btn-tour-dash');
    
    if (tourDashInterval) {
        // Detener recorrido
        clearInterval(tourDashInterval);
        tourDashInterval = null;
        if (btn) {
            btn.innerHTML = "▶ INICIAR RECORRIDO";
            btn.classList.remove('active');
        }
        // Alejar la cámara a vista general
        mapDashInstance.flyTo([-38.4161, -63.6167], 4, { duration: 2 });
    } else {
        // Iniciar recorrido
        tourDashIndex = 0;
        siguienteDestinoDash(); 
        tourDashInterval = setInterval(siguienteDestinoDash, 10000); // 10 segundos por punto
        if (btn) {
            btn.innerHTML = "■ DETENER";
            btn.classList.add('active');
        }
    }
}

async function cargarEstadoSoftware(idSolicitado) {
    if (currentHospitalId !== idSolicitado) return;
    const container = document.getElementById('logs-container');
    const legend = document.getElementById('software-legend');
    
    if (legend) legend.innerText = 'Sincronizando información de integraciones...';
    if (container) container.innerHTML = '<div style="text-align:center; padding: 40px; color:#7f8c8d;">Cargando estado de canales...</div>';
    
    try {
        // Ahora pasamos la variable global currentSoftwareMinutes en la URL
        const res = await authFetch(`/api/hospital/${idSolicitado}/software?minutos=${currentSoftwareMinutes}`);
        const data = await res.json();
        renderizarSoftware(data);
    } catch(e) {
        console.error("Error cargando software:", e);
        if (legend) legend.innerText = '⚠️ Error de conexión al servidor';
        if (container) container.innerHTML = '<div style="text-align:center; padding: 40px; color:#e74c3c;">Error al cargar los datos de integraciones.</div>';
    }
}

function renderizarSoftware(data) {
    const container = document.getElementById('logs-container');
    const legend = document.getElementById('software-legend');
    if (!container) return;
    
    // 1. LEYENDA DINÁMICA
    if (legend && data.metadata) {
        let textoTiempo = '';
        if (data.metadata.minutos === 0) textoTiempo = 'Total Histórico';
        else if (data.metadata.minutos === 30) textoTiempo = 'los últimos 30 minutos';
        else if (data.metadata.minutos === 60) textoTiempo = 'la última hora';
        else if (data.metadata.minutos === 1440) textoTiempo = 'las últimas 24 horas';
        else if (data.metadata.minutos === 10080) textoTiempo = 'los últimos 7 días';
        
        if (data.metadata.minutos === 0) {
            legend.innerHTML = `Tráfico <b style="color:#27ae60">${textoTiempo}</b> acumulado desde el último reinicio del servicio.`;
        } else if (!data.metadata.is_historical) {
            legend.innerHTML = `⚠️ Sin historial suficiente en este período. Mostrando solo valores actuales.`;
        } else {
            legend.innerHTML = `Tráfico de mensajes cursados en <b style="color:var(--primary)">${textoTiempo}</b>`;
        }
    }

    // 2. RENDER DE TARJETAS (Sin cambios importantes respecto a la versión anterior)
    container.innerHTML = '';
    let html = '';
    
    if (!data || !data.mirth || Object.keys(data.mirth).length === 0) {
        container.innerHTML = `
            <div style="padding: 60px 20px; text-align: center; color: #7f8c8d;">
                <h3 style="margin-top: 20px;">Sin Integraciones Reportadas</h3>
                <p style="font-style: italic;">Este hospital no tiene canales de Mirth monitoreados actualmente.</p>
            </div>
        `;
        return;
    }

    const isDark = document.body.classList.contains('dark-theme');
    const theadBg = isDark ? 'transparent' : '#fdfdfd';

    Object.keys(data.mirth).forEach(instancia => {
        const canales = data.mirth[instancia];
        let tablaHtml = `
            <div class="card" style="padding: 0; overflow: hidden; margin-bottom: 25px; border-top: 4px solid #f39c12;">
                <div style="padding: 15px 20px; border-bottom: 1px solid #eee; display:flex; align-items:center; gap: 10px;" class="detail-card-header">
                    <span style="font-size: 1.5em;">🔄</span>
                    <h3 style="margin:0; font-size:1.1em; color:#2c3e50;">Mirth Connect: <span style="color: #f39c12;">${instancia}</span></h3>
                </div>
                <div class="table-container-island" style="margin:0; padding: 0; box-shadow: none; border-radius: 0;">
                    <table class="table-clean" style="margin:0; width:100%;">
                        <thead style="background: ${theadBg}; border-bottom: 2px solid #eee;">
                            <tr>
                                <th style="padding: 12px 20px;">Canal</th>
                                <th style="padding: 12px 20px; text-align: center;">Estado</th>
                                <th style="padding: 12px 20px; text-align: right;">Recibidos</th>
                                <th style="padding: 12px 20px; text-align: right;">Enviados</th>
                                <th style="padding: 12px 20px; text-align: right;">Encolados</th>
                                <th style="padding: 12px 20px;">Último Error</th>
                            </tr>
                        </thead>
                        <tbody>
        `;
        
        canales.forEach(c => {
            const status = (c.status || '').toUpperCase();
            let statusColor = '#95a5a6'; let statusBg = 'rgba(149, 165, 166, 0.15)';
            if (status === 'STARTED') { statusColor = '#27ae60'; statusBg = 'rgba(39, 174, 96, 0.15)'; }
            else if (status === 'STOPPED') { statusColor = '#e74c3c'; statusBg = 'rgba(231, 76, 60, 0.15)'; }
            else if (status === 'PAUSED') { statusColor = '#f39c12'; statusBg = 'rgba(243, 156, 18, 0.15)'; }
            else if (status === 'ERROR') { statusColor = '#c0392b'; statusBg = 'rgba(192, 57, 43, 0.15)'; }
            
            const queuedStyle = c.queued > 0 ? 'color: #e74c3c; font-weight: bold; background: rgba(231, 76, 60, 0.15); padding: 2px 8px; border-radius: 10px;' : 'color: #7f8c8d;';
            const valRecibidos = c.received !== undefined ? c.received.toLocaleString('es-AR') : '-';
            const valEnviados = c.sent !== undefined ? c.sent.toLocaleString('es-AR') : '-';

            tablaHtml += `
                <tr style="border-bottom: 1px solid #f1f5f8;">
                    <td style="padding: 12px 20px; font-weight: 600; color: #2c3e50;">${c.channel}</td>
                    <td style="padding: 12px 20px; text-align: center;">
                        <span style="color: ${statusColor}; background: ${statusBg}; padding: 4px 10px; border-radius: 12px; font-size: 0.85em; font-weight: bold; border: 1px solid ${statusColor}40;">${status}</span>
                    </td>
                    <td style="padding: 12px 20px; text-align: right; color: #3498db; font-weight: 500;">${valRecibidos}</td>
                    <td style="padding: 12px 20px; text-align: right; color: #2ecc71; font-weight: 500;">${valEnviados}</td>
                    <td style="padding: 12px 20px; text-align: right;">
                        <span style="${queuedStyle}">${c.queued.toLocaleString('es-AR')}</span>
                    </td>
                    <td style="padding: 12px 20px; color: #e74c3c; font-size: 0.85em;">${c.last_error || '-'}</td>
                </tr>
            `;
        });
        tablaHtml += `</tbody></table></div></div>`;
        html += tablaHtml;
    });
    container.innerHTML = html;
}

function cambiarRangoSoftware(minutos, btn) {
    document.querySelectorAll('.sw-time-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentSoftwareMinutes = minutos;
    cargarEstadoSoftware(currentHospitalId);
}

function actualizarTarjetasKpiSuperiores() {
    const topCards = document.getElementById('kpi-top-cards');
    if (!topCards) return;

    // Si no hay datos, ocultamos las tarjetas
    if (!Array.isArray(currentKpiHistoryData) || currentKpiHistoryData.length === 0) {
        topCards.style.display = 'none';
        return;
    }

    topCards.style.display = 'grid';

    let sumAdmitidos = 0;
    let sumConImagen = 0;
    let sumEjecutados = 0;
    let sumDefinitivos = 0;

    // Sumarizamos todos los registros del periodo seleccionado
    currentKpiHistoryData.forEach(d => {
        const metrics = d.application_metrics;
        if (!metrics || !Array.isArray(metrics.ris)) return;

        metrics.ris.forEach(item => {
            // Filtramos AETs o Modalidades excluidas globalmente
            if (EXCLUDED_AETS.includes(item.aet || item.equipo) || EXCLUDED_MODS.includes(item.mod)) return;

            sumAdmitidos += (item.admitidos || 0);
            sumConImagen += (item.con_imagen || 0);
            sumEjecutados += (item.ejecutados || 0);
            sumDefinitivos += (item.definitivos || 0);
        });
    });

    // 1. Cálculo Tarjeta Asociación (Imágenes vs Admitidos)
    let tasaAsocRaw = sumAdmitidos > 0 ? (sumConImagen / sumAdmitidos) * 100 : 0;
    const tasaAsoc = Math.min(100, tasaAsocRaw).toFixed(1); // El techo de 100%
    document.getElementById('kpi-card-tasa-val').innerText = `${tasaAsoc}%`;
    document.getElementById('kpi-card-assoc-val').innerText = sumConImagen.toLocaleString('es-AR');

    // 2. Cálculo Tarjeta Completitud (Definitivos vs Ejecutados)
    let tasaInfRaw = sumEjecutados > 0 ? (sumDefinitivos / sumEjecutados) * 100 : 0;
    const tasaInf = Math.min(100, tasaInfRaw).toFixed(1); // El techo de 100%
    document.getElementById('kpi-card-comp-val').innerText = `${tasaInf}%`;
    document.getElementById('kpi-card-def-val').innerText = sumDefinitivos.toLocaleString('es-AR');
}