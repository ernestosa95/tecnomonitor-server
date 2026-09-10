# Plan de acción priorizado

Combina los hallazgos de [04-seguridad.md](04-seguridad.md), [05-performance.md](05-performance.md)
y [06-operaciones-y-scripts.md](06-operaciones-y-scripts.md) en un orden de ejecución. El
criterio de orden es **impacto / esfuerzo**, con una restricción dura: nada que dependa de
credenciales rotadas se hace antes de rotarlas, y nada que reescriba el historial de git se
hace antes de que ambas partes lo tengan claro (es una operación destructiva sobre el
historial compartido).

Cada ítem indica el archivo de origen (`Sx`/`Px`) para el detalle completo.

---

## Fase 0 — Hoy, fuera de código (minutos, no requiere deploy)

No son cambios de código: son acciones administrativas que cierran la ventana de exposición
ya mismo, independientemente de cuándo se toque el repo.

| # | Acción | Motivo | Quién |
|---|---|---|---|
| 0.1 | Rotar/revocar el token de Asana comprometido (Admin Console → Apps → Personal Access Tokens) y generar uno nuevo para `ASANA_ACCESS_TOKEN` en el `.env` de producción. | [S1](04-seguridad.md#s1) — token real en git desde el primer commit | Quien administre el workspace de Asana |
| 0.2 | Contactar a `gianluca.levrero@tecnoimagen.com.ar` y forzar cambio de contraseña de su cuenta. | [S1b](04-seguridad.md#s1b) — contraseña real en git, con `must_change_password=False` | Admin del sistema |

**Todo lo demás de este plan puede esperar a después de esta fase, pero esta fase no debería esperar a nada.**

---

## Fase 1 — Esta semana (cambios triviales/bajos, alto impacto, sin riesgo de romper nada)

**✅ Completada — 2026-09-10.**

Son ediciones acotadas, de una función o unas pocas líneas, sin cambios de esquema ni de
contrato de API. Se hicieron y quedaron listas para desplegar en un solo batch.

| # | Acción | Archivo(s) | Ref | Estado |
|---|---|---|---|---|
| 1.1 | Borrar `Accesorios/limpiar_alertas.py` (duplicado obsoleto, ya con el token viejo) y `user.py` + `Accesorios/user.py` (scripts de un solo uso, ya ejecutados). | raíz, `Accesorios/` | [S1](04-seguridad.md#s1), [S1b](04-seguridad.md#s1b) | ✅ |
| 1.2 | Envolver las 3 llamadas de `alerts_engine` en `ciclo_vigilancia()` con `asyncio.to_thread(...)`, igual que ya se hace con `maintenance` y `limpiar_alertas`. | `server.py` | [P1](05-performance.md#p1) — HIGH, el fix de mayor impacto de todo el análisis de performance | ✅ |
| 1.3 | Agregar índice compuesto `(hospital_id, app_name, timestamp)` en `software_monitoring` + script de migración para la DB ya existente (`create_all()` no agrega índices a tablas que ya existen). | `database.py`, `Accesorios/agregar_indice_software_monitoring.py` (nuevo) | [P4](05-performance.md#p4) | ✅ código y ✅ script corrido en producción |
| 1.4 | Quitar `"token": token` de la respuesta de `POST /api/login` (ya viaja en la cookie httpOnly). También se sacó `sessionStorage.setItem('tecnomonitor_token', ...)` en `login.html`, que era código muerto (nada en el repo lo leía) y hubiera guardado `"undefined"` tras este cambio. | `dashboard_app/dashboard.py`, `dashboard_app/templates/login.html` | [S6](04-seguridad.md#s6) | ✅ |
| 1.5 | Borrar la definición sin protección de `POST /api/internal/trigger-ws` en `dashboard_app/dashboard.py`, dejando solo la de `server.py` (que sí valida IP). | `dashboard_app/dashboard.py` | [S7](04-seguridad.md#s7) | ✅ |
| 1.6 | Reemplazar `PASSWORD_TEMPORAL` fija por una generada con `secrets.SystemRandom()` por usuario, y forzar `must_change_password=True` en los 3 flujos (alta, reactivación, reset) — antes quedaba en `False` por default del modelo. | `create_user.py` | [S1c](04-seguridad.md#s1c) | ✅ |
| 1.7 | Agregar contador de intentos fallidos por `email`/username además de por IP en el login, con su propia tabla (`login_attempts_email`, se crea sola en el próximo arranque). | `database.py`, `dashboard_app/dashboard.py` | [S4](04-seguridad.md#s4) | ✅ |
| 1.8 | Sanear campos de `/submit-lead` contra CSV/Formula Injection (prefijar `'` si empiezan con `=+-@`/tab/CR) y agregar `@limiter.limit(...)` a `/submit-lead` (10/min) y `/v1/generar-reporte-ris` (5/min). | `dashboard_app/dashboard.py` | [S3](04-seguridad.md#s3) | ✅ |
| 1.9 | Generar `requirements.txt` best-effort a partir de los imports del repo (no hay entorno de producción accesible desde acá para `pip freeze`). | raíz (`requirements.txt`, nuevo) | [S8](04-seguridad.md#s8), operaciones §1 | ⚠️ generado sin versiones fijadas — **falta reemplazarlo con `pip freeze` real de producción** |

**Dependencia**: 1.1 se hizo **después** de confirmar que 0.1 y 0.2 ya estaban resueltas
(rotar antes de borrar, no al revés).

**Deploy confirmado en producción (2026-09-10):** servidor levantado correctamente, script de
migración del índice corrido, y `requirements.txt` reemplazado en producción por la salida
real de `pip freeze`. El commit lo hace el equipo directamente desde el servidor de
producción, no desde este checkout.

---

## Fase 2 — Próximas 2-3 semanas (esfuerzo medio, tocan contrato de API o lógica de negocio)

**⏸️ En pausa (2026-09-10)** — a pedido, se posterga para atender otros temas primero. Antes
de retomarla falta definir el rollout de 2.1 (si el equipo controla el agente desplegado en
cada hospital, y con qué estrategia de transición evitar cortar ingesta de agentes viejos).

Requieren más diseño (definir un formato de API key, decidir un umbral de tamaño de
payload) y coordinación con quien mantiene los agentes desplegados en los hospitales, porque
algunos cambios los afectan directamente.

| # | Acción | Archivo(s) | Ref |
|---|---|---|---|
| 2.1 | **Diseño ya decidido** — ver [11-plan-auth-ingesta-agente.md](11-plan-auth-ingesta-agente.md): token por hospital (SHA-256, columna nueva en `hospitales_metadata`), exigido vía `Authorization: Bearer <token>` solo para `schema_version 4.5` en adelante. Las versiones viejas (`3.0`-`4.3`) no cambian, así los ~80 hospitales migran de a uno según se les actualiza el agente, sin fecha de corte global. Rate limiting del endpoint sigue como ítem separado (no bloquea este). | `main.py`, `database.py`, `dashboard_app/core.py`, `routers/hospitales_metadata.py` | [S2](04-seguridad.md#s2) — HIGH, el hallazgo de seguridad más importante después de las credenciales filtradas |
| 2.2 | Agregar límite de tamaño de body (1-2 MB) a nivel de Nginx/Starlette para las rutas de ingesta. | Nginx config / `main.py` | [S5](04-seguridad.md#s5) |
| 2.3 | Reescribir `calcular_kpis_hospital` para agregar con `SUM()` en SQL en vez de traer todas las filas de `reportes_uso` a Python, o mantener un contador incremental en `hospitales_metadata`. | `dashboard_app/resumen_hospital.py:74-76` | [P2](05-performance.md#p2) — HIGH |
| 2.4 | Normalizar `app_name` a minúsculas al escribir en `software_monitoring` (ya se hace para el diccionario de logs) y reemplazar los loops por-hospital en `verificar_estado_software`/`verificar_mirth` por una sola query con `WHERE hospital_id IN (...)`. | `dashboard_app/alerts_engine/orquestador.py`, `software/mirth.py` (reorganizado en paquete, ver [09](09-plan-refactor-alertas.md)) | [P3](05-performance.md#p3) |
| 2.5 | Reemplazar el re-parseo de `full_json_data` por las columnas ya desnormalizadas (`host_cpu_usage`, `host_ram_usage`, `host_status`) en los endpoints que piden "el último reporte". | `dashboard_app/routers/hospital_detalle.py` (ruta movida ahí en el refactor de organización), `resumen_hospital.py:28-58` | [P5](05-performance.md#p5) |
| 2.6 | Agregar `.env.example` documentando todas las variables de entorno usadas (sin valores reales). | raíz | operaciones §2 |

---

## Fase 3 — Backlog / mediano plazo (esfuerzo medio-alto, o depende de decisiones de producto)

No son urgentes hoy, pero conviene tenerlos agendados antes de que el síntoma aparezca en
producción (DB lenta, alertas duplicadas, etc.) en vez de reaccionar en caliente.

| # | Acción | Ref |
|---|---|---|
| 3.1 | Implementar purga automática real de `reportes_historicos`, `reportes_uso` y `software_monitoring` dentro de `ejecutar_mantenimiento()`, usando (o reemplazando) `DIAS_RETENCION_TOTAL`. | [P8](05-performance.md#p8) |
| 3.2 | Sacar las fechas hardcodeadas de `borrar_mes.py`/`exportar_mes.py`, pasarlas por argumento de línea de comandos, y agregar modo `--dry-run`. | operaciones §6.3 |
| 3.3 | Consolidar `Accesorios/` con la raíz (decidir una única fuente de verdad para scripts operativos, eliminar duplicados restantes como `migrar_datos_manuales.py`). | operaciones §5 |
| 3.4 | Confirmar si `provincias_endpoint.py` y `Accesorios/modelsOUT.py` siguen en uso; si no, borrarlos. | operaciones §4, §5 |
| 3.5 | Migrar `asana_conector` a un cliente HTTP async (`httpx.AsyncClient`) para paralelizar notificaciones en vez de serializarlas — mejora incremental sobre 1.2. | [P1](05-performance.md#p1) |
| 3.6 | Reescribir el historial de git (`git filter-repo`/BFG) para eliminar el token de Asana y la contraseña filtrados, **solo si el repo se va a compartir o hacer público**. Operación destructiva sobre el historial: coordinar con todo el equipo (todos deben re-clonar), hacerlo en una ventana acordada, y solo después de que 0.1/0.2 ya estén resueltos (si no, reescribir el historial sin haber rotado las credenciales no sirve de nada). | [S1](04-seguridad.md#s1), [S1b](04-seguridad.md#s1b) |

---

## Fase 4 — Solo si la red de hospitales crece significativamente (no accionable hoy)

| # | Acción | Ref |
|---|---|---|
| 4.1 | Evaluar migración de SQLite a PostgreSQL — SQLAlchemy ya abstrae el motor; el trabajo real es de infraestructura + ajustar el puñado de `text()` con SQL específico de SQLite (`PRAGMA`, `ATTACH DATABASE`). Es el techo estructural de escalabilidad del sistema, no algo a resolver ahora si el volumen actual es estable. | [P6](05-performance.md#p6) |

---

## Fuera de este plan, pero completado — reorganización de `dashboard_app/`

**✅ Hecho — 2026-09-10.** No surgió de un hallazgo de seguridad/performance sino de un
pedido aparte (archivos monolíticos difíciles de mantener): `dashboard_app/dashboard.py`
pasó de 2910 a 195 líneas, repartido en `core.py` + 10 routers por dominio bajo
`dashboard_app/routers/`. Mismo comportamiento, mismas 74 rutas, verificado en cada uno de
los 10 pasos (local con `TestClient` y contra producción real). Detalle completo en
[08-plan-refactor-dashboard.md](08-plan-refactor-dashboard.md).

Dos hallazgos salieron de ese trabajo, sin arreglar a propósito (el refactor fue solo
reorganización): `/api/v1/nodos-hospitalarios` está roto (llama a una función que no
existe) y hay ~380 líneas de código muerto duplicadas contra `generator_report.py`. Quedan
como candidatos a una tarea de limpieza aparte, no forman parte de ninguna fase de este
plan todavía.

---

## Resumen visual

```
Fase 0 (hoy)         →  ✅ rotar token Asana + cambiar contraseña filtrada
Fase 1 (esta semana) →  ✅ borrar archivos con secretos, to_thread en alertas,
                         índice software_monitoring, limpiezas de auth/API menores
Fase 2 (2-3 semanas) →  ⏸️ pausada — auth en ingesta, límite de payload, agregados
                         en SQL, N+1 queries, desnormalización
Fase 3 (backlog)     →  pendiente — purga automática, scripts con --dry-run,
                         consolidar Accesorios/, reescribir historial de git (si aplica)
Fase 4 (si crece)    →  pendiente — evaluar Postgres

Aparte del plan       →  ✅ reorganización de dashboard_app/ (2910 → 195 líneas,
(no numerado)            ver 08-plan-refactor-dashboard.md)
```

La única dependencia dura del plan es **0 antes que 1.1** y **0/1.1 antes que 3.6**. El
resto de los ítems dentro de cada fase son independientes entre sí y se pueden paralelizar
o reordenar según disponibilidad del equipo.
