# Análisis de performance

**Estado — 2026-09-10**: P1 y P4 están resueltos (Fase 1, verificado en producción). El
resto (P2, P3, P5, P6, P7, P8) sigue pendiente — no formaban parte de la Fase 1 ni del
refactor de organización de `dashboard_app/` (que fue solo mover código, sin tocar lógica
ni queries). Las referencias a `dashboard_app/dashboard.py:línea` de los hallazgos
pendientes son del momento del análisis original; ese código ahora vive repartido en
`dashboard_app/routers/*.py` — ver [08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md)
para el mapeo de qué se movió a dónde.

Igual metodología que en seguridad: lectura completa de los módulos centrales
(`database.py`, `main.py`, `server.py`, `dashboard_app/dashboard.py`,
`dashboard_app/alerts_engine.py`, `dashboard_app/resumen_hospital.py`) más búsqueda
dirigida de patrones (loops con queries adentro, `.all()` sin filtro de fecha/límite,
llamadas de red síncronas). El código ya muestra varias correcciones de performance
previas y documentadas en comentarios (`LIMIT 15000`, downsampling a 600 puntos, caché en
memoria de 30-60s) — quien mantiene esto ya viene atacando estos problemas de a uno; lo que
sigue es lo que queda.

---

## <a name="p1"></a>P1 — HIGH — El ciclo de alertas bloquea el event loop entero cada 60s

**✅ RESUELTO — Fase 1.** Las tres llamadas de `alerts_engine` en `ciclo_vigilancia()`
quedaron envueltas en `asyncio.to_thread(...)` (se extrajo un helper sync,
`_ejecutar_ciclo_alertas()`, para poder envolver las tres juntas en una sola llamada).
Verificado con `TestClient` y en producción.

**Dónde (al momento del hallazgo)**: `server.py:81-95` (`ciclo_vigilancia`) llama de forma **síncrona y sin
`asyncio.to_thread`** a:
```python
alerts_engine.procesar_offline(db)
alerts_engine.verificar_kpis_programados(db)
alerts_engine.verificar_estado_software(db)
```
dentro de una función `async def` que corre en el mismo event loop que atiende todas las
requests HTTP y los WebSockets del proceso.

Estas tres funciones hacen consultas SQL síncronas (`sqlalchemy` con SQLite, rápidas en
general) **y llamadas HTTP síncronas a la API de Asana** vía
`asana_conector.crear_tarea_alerta` (`dashboard_app/alerts_engine.py:548,564,590,600,810`),
usando el SDK sync de `asana` — cada llamada puede tardar cientos de ms a varios segundos
si Asana está lenta o hay retries. Además, varias de estas llamadas están **dentro de loops
por hospital** (`for hosp in hospitales_ris`, `for hosp in hospitales_activos`), por lo que
en un tick con varios hospitales generando alertas simultáneamente, el tiempo bloqueado se
multiplica.

**Impacto**: mientras el tick de alertas corre, **nada más en el proceso avanza**: las
requests del dashboard quedan colgadas, el listener de ingesta (`/v1/hospital-status`) no
procesa reportes entrantes, y los WebSockets no envían ni reciben. Con varios hospitales
con incidentes simultáneos + Asana con latencia, esto puede ser de varios segundos a
decenas de segundos de congelamiento total del servidor, cada 60 segundos.

**Contraste**: las otras dos tareas periódicas del mismo loop (`maintenance.py`,
`limpiar_alertas.py`) sí están correctamente envueltas en `asyncio.to_thread` (`server.py:
101, 112`) — el patrón correcto ya existe en el propio código, solo falta aplicarlo acá.

**Acción recomendada**: envolver las tres llamadas de `alerts_engine` en
`asyncio.to_thread(...)`, igual que ya se hace con mantenimiento y limpieza. Si se quiere
ir más lejos, migrar `asana_conector` a un cliente HTTP async (`httpx.AsyncClient`) para
poder paralelizar las notificaciones de varios hospitales en vez de serializarlas.

