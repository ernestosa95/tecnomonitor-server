# Arquitectura

## 1. Visión general

TecnoMonitor es un sistema de monitoreo centralizado: agentes instalados en cada hospital
envían reportes periódicos (estado de hardware, VMs, software clínico, KPIs de uso) a un
único servidor central, que los guarda, evalúa reglas de alertas y expone un dashboard web
para el equipo interno y para clientes externos.

```
Agentes en hospitales (fuera de este repo)
        │  POST JSON
        ▼
┌────────────────────────── server.py (proceso único, uvicorn, puerto 8001) ──────────────────────────┐
│                                                                                                        │
│  master_app = FastAPI (lifespan)                                                                      │
│   ├─ include_router(listener_app.router)   ← rutas de main.py  (/v1/hospital-status, /api/admin/...)  │
│   ├─ include_router(informes_ia_router)    ← opcional, si el paquete externo informes_ia carga bien   │
│   ├─ mount("/", dashboard_app)             ← app completa de dashboard_app/ (~74 rutas, ver §2)        │
│   ├─ WS  /ws/alertas                       ← broadcast a clientes conectados                          │
│   └─ background task: ciclo_vigilancia()   ← loop infinito, tick cada 60s                             │
│        ├─ alerts_engine.procesar_offline()                                                            │
│        ├─ alerts_engine.verificar_kpis_programados()                                                  │
│        ├─ alerts_engine.verificar_estado_software()                                                   │
│        ├─ cada 24h: maintenance.ejecutar_mantenimiento()      (asyncio.to_thread)                     │
│        └─ cada 12h: limpiar_alertas.limpiar_alertas_huerfanas() (asyncio.to_thread)                    │
│                                                                                                        │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
        │                                   │                              │
        ▼                                   ▼                              ▼
  SQLite (monitor_hospitales.db,      Asana API (tickets            Gemini/paquete informes_ia
  WAL mode, un solo archivo)          de alertas)                   (externo al repo, genera PDFs)
```

Todo corre como **un único proceso Python**. No hay separación de servicios, cola de
mensajería ni base de datos gestionada: es una app monolítica que se despliega con
`uvicorn server:master_app` detrás de un Nginx local (ver `proxy_headers=True,
forwarded_allow_ips="127.0.0.1"` en `server.py`).

## 2. Componentes principales

| Componente | Archivo(s) | Responsabilidad |
|---|---|---|
| Listener de ingesta | `main.py`, `transformer.py`, `schemas.py` | Recibe reportes de agentes (v2/v3/v4), normaliza formatos legacy y persiste. |
| Dashboard web | `dashboard_app/dashboard.py` (195 líneas) + `dashboard_app/core.py` + `dashboard_app/routers/*.py` (~74 rutas en total) | Login, vistas HTML, APIs de datos, ABM de hospitales/usuarios/clientes, reportes PDF, panel de incidentes. Ver detalle de la reorganización en §2bis. |
| Auth & permisos | `dashboard_app/auth.py`, `dashboard_app/permissions.py` | JWT + bcrypt + matriz de roles centralizada (fuente única de verdad). Distinto de `dashboard_app/routers/auth.py` (router de login/sesión) — mismo nombre base, módulos distintos. |
| Motor de alertas | `dashboard_app/alerts_engine/` (paquete: `config.py`, `estado.py`, `exclusiones.py`, `infra.py`, `kpis_negocio/`, `software/`, `orquestador.py` — antes un único archivo de 1226 líneas) | Evalúa umbrales de hardware/KPIs/software y abre/cierra tickets en Asana. Reorganizado por dominio con un orquestador central — ver [09-plan-refactor-alertas.md](09-plan-refactor-alertas.md). |
| Conector Asana | `dashboard_app/asana_conector.py` | Wrapper del SDK de Asana, con circuit breaker si falta el token. |
| Generador de reportes | `dashboard_app/generator_report.py` (1215 líneas) | Arma PDFs (reportlab) y gráficos (matplotlib) de infraestructura/KPIs. |
| Informes con IA | `dashboard_app/informes_ia_router.py` + paquete externo `informes_ia` | Router aislado que delega en un paquete que vive **fuera de este repo** (`/home/tecnoxaas/Documents/informes_ia_proyectos/`); si no carga, responde 503 sin tumbar el resto de la app. |
| Mantenimiento de DB | `maintenance.py` | Purga/compacta datos históricos periódicamente. |
| Limpieza de alertas | `dashboard_app/limpiar_alertas.py` | Cierra alertas huérfanas (sin reporte reciente del hospital). |
| Scripts operativos | raíz + `Accesorios/` | ETL, migraciones de esquema, exportaciones puntuales — ver [06-operaciones-y-scripts.md](06-operaciones-y-scripts.md). |

