# Operaciones y scripts

## 1. Despliegue

Proceso único, `uvicorn server:master_app --host 0.0.0.0 --port 8001`, detrás de un Nginx
local que hace de reverse proxy (`proxy_headers=True, forwarded_allow_ips="127.0.0.1"` en
`server.py`). No hay `Dockerfile`, `docker-compose.yml`, archivo `.service` de systemd, ni
script de despliegue en el repo — el proceso de arranque en el servidor real no está
documentado en el código.

**✅ Resuelto (Fase 1)**: existe `requirements.txt` en la raíz, generado con `pip freeze`
real desde el entorno de producción (versiones fijadas). Antes de esto no existía ningún
manifiesto de dependencias — ver [04-seguridad.md#s8](04-seguridad.md#s8).

## 2. Variables de entorno esperadas (`.env`, no versionado)

Inferidas de `os.getenv`/`os.environ.get` en el código:

| Variable | Usada en | Obligatoria |
|---|---|---|
| `JWT_SECRET` | `dashboard_app/auth.py` | Sí — el proceso no arranca sin ella |
| `ASANA_ACCESS_TOKEN` | `dashboard_app/asana_conector.py` | No — circuit breaker deshabilita Asana si falta |
| `WORKSPACE_GID`, `MAIN_PROJECT_GID`, `RESPONSABLE_GID`, `FOLLOWERS_GIDS` | `dashboard_app/asana_conector.py` | Relacionadas a Asana |
| (variables propias de `informes_ia`, ej. `GEMINI_API_KEY`) | paquete externo, `.env` separado en `/home/tecnoxaas/Documents/informes_ia_proyectos/.env` | Módulo opcional |

No hay un `.env.example` en el repo documentando esta lista — vale la pena agregarlo para
que un despliegue nuevo no tenga que reconstruirla leyendo el código.

## 3. Background jobs (dentro del proceso, `server.py`)

| Job | Frecuencia | Qué hace |
|---|---|---|
| `alerts_engine.procesar_offline` | cada 60s | Detecta hospitales sin reporte reciente (offline). |
| `alerts_engine.verificar_kpis_programados` | cada 60s | Evalúa KPIs de negocio contra umbrales configurados (ej. inactividad RAD/MAMO). |
| `alerts_engine.verificar_estado_software` | cada 60s | Evalúa Mirth, logs (Elasticsearch/Suitestensa), certificados SSL, colas DICOM. |
| `maintenance.ejecutar_mantenimiento` | cada 24h (1440 ticks) | Mantenimiento/purga programada de la DB. |
| `limpiar_alertas.limpiar_alertas_huerfanas` | cada 12h (720 ticks) | Cierra alertas activas de hospitales que ya no reportan esa condición. |

**✅ Resuelto (Fase 1)** — las tres tareas de `alerts_engine` ahora corren envueltas en
`asyncio.to_thread(...)`, igual que mantenimiento y limpieza. Ver
[05-performance.md#p1](05-performance.md#p1).

## 4. Scripts manuales — raíz del repo

| Script | Propósito | ¿Cuándo se corre? | Riesgo si se corre mal |
|---|---|---|---|
| `create_user.py` | ABM de usuarios por CLI (alta, listado, edición). | Manual, por un Admin | Bajo — pide confirmación interactiva. Genera contraseña temporal aleatoria por usuario desde la Fase 1 ([04-seguridad.md#s1c](04-seguridad.md#s1c), resuelto). |
| `borrar_mes.py` | `DELETE` de `reportes_historicos`/`software_monitoring` para un rango de fechas **hardcodeado en el archivo**. | Manual, mensual (según el nombre) | **Alto** — sin dry-run, sin confirmación, sin backup previo automático. Si alguien lo corre sin actualizar `FECHA_INICIO`/`FECHA_FIN` (quedaron en "2026-03-01" a "2026-04-01" al momento de esta revisión), borra el período equivocado. |
| `exportar_mes.py` | Copia (`ATTACH DATABASE`) un rango de fechas a un archivo `.db` "gemelo" de archivo histórico, antes de purgar. | Manual, antes de `borrar_mes.py` | Medio — mismo problema de fechas hardcodeadas; si no se corre *antes* de `borrar_mes.py`, se pierden datos sin archivar. |
| `limpiar_db.py` | `VACUUM` + `wal_checkpoint(TRUNCATE)` para compactar el archivo `.db` tras una purga. | Manual, después de borrar | Bajo, pero bloquea la DB mientras corre (ver [05-performance.md#p7](05-performance.md#p7)) |
| `exportar_equipos_pacs.py`, `exportar_resumen_hospitales.py`, `exportar_mes.py` | Exportaciones puntuales a CSV/Excel para análisis/reporting ad-hoc. | Manual, on-demand | Bajo (solo lectura) |
| `provincias_endpoint.py` | Parece un módulo experimental/standalone relacionado a la agregación por provincia — **no se ve importado desde `server.py`/`dashboard.py`**; podría ser código muerto o un prototipo previo a `resumen_hospital.py`/`/api/provincias`. Confirmar si sigue en uso. | — | — |
| `limpiar_alertas.py`, `maintenance.py`, `migrar_datos_manuales.py`, `migrar_username.py` | Ver secciones de arriba / duplicados abajo. | — | — |

**`user.py` — ✅ resuelto (Fase 1)**: era un script de un solo uso con un email y una
contraseña real hardcodeados ([04-seguridad.md#s1b](04-seguridad.md#s1b)). La contraseña
se cambió y el archivo (y su duplicado en `Accesorios/`) se borraron — ya no existen en el
repo, solo en el historial de git.

## 5. Scripts manuales — `Accesorios/`

Carpeta de scripts sueltos, mayormente migraciones puntuales de esquema (`ALTER TABLE ...
ADD COLUMN`) ya aplicadas en su momento, sin un framework de migraciones (no hay Alembic ni
similar) que lleve registro de qué se aplicó y cuándo:

| Script | Propósito |
|---|---|
| `actualizar_db.py` | Agrega columna `alerts_enabled` a `hospitales_metadata`. |
| `migrar_asana_id.py` | Agrega columna `asana_id` a `users`. |
| `migrar_ris.py` | Agrega columna `has_ris` a `hospitales_metadata`. |
| `migrar_kpi_settings.py` | Agrega columna `kpi_settings` a `hospitales_metadata`. |
| `migratev2v3.py` | Migración de datos v2→v3 sobre la DB completa, **con backup previo automático** (`backup_database()`) — buena práctica, a diferencia de `borrar_mes.py`. |
| `importar_diccionario.py` | Carga el diccionario de logs (`diccionario_error.json`, en la raíz) a la tabla `log_dictionary` vía upsert. Ahora hay también un endpoint equivalente (`POST /api/admin/diccionario-logs`), por lo que este script podría ser el mecanismo original, previo al endpoint. |
| `modelsOUT.py` | Copia de modelos SQLAlchemy — no se ve usada; revisar si es código muerto. |
| `agregar_indice_software_monitoring.py` | **Nuevo (Fase 1).** `CREATE INDEX IF NOT EXISTS` para el índice compuesto de `software_monitoring` ([05-performance.md#p4](05-performance.md#p4), resuelto) — hace falta porque `create_all()` no agrega índices a una tabla que ya existe. Ya corrido en producción, idempotente si se vuelve a correr. |

### Duplicados exactos o casi exactos entre raíz y `Accesorios/`

| Archivo | Estado |
|---|---|
| `migrar_datos_manuales.py` | Idéntico byte a byte en ambos lugares — sigue así, no se tocó. |
| `user.py` | **✅ Resuelto** — ambas copias tenían credenciales reales hardcodeadas ([S1b](04-seguridad.md#s1b)); las dos se borraron en la Fase 1. |
| `limpiar_alertas.py` | **✅ Resuelto** — la copia de `Accesorios/` tenía el token de Asana hardcodeado ([S1](04-seguridad.md#s1)) y se borró en la Fase 1. Queda solo la de la raíz, que lee el token desde env var. |

No hay una única fuente de verdad para el resto de estos scripts (`migrar_datos_manuales.py`
sigue duplicado) — `Accesorios/` funciona como una carpeta "por las dudas" que fue
acumulando copias. Sigue pendiente decidir explícitamente qué vive en cada lado (o eliminar
`Accesorios/` y quedarse con un único lugar para scripts operativos).

## 6. Recomendaciones generales de operaciones

1. ~~Generar `requirements.txt` (`pip freeze` desde producción)~~ — ✅ hecho, Fase 1.
2. Agregar un `.env.example` con las variables de §2 documentadas (sin valores reales). Pendiente.
3. Sacar las fechas hardcodeadas de `borrar_mes.py`/`exportar_mes.py` y pasarlas como
   argumento de línea de comandos, con un modo `--dry-run` que solo cuente filas afectadas
   sin borrar/copiar nada. Pendiente.
4. Consolidar `Accesorios/` con la raíz, eliminando duplicados (`migrar_datos_manuales.py`
   es el que queda). Pendiente.
5. Confirmar si `provincias_endpoint.py` y `Accesorios/modelsOUT.py` siguen en uso; si no,
   borrarlos en vez de dejarlos como código muerto que confunde a quien lea el repo después.
   Pendiente.

## 7. Reorganización de `dashboard_app/` (código, no scripts)

Por separado de los scripts de este documento: `dashboard_app/dashboard.py` (era un único
archivo de 2910 líneas) se reorganizó en `dashboard_app/core.py` + 10 módulos bajo
`dashboard_app/routers/`, sin cambiar comportamiento — ver
[08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md) para el detalle completo y
[01-arquitectura.md §2bis](01-arquitectura.md#2bis-cómo-está-organizado-dashboard_app-por-dentro)
para el resumen. Al desplegar cambios sobre el dashboard web, tené en cuenta que el archivo
relevante para una ruta puntual probablemente ya no sea `dashboard.py`.

Mismo criterio aplicado al motor de alertas: `dashboard_app/alerts_engine.py` (era un
único archivo de 1226 líneas) ahora es el paquete `dashboard_app/alerts_engine/`
(`config.py`, `estado.py`, `exclusiones.py`, `infra.py`, `kpis_negocio/`, `software/`,
`orquestador.py`). El archivo viejo ya no existe -- si el despliegue es por copia de
archivos (no git), hay que borrarlo del servidor explícitamente, no alcanza con subir la
carpeta nueva al lado. Ver [09-plan-refactor-alertas.md](09-plan-refactor-alertas.md).