---

## <a name="p2"></a>P2 — HIGH — Agregados de red completa recalculan sobre TODO el histórico, sin filtro de fecha

**Dónde**: `dashboard_app/resumen_hospital.py:74-76`, función `calcular_kpis_hospital`,
llamada desde `/api/resumen-hospitales` y `/api/provincias`:
```python
usos = db.query(database.ReporteUso).filter(
    database.ReporteUso.hospital_id == hospital_id
).all()
```
Sin `LIMIT`, sin cota de fecha (`timestamp >=`). Trae **todas** las filas de `reportes_uso`
que existan para ese hospital desde el día que se instaló el agente, y las parsea una por
una en Python (`json.loads` + acumulación) en cada llamada. Se ejecuta una vez por
hospital, para construir el resumen de toda la red.

**Impacto**: el propio docstring del archivo (línea 9-10) ya reconoce el riesgo
("`reportes_historicos` puede tener millones de filas"). El costo de esta función crece
linealmente con la antigüedad de cada hospital — un hospital con 2 años de historia paga
hoy exactamente el mismo costo por refresco de caché que pagará dentro de 3 años, pero
mayor. Mitigado parcialmente por el caché en memoria de 30-60s
(`dashboard_app/routers/resumen_red.py` tras el refactor de organización), pero cada
expiración de caché recalcula todo de nuevo para los ~40-70 hospitales de la red.

**Acción recomendada**: los acumulados (`estudios`, `admitidas`, `asociadas`,
`definitivas`) son sumas — se pueden calcular con `SUM()` en SQL en vez de traer las filas
a Python, o, mejor aún, mantener un contador acumulado incremental en
`hospitales_metadata` que se actualiza en cada ingesta (`main.py`) en vez de recalcularse
desde cero. Si se necesita mantener el cálculo en Python por la lógica de exclusión de
AETs, al menos acotar con un `timestamp <=` razonable o materializar un agregado
diario/mensual.

---

## <a name="p3"></a>P3 — MEDIUM — N+1 queries en la verificación de software por hospital

**Dónde**: `dashboard_app/alerts_engine.py:857-875` (`_verificar_mirth`) y patrones
análogos en `verificar_estado_software` (líneas 820-1141): loops `for hosp in
hospitales_activos` / `for hosp in hospitales_ris` que ejecutan una query SQL
independiente por hospital dentro del loop, en vez de una sola query para todos los
hospitales activos con `GROUP BY`/`WHERE hospital_id IN (...)`.

La query de Mirth en particular usa `LOWER(app_name) LIKE '%mirth%'` — el `LOWER()` y el
wildcard inicial (`%mirth%`) impiden que SQLite use el índice existente sobre `app_name`
(`database.py:103`), forzando un table scan de `software_monitoring` **por cada hospital,
en cada tick de 60s**.

**Acción recomendada**: normalizar `app_name` a minúsculas al escribir (ya se hace en
`main.py:43`: `app_name = payload.app_name.lower()` para el diccionario de logs — aplicar
el mismo criterio acá) para poder usar `app_name = 'mirth'` sin `LOWER()`/`LIKE`, y traer
todos los hospitales en una sola query con `WHERE hospital_id IN (:lista)` en vez de un
loop.

---

## <a name="p4"></a>P4 — MEDIUM — `software_monitoring` sin índice compuesto para el patrón de acceso real

**✅ RESUELTO — Fase 1.** Índice `idx_swmon_hosp_app_ts` agregado al modelo, más el script
de migración `Accesorios/agregar_indice_software_monitoring.py` (necesario porque
`create_all()` no agrega índices a una tabla que ya existe) — corrido en producción.

