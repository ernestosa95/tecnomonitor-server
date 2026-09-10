# Modelo de datos

Motor: **SQLite** (`database.py`), un único archivo `monitor_hospitales.db` en modo
`WAL` con `synchronous=NORMAL` y `timeout=15s`. No hay motor gestionado (Postgres/MySQL) ni
pool de conexiones más allá de lo que da `sqlite3`. Ver implicancias de escalabilidad en
[05-performance.md](05-performance.md#p6).

## Tablas (`database.py`)

| Tabla | Propósito | Notas |
|---|---|---|
| `reportes_historicos` | Serie temporal de estado de infraestructura por hospital (CPU, RAM, power, `full_json_data` con el payload completo). | Índice compuesto `(hospital_id, timestamp)`. `full_json_data` es una columna `JSON` que guarda el reporte entero — crece sin límite de tamaño por fila. |
| `reportes_uso` | Serie temporal de KPIs de aplicación (`application_metrics`), guardados como texto JSON crudo (`kpi_json_data`). | Índice compuesto `(hospital_id, timestamp)`. |
| `software_monitoring` | Estado de componentes de software: canales Mirth, eventos de log (Elasticsearch/Suitestensa), certificados SSL, colas de auto-enrutado DICOM. Todo mezclado en una tabla genérica (`app_name` + `component_id` + `extra_data` JSON). | Índice compuesto `(hospital_id, app_name, timestamp)` agregado en la Fase 1 (`idx_swmon_hosp_app_ts`) — ver [05-performance.md#p4](05-performance.md#p4). |
| `log_dictionary` | Diccionario de eventos de log conocidos (título, descripción, acción recomendada, severidad por defecto), cargado vía `/api/admin/diccionario-logs`. | Enriquece los eventos crudos de `software_monitoring`. |
| `alertas` | Alertas activas/históricas, con vínculo al ticket de Asana (`asana_task_gid`). | |
| `alert_exclusions` | Reglas de supresión de alertas (por hospital, patrón, modo de match, nivel máximo, ventana de mantenimiento). Incluye telemetría propia (`hits`, `last_hit`). | Pensada para evitar ruido de falsos positivos conocidos (discos que viven al 92% por diseño, etc.). |
| `configuracion` | Config global clave/valor (umbrales, flags). | |
| `hospitales_metadata` | ABM de hospitales: nombre, provincia, coordenadas, proyecto de Asana, visibilidad, `kpi_settings` (JSON por hospital), flag `datos_manuales`. | |
| `hospital_manual_kpi` | Snapshot único (no serie temporal) de KPIs para hospitales sin agente instalado, cargado a mano. | 1 fila por hospital (PK = `hospital_id`). |
| `users` | Usuarios internos y clientes. Password con bcrypt, rol, flag `must_change_password`, `asana_id` opcional. | |
| `cliente_hospital_access` | Relación N:M usuario-Cliente ↔ hospital, con flags por pestaña (`ver_infra`, `ver_software`, `ver_kpis`). | `UniqueConstraint(user_id, hospital_id)`, `ON DELETE CASCADE`. Es la base del scoping de datos para el rol `Cliente`. |
| `login_attempts` | Contador de intentos fallidos de login y bloqueo temporal, **por IP** (PK = `ip`). | |
| `login_attempts_email` | Igual que `login_attempts` pero **por cuenta** (PK = `email`). Agregada en la Fase 1 para cerrar el escenario de fuerza bruta distribuida — ver [04-seguridad.md#s4](04-seguridad.md#s4). | |
| `access_requests` | Solicitudes de alta (interno o cliente) pendientes de aprobación por un Admin. | |
| `historial_reportes` | Bitácora de reportes PDF generados (tipo, rango de fechas, estado, link de Asana). | |

## Formato de payload de ingesta (`schemas.py`)

- **V3 (`AgentReportV3`)**: modelo Pydantic fuertemente tipado — `envelope`,
  `physical_layer` (host, telemetría, sensores de temperatura/fans/power, salud de red),
  `virtual_layer` (lista de VMs con su propia telemetría y storage). Permite campos extra
  (`Config.extra = "allow"`) para no romper con agentes desactualizados.
- **V4 (`AgentReportV4`)**: modelo "maestro" actual, pero `envelope`, `physical_layer` y
  `virtual_layer` están tipados como `Dict[str, Any]` / `List[Dict[str, Any]]` — es decir,
  **sin validación estructural real**, a diferencia de V3. Solo `application_metrics`
  mantiene tipado estricto (`RISMetric`, `PACSMetric`, `UserMetric`). Ver el impacto de
  esto en seguridad/performance (payloads sin cota de tamaño ni forma).
- **Compatibilidad V2**: `transformer.transformar_v2_a_v3` traduce el formato viejo
  (`header`, `physical_host`, `environment.thermal/power`) al envelope V3 actual,
  "blindado" contra `None`s con `or {}` en cada nivel.

## Relación con el módulo externo `informes_ia`

El router `dashboard_app/informes_ia_router.py` delega en un paquete que **no vive en este
repo** (`informes_ia`, cargado desde `/home/tecnoxaas/Documents/informes_ia_proyectos/` vía
`.env` propio). Usa su propio almacén SQLite (`informes_ia_historial.sqlite`), separado de
`monitor_hospitales.db`. Esta documentación no cubre su modelo de datos interno porque está
fuera del alcance del repositorio.