## 2bis. Cómo está organizado `dashboard_app/` por dentro

`dashboard_app/dashboard.py` era un único archivo de 2910 líneas con las 74 rutas de la
app (login, dashboard de red, ABM, alertas, informes, etc.). Se reorganizó en routers de
FastAPI por dominio — mismas rutas, mismo comportamiento, solo se movió el código de
lugar. Ver el detalle completo (qué se movió, en qué orden, y cómo se verificó cada paso)
en [08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md).

| Archivo | Rol |
|---|---|
| `dashboard_app/dashboard.py` (195 líneas) | Composición: crea la app, arma middleware (CORS, GZip, cabeceras de seguridad, rate limiting), y hace `include_router(...)` de los 10 módulos de abajo. |
| `dashboard_app/core.py` | Lo mínimo que comparten 2+ routers: `get_db`, `templates`, `limiter`, `generar_password_temporal`, `ROLES_INTERNOS_VALIDOS`. |
| `dashboard_app/routers/auth.py` | Login/logout, `/`, `/monitor`, `/beta`, rate limit de intentos fallidos. |
| `dashboard_app/routers/websocket.py` | `/ws/alertas` y su `ConnectionManager`. |
| `dashboard_app/routers/resumen_red.py` | Resumen por hospital, por provincia/proyecto, datos del mapa. |
| `dashboard_app/routers/hospitales_metadata.py` | ABM de hospitales (alta, edición, toggles, KPIs manuales). |
| `dashboard_app/routers/hospital_detalle.py` | Detalle/histórico/estado de software de un hospital puntual (incluye la función más grande de toda la app, estado de software). |
| `dashboard_app/routers/alertas_config.py` | Alertas activas, configuración global de umbrales, exclusiones. |
| `dashboard_app/routers/informes.py` | Generación de PDFs/gráficos, historial de reportes, RIS Analytics. |
| `dashboard_app/routers/usuarios.py` | Perfil propio, cambio de contraseña, ABM de usuarios internos. |
| `dashboard_app/routers/clientes.py` | ABM del rol Cliente, portal `/cliente`. |
| `dashboard_app/routers/solicitudes_acceso.py` | Los dos flujos de alta (legacy directo a Asana, y el nuevo con cola de aprobación). |

Dos hallazgos de este trabajo, documentados con más detalle en el plan de refactor:
`/api/v1/nodos-hospitalarios` llama a una función que no existe en el repo (bug
preexistente, no introducido acá), y hay ~380 líneas de código muerto (funciones de
generación de gráficos duplicadas contra `generator_report.py`, sin llamador real) que se
movieron tal cual sin corregir, porque este trabajo fue solo reorganización.

## 3. Por qué es un único proceso

`server.py` importa tanto `main.py` (listener) como `dashboard_app/dashboard.py`
(dashboard) y los fusiona en una sola app FastAPI (`master_app`) para que compartan el
mismo loop de eventos, el mismo `ConnectionManager` de WebSockets y el mismo proceso de
background. Es una decisión de diseño consciente (compilable con `compiler.txt`, que
documenta también el modo de correr cada mitad por separado en desarrollo), pero implica
que **un bug o un bloqueo en cualquiera de las dos mitades afecta a la otra** — no hay
aislamiento de fallos entre "recibir datos de agentes" y "servir el dashboard a usuarios".

## 4. Flujo de ingesta de un reporte