**Dónde (al momento del hallazgo)**: `database.py:98-109`. La tabla tiene índices individuales en `hospital_id`,
`app_name`, `component_id` y `timestamp`, pero **no** un índice compuesto
`(hospital_id, app_name, timestamp)`, que es exactamente el patrón de `WHERE hospital_id =
:hid AND app_name = ... ORDER BY timestamp` que usan tanto el motor de alertas
(cada 60s) como `/api/hospital/{hospital_id}/software`. SQLite puede usar como mucho uno de
los índices simples por query, típicamente el de `hospital_id`, y filtrar el resto en
memoria.

**Acción recomendada**: agregar `Index('idx_swmon_hosp_app_ts', 'hospital_id', 'app_name',
'timestamp')`, mismo patrón que ya se usó para `reportes_historicos` y `reportes_uso`
(`idx_hospital_timestamp`, `idx_uso_hospital_timestamp`).

---

## <a name="p5"></a>P5 — MEDIUM — `full_json_data` completo cargado y parseado para leer 2-3 campos

**Dónde**: múltiples puntos, ej. `dashboard_app/routers/hospital_detalle.py`
(`/api/hospital/{hospital_id}`, ruta movida ahí en el refactor de organización) y
`resumen_hospital.py:28-58` (`uso_disco_j_appv_tb`,
`ram_pct`): se hace `SELECT *` (o se trae la columna `full_json_data` completa, que
incluye el payload entero del agente — puede ser varios KB por fila) y se decodifica el
JSON en Python solo para extraer 1-2 valores (`ram usage_percent`, un disco puntual).
Ya mitigado en parte en `/api/hospital/{hospital_id}/history` con `LIMIT 15000` +
downsampling, pero no en los endpoints que solo piden "el último reporte".

**Impacto**: costo de I/O y CPU (deserializar JSON grande) desproporcionado al dato que se
necesita, multiplicado por la frecuencia con la que se llaman estos endpoints (dashboard en
vivo, refrescos periódicos del frontend).

**Acción recomendada**: para las columnas que se consultan sueltas con frecuencia (RAM%,
CPU%, estado), ya existen columnas propias en `reportes_historicos`
(`host_cpu_usage`, `host_ram_usage`, `host_status` — ver `database.py:41-44`) — usarlas en
vez de re-parsear `full_json_data` cuando el dato ya está desnormalizado en una columna.
Con SQLite no es viable extraer campos de JSON de forma indexada de manera práctica, así
que la desnormalización que ya empezaron es el camino correcto — falta terminar de
aplicarla donde todavía se vuelve a parsear el JSON completo.

---

## <a name="p6"></a>P6 — MEDIUM — SQLite como base de datos de producción: límite estructural de escalabilidad

**Dónde**: `database.py:12-21`. Un único archivo SQLite, con WAL activado (buena práctica
para SQLite) pero SQLite sigue siendo **de un solo escritor a la vez**. El propio
`timeout=15` (línea 19) es un reconocimiento implícito de que hay contención de escritura
esperable: si el listener de ingesta, el motor de alertas y varias requests del dashboard
que escriben (config, ABM) coinciden, alguna espera hasta 15s por el lock antes de fallar.

**Impacto**: mientras el volumen de hospitales/agentes sea el actual, es manejable — pero
es el techo estructural del sistema. No hay forma de escalar horizontalmente el proceso
(no se puede correr dos instancias de `server.py` contra el mismo archivo SQLite sin
arriesgar corrupción/contención) ni de separar lecturas de escrituras.

**Acción recomendada**: no es urgente si el volumen actual de hospitales/reportes es
estable, pero conviene tenerlo mapeado como el motivo #1 por el que, si la red de
hospitales crece significativamente, el siguiente paso natural es migrar a PostgreSQL
(SQLAlchemy ya abstrae el motor, el cambio sería principalmente de infraestructura + ajustar
el puñado de `text()` con SQL específico de SQLite, ej. `PRAGMA`, `ATTACH DATABASE`).

---

## <a name="p7"></a>P7 — LOW — Scripts de exportación/purga cargan tablas completas en memoria

