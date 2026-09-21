# Referencia de API

Inventario de endpoints HTTP/WebSocket expuestos por `master_app` (fusión de `main.py` +
`dashboard_app/` + router opcional de `informes_ia`). El código de `dashboard_app/` está
repartido en `dashboard_app/dashboard.py` (composición) y `dashboard_app/routers/*.py` (un
módulo por dominio) desde el refactor de organización — ver
[01-arquitectura.md §2bis](01-arquitectura.md#2bis-cómo-está-organizado-dashboard_app-por-dentro)
y [08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md) para el mapeo completo de
qué ruta vive en qué archivo; esta tabla no repite esa ubicación por fila. Roles: `Admin`,
`Ingenieria`, `Comercial`, `Visor`, `Cliente` (ver [04-seguridad.md](04-seguridad.md) para
el detalle del RBAC).

## Ingesta (`main.py`) — sin prefijo, montado directo en `master_app`

| Método | Ruta | Descripción | Auth | Notas |
|---|---|---|---|---|
| POST | `/v1/hospital-status` | Recibe el reporte periódico de un agente (v2/v3/v4), normaliza y persiste en 3 tablas. | **Ninguna** | Sin API key de agente, sin rate limit. Ver [04-seguridad.md#s2](04-seguridad.md#s2). |
| POST | `/api/admin/diccionario-logs` | Alta/actualización masiva del diccionario de eventos de log. | `Admin` | |

## Autenticación / sesión

| Método | Ruta | Descripción | Auth | Notas |
|---|---|---|---|---|
| GET | `/` | Página de login. | pública | |
| POST | `/api/login` | Login por email o username + password. Setea cookie `tecnomonitor_token` (JWT, httpOnly, secure, SameSite=Lax, 8h). | pública | Bloqueo tras `MAX_INTENTOS_LOGIN` fallidos, por IP (`login_attempts`) **y por cuenta** (`login_attempts_email`, agregado en Fase 1). El JWT ya no viaja en el body de la respuesta, solo en la cookie (fix Fase 1). |
| POST | `/api/logout` | Borra la cookie de sesión. | cualquiera | |
| GET | `/api/me/permissions` | Devuelve vistas/scope/solo-lectura del usuario logueado (fuente: `permissions.py`). | logueado | |
| GET / PUT | `/api/usuario/perfil` | Ver/editar el perfil propio. | logueado | |
| POST | `/api/user/change-password` | Cambio de contraseña propia. | logueado | Valida longitud/mayúscula/número/símbolo (`validar_password`). |
| POST | `/api/user/request-access` | Solicitud de alta (autoservicio). | pública | |

## Dashboard / datos de red completa

| Método | Ruta | Descripción | Auth |
|---|---|---|---|
| GET | `/monitor` | Vista principal (tabla de toda la red). | HTML, chequeo real vía API |
| GET | `/api/resumen-hospitales` | Resumen agregado por hospital (cacheado 30s en memoria). | `bloquear_cliente()` (cualquier rol interno) |
| GET | `/api/provincias` | Resumen agregado por provincia (cacheado 60s en memoria). | `bloquear_cliente()` |
| GET | `/api/mapa-data` | Datos para el mapa nacional. | `bloquear_cliente()` |
| GET | `/api/alertas` | Alertas activas/históricas. | `Admin`, `Ingenieria` |
| GET / POST | `/api/config` | Configuración global de umbrales. | `Admin`, `Ingenieria` |
| GET | `/api/v1/nodos-hospitalarios` | ⚠️ **Rota** — llama a `obtener_nodos_desde_db()`, función inexistente en el repo, devuelve 500 siempre (bug preexistente, ver [08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md)). | logueado |
| GET | `/api/users/responsables` | Usuarios que pueden ser responsables de tareas Asana. | `Admin`, `Ingenieria` |

## Detalle por hospital (scoping con `require_hospital_access`)

| Método | Ruta | Descripción | Auth |
|---|---|---|---|
| GET | `/api/hospital/{hospital_id}` | Último reporte completo de infraestructura. | pestaña `infra` |
| GET | `/api/hospital/{hospital_id}/history` | Serie histórica (CPU, temperaturas, red, VMs). `LIMIT 15000` en SQL + downsampling a ~600 puntos antes de responder. | pestaña `infra` |
| GET | `/api/hospital/{hospital_id}/kpi-history` | Histórico de KPIs de uso. | pestaña `kpis` |
| GET | `/api/hospital/{hospital_id}/software` | Estado de Mirth / logs / SSL / colas DICOM. Cada canal de Mirth trae `stale` y `sin_datos_min`: `stale` es true si su última lectura quedó más de `mirth_stale_minutes` (15 por defecto) detrás de la más reciente de Mirth del hospital, igual que el mapa de integraciones; `sin_datos_min` es ese atraso en minutos. | pestaña `software` |
| GET / POST | `/api/hospital/{hospital_id}/kpi-settings` | Config de KPIs granulares (activa/desactiva alertas de inactividad RAD/MAMO, etc.). | GET: `Admin/Ingenieria/Comercial`; POST: `Admin/Ingenieria` |
| GET | `/api/logs-dictionary/{event_id}` | Detalle de un evento del diccionario de logs. | logueado |
| GET | `/api/cliente/casos/{hospital_id}` | Casos/incidentes vistos por un Cliente. | logueado + chequeo manual de `hospitales_de_cliente()` inline (equivalente a `require_hospital_access`, pero duplicando la lógica en vez de reusar el helper) |

## ABM — hospitales, usuarios, clientes, exclusiones (todo bajo `Admin`/`Ingenieria`, algunas solo `Admin`)

| Método | Ruta | Auth |
|---|---|---|
| GET/POST/PUT/DELETE `/api/hospitales-metadata[/…]` (+ 4 rutas `toggle*`) | `Admin`+`Ingenieria` (DELETE: solo `Admin`) |
| GET/PUT `/api/hospitales-metadata/{hid}/manual-kpi` | `Admin`, `Ingenieria` |
| GET/POST `/api/admin/usuarios`, PUT/PATCH/POST `.../{user_id}[/toggle-active\|reset-password]` | `Admin` |
| GET/POST `/api/admin/clientes`, y accesos/activación/reset por cliente | `Admin` |
| GET `/api/admin/access-requests`, POST `.../aprobar-interno`, `.../aprobar-cliente`, `.../rechazar` | `Admin` |
| POST `/api/access-requests` | pública (autoservicio, ver seguridad) |
| GET `/api/hospitales-publico` | pública |
| GET/POST/PUT/DELETE `/api/exclusiones[/preview][/{excl_id}]` | `Admin`, `Ingenieria` |

### Mapa de integraciones Mirth (`routers/mirth_topologia.py`, ver docs/13-contrato-topologia-mirth.md)

| Método | Ruta | Descripción | Auth |
|---|---|---|---|
| GET/POST/PUT `/api/hospital/{hid}/mirth/nodos[/{nodo_id}]` | ABM de nodos curados (sistemas origen/destino del mapa). | `Admin`, `Ingenieria` |
| PATCH `/api/hospital/{hid}/mirth/nodos/{nodo_id}/toggle` | Activa/desactiva un nodo. | `Admin`, `Ingenieria` |
| DELETE `/api/hospital/{hid}/mirth/nodos/{nodo_id}` | Borra un nodo; nulea `nodo_origen_id`/`nodo_destino_id` en `mirth_canales_meta` antes de borrar (SQLite no enforcea FKs). | `Admin` |
| POST `/api/hospital/{hid}/mirth/nodos/adoptar` | Crea un nodo a partir de una `sugerencia` (endpoint técnico auto-detectado) y lo asigna a los `channel_ids` indicados. | `Admin`, `Ingenieria` |
| GET `/api/hospital/{hid}/mirth/canales` | Inventario unificado de canales: topología técnica + curación + último estado, por hospital. | `Admin`, `Ingenieria` |
| GET `/api/hospital/{hid}/mirth/sin-clasificar` | Subconjunto de `/canales` sin fila en `mirth_canales_meta`. | `Admin`, `Ingenieria` |
| PUT `/api/hospital/{hid}/mirth/canales/{channel_id}` | Upsert de la curación de un canal (criticidad, nombre humano, nodos asignados, oculto). | `Admin`, `Ingenieria` |
| DELETE `/api/hospital/{hid}/mirth/canales/{channel_id}` | Borra solo la curación (no la topología técnica reportada). | `Admin` |
| GET `/api/mirth/pendientes` | Conteo de canales sin clasificar por hospital (badge global). | `Admin`, `Ingenieria` |
| GET `/api/hospital/{hid}/mirth/mapa` (`routers/mirth_mapa.py`) | Arma `origenes`/`destinos`/`canales`/`tl` (línea de tiempo bucketizada) para el mapa de integraciones — junta curación + topología técnica + histórico de `software_monitoring`. Query params: `minutos` (default 180), `paso` en minutos (default 5), `incluir_auto` (default 1, agrega nodos sintéticos por endpoint técnico cuando no hay curación). | pestaña `software` |

## Reportes / informes

| Método | Ruta | Descripción | Auth |
|---|---|---|---|
| POST | `/api/informes/pdf` | Genera PDF de infraestructura o KPIs (reportlab + matplotlib). | `Admin/Ingenieria/Comercial`, **rate-limited 5/min** (`@limiter.limit`) |
| GET | `/api/informes/historial` | Historial de reportes generados. | `Admin/Ingenieria/Comercial` |
| POST | `/v1/generar-reporte-ris` | Genera PDF "RIS Analytics" desde la calculadora pública (`solucion2.html`). | pública — el `Depends(auth.get_current_user)` está comentado explícitamente en el código. **Rate-limited 5/min** desde la Fase 1. |
| `/api/informes-ia/*` (router externo, si `informes_ia` cargó) | Solicitar/consultar/editar/aprobar/descargar informes con IA. | `Admin/Ingenieria/Comercial` (aprobar: solo `Admin`) |

## Marketing / páginas públicas y herramientas internas

| Método | Ruta | Descripción | Auth |
|---|---|---|---|
| GET | `/herramientas`, `/ris-analytics`, `/prov-analytics`, `/hl7-analytics`, `/pacs-capacity`, `/salta-project`, `/renovacion`, `/demo-pacs`, `/tecno-solution` | Páginas HTML de ventas/herramientas internas. | mayormente públicas |
| POST | `/submit-lead` | Formulario de contacto de un evento → append a CSV en disco. | pública. **Rate-limited 10/min y saneado contra CSV/Formula Injection desde la Fase 1.** |
| POST | `/submit-lead-demo-pacs` | Formulario de la landing `/demo-pacs` (nombre, institución, cargo, volumen, email, teléfono, plan, origen) → una fila por envío en `leads_demo_pacs.csv` (relativo al cwd del server; UTF-8 con BOM, fecha en hora de Argentina). | pública. **Rate-limited 10/min**, validación de email/teléfono y largo máximo por campo, saneado contra CSV/Formula Injection. |
| GET | `/beta`, `/beta/simulador` | Vista beta / simulador embebido. | `/beta` pública; `/beta/simulador` requiere login (sin chequeo de rol) |
| GET | `/cliente` | Portal del rol Cliente. | login validado manualmente en el handler |

## WebSocket

| Ruta | Descripción | Auth |
|---|---|---|
| `WS /ws/alertas` | Canal de push para refrescar el dashboard cuando cambian las alertas. | **ninguna** (cualquiera que abra el socket recibe los broadcasts; no filtra por hospital/rol) |
| `POST /api/internal/trigger-ws` | Dispara el broadcast a todos los sockets conectados. | Vive solo en `server.py` (chequea `request.client.host == "127.0.0.1"`). Hasta la Fase 1 existía una segunda copia sin protección en `dashboard_app/dashboard.py`, que ganaba solo por orden de montaje; se eliminó — ver [04-seguridad.md#s7](04-seguridad.md#s7). |

---

*Nota: esta tabla no incluye cada ruta trivial de renderizado de template sin lógica (hay
varias que solo hacen `return templates.TemplateResponse(...)`). El total relevado es de
74 rutas en `dashboard_app/` (repartidas entre `dashboard.py` y `routers/*.py` desde el
refactor de organización) + 2 en `main.py`. El total de rutas no cambió por el refactor —
verificado en cada paso, ver [08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md).*