1. Un agente hace `POST /v1/hospital-status` con el payload JSON (`main.py:82`).
2. Se detecta la versión de schema (`3.0`/`4.0`.. pasan directo; cualquier otra cosa se
   asume v2 legacy y pasa por `transformer.transformar_v2_a_v3`).
3. Se valida contra `schemas.AgentReportV4` (Pydantic).
4. Se separan y persisten en tablas distintas: `reportes_historicos` (infra),
   `reportes_uso` (KPIs de aplicación), `software_monitoring` (Mirth, Elasticsearch/logs,
   certificados SSL, colas de auto-enrutado DICOM), enriqueciendo eventos de log contra
   `log_dictionary`.
5. Se commitea todo en una sola transacción.

## 5. Flujo de alertas

Cada 60s, `ciclo_vigilancia()` corre tres pasadas sobre todos los hospitales (hardware
offline, KPIs programados, estado de software) dentro de `asyncio.to_thread(...)`, para no
bloquear el loop de eventos mientras dura (las llamadas a Asana son síncronas y pueden
tardar segundos) — ver [04-seguridad.md](04-seguridad.md) y
[07-plan-de-accion.md](07-plan-de-accion.md) ítem 1.2, corregido en la Fase 1. Cuando
detecta una condición que cruza el umbral (y no está cubierta por una regla de
`AlertExclusionModel`), crea o cierra una tarea en Asana vía
`asana_conector.crear_tarea_alerta` y dispara un `POST` local a
`/api/internal/trigger-ws` (la versión protegida por IP de `server.py`, la duplicada sin
protección de `dashboard.py` se eliminó en la misma Fase 1) para que el dashboard se
actualice en vivo por WebSocket.

## 6. Frontend

Server-side rendering con Jinja2 (`dashboard_app/templates/`) + JS vanilla
(`dashboard_app/static/script.js`, ~5100 líneas, compartido por las dos plantillas de abajo)
+ Chart.js para gráficos. Hay una carpeta `static/` con un `manifest.json` y `sw.js`
(service worker), sugiriendo soporte PWA parcial. También conviven varias páginas de
marketing/ventas (`solucion1..4.html`, `demo-pacs.html`, `herramientas.html`).

⚠️ **`index_beta.html` (`/beta`) es la plantilla que efectivamente usa el equipo interno
hoy — no es una "vista beta" secundaria pese al nombre.** `login.html` redirige ahí a todo
usuario interno tras un login exitoso (`window.location.href = '/beta'`, comentado
explícitamente `// internos, como hasta ahora`). `index.html` (`/monitor`) es una versión
anterior que quedó de una etapa previa del proyecto — sigue montada y accesible (hay un
botón "Usar versión clásica" en el login que lleva ahí, y `manifest.json` todavía apunta
`start_url` a `/monitor`), pero **no es la que se mantiene activamente ni la que ve el
equipo por default**. Al tocar el frontend, el trabajo real va en `index_beta.html` +
`script.js` — cualquier cambio espejado en `index.html` es best-effort para no romper esa
ruta vieja, no el objetivo principal. Esto se confundió al menos una vez durante el
desarrollo del mapa de integraciones (ver
[13-contrato-topologia-mirth.md](13-contrato-topologia-mirth.md)); si se vuelve a tocar el
frontend, confirmar primero en `login.html` a qué ruta redirige antes de asumir cuál
plantilla es la vigente.

**Refresco en vivo (`static/refresco-vivo.js`, solo `index_beta.html`):** cada 30s consulta
`/api/hospital/{id}` y compara `db_timestamp`. En el home aplica el refresco solo. En el
detalle de un hospital **no redibuja automáticamente** (hacerlo reconstruía las tarjetas
superiores y colapsaba las plegables que el usuario tenía abiertas): guarda el dato nuevo y
muestra un botón "Datos nuevos" junto a la tarjeta del hospital; recién al click aplica
cabecera + pestaña activa. La primera lectura tras abrir un hospital es la línea base (no
cuenta como dato nuevo). Único caso donde sí aplica directo: al volver a la pestaña del
navegador tras estar oculta o al reconectar el WebSocket, porque ahí la vista puede llevar
minutos congelada.