**Dónde**: `exportar_mes.py`, `borrar_mes.py`, `exportar_equipos_pacs.py`,
`exportar_resumen_hospitales.py` — ver inventario completo en
[06-operaciones-y-scripts.md](06-operaciones-y-scripts.md). Son scripts manuales, no
impactan el servidor en vivo directamente, pero sobre una DB de varios GB pueden tardar
minutos y competir por el lock de escritura de SQLite con el proceso principal si se
corren mientras el servidor sigue recibiendo ingesta.

**Acción recomendada**: documentar (y, si se puede, forzar) que estos scripts se corran
con el servicio detenido o en una ventana de mantenimiento, no en caliente.

---

## <a name="p8"></a>P8 — MEDIUM — No hay purga real de datos: la DB crece sin límite superior

**Dónde**: `maintenance.py:12-13` declara `DIAS_RETENCION_TOTAL = 365`, pero esa constante
**no se usa en ningún lugar del archivo ni del resto del repo** (confirmado por búsqueda en
todo el repositorio). `ejecutar_mantenimiento()` solo **comprime** el detalle de
`reportes_historicos` más viejo que `DIAS_RETENCION_DETALLE=7` días en bloques de 30 min —
nunca borra filas por antigüedad. Las tablas `reportes_uso` y `software_monitoring` no
tienen ningún job de compactación ni purga: `software_monitoring` en particular inserta una
fila **por canal Mirth, por regla de log, por certificado SSL y por cola DICOM, en cada
reporte de cada hospital** (`main.py:203-310`), por lo que crece más rápido que
`reportes_historicos`.

**Impacto**: sin un techo real, el archivo `monitor_hospitales.db` crece indefinidamente.
Esto empeora directamente P6 (SQLite de un solo escritor): un archivo más grande implica
`VACUUM`/backups más lentos, más contención de lock por escritura (más páginas para
sincronizar en WAL), y planes de consulta más costosos sobre las tablas sin purgar. Hoy se
mitiga manualmente y de forma no automatizada con `borrar_mes.py` + `exportar_mes.py` (ver
[06](06-operaciones-y-scripts.md)), que solo cubren `reportes_historicos`/
`software_monitoring` y dependen de que alguien los corra a mano con las fechas correctas.

**Acción recomendada**: decidir una política de retención real (usar `DIAS_RETENCION_TOTAL`
o reemplazarla) e implementar la purga automática de las tres tablas de serie temporal
(`reportes_historicos`, `reportes_uso`, `software_monitoring`) dentro del propio
`ejecutar_mantenimiento()`, en vez de depender de scripts manuales con fechas hardcodeadas.

---

## Resumen priorizado

| # | Hallazgo | Severidad | Estado |
|---|---|---|---|
| P1 | Ciclo de alertas bloquea el event loop (Asana síncrono sin `to_thread`) | HIGH | ✅ Resuelto (Fase 1) |
| P2 | KPIs de red recalculan sobre histórico completo sin cota de fecha | HIGH | Pendiente |
| P3 | N+1 queries por hospital en verificación de software (+ `LIKE`/`LOWER` sin índice) | MEDIUM | Pendiente |
| P4 | Falta índice compuesto en `software_monitoring` | MEDIUM | ✅ Resuelto (Fase 1) |
| P5 | Re-parseo de `full_json_data` completo para leer 1-2 campos ya desnormalizados | MEDIUM | Pendiente |
| P6 | SQLite es el techo de escalabilidad estructural | MEDIUM (a futuro) | Pendiente (no urgente) |
| P8 | Sin purga real de datos — crecimiento indefinido de la DB | MEDIUM | Pendiente |
| P7 | Scripts de mantenimiento compiten por el lock de escritura si corren en caliente | LOW | Pendiente |

Detalle de ejecución de P1/P4 en [07-plan-de-accion.md](07-plan-de-accion.md). P2, P3, P5,
P6, P7, P8 no están en ninguna fase activa del plan todavía — son candidatos a una fase
futura si hace falta.
