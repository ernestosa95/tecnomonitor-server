# Documentación — TecnoMonitor Server

Documentación técnica del backend de TecnoMonitor: plataforma de monitoreo remoto de
infraestructura IT y software (PACS/RIS, Mirth Connect, certificados SSL, colas de
auto-enrutado DICOM, KPIs de uso) para la red de hospitales/clientes de Tecnoimagen.

Generada a partir de una revisión completa del código fuente el `2026-09-10`, y actualizada
el mismo día a medida que se fue ejecutando el plan de remediación. No sustituye al código
como fuente de verdad — cuando algo cambie en el repo, esta documentación puede quedar
desactualizada y hay que revisarla.

## Índice

1. [Arquitectura](01-arquitectura.md) — cómo está armado el sistema, procesos, componentes.
2. [Modelo de datos](02-modelo-de-datos.md) — tablas, relaciones, formatos de payload.
3. [Referencia de API](03-api-referencia.md) — inventario de endpoints HTTP/WebSocket.
4. [Análisis de seguridad](04-seguridad.md) — hallazgos, priorizados, con estado actual.
5. [Análisis de performance](05-performance.md) — hallazgos, priorizados, con estado actual.
6. [Operaciones y scripts](06-operaciones-y-scripts.md) — mantenimiento, migraciones, ETL.
7. [Plan de acción](07-plan-de-accion.md) — orden de ejecución de todos los hallazgos, con
   estado por fase.
8. [Plan de refactor de dashboard.py](08-plan-refactor-dashboard.md) — cómo se seccionó el
   archivo de 2910 líneas en routers por dominio (✅ completo).
9. [Plan de refactor del motor de alertas](09-plan-refactor-alertas.md) — cómo se
   reestructuró `alerts_engine.py` en un paquete por dominio con un orquestador central,
   para agregar detectores nuevos más fácil (✅ completo).
10. [Contrato de ingesta del agente](10-contrato-ingesta-agente.md) — qué estructura de
    datos exacta tiene que mandar el agente en `POST /v1/hospital-status` para que el
    servidor lo acepte y genere alertas/KPIs correctamente.
11. [Plan de autenticación del agente por token](11-plan-auth-ingesta-agente.md) — diseño
    decidido para resolver [S2](04-seguridad.md#s2) de forma escalonada por versión de
    agente, sin cortar ingesta de los hospitales que todavía no migraron (⏸️ diseño
    aprobado, sin ejecutar).

## Estado general — 2026-09-10

- **Fase 0 y Fase 1 del plan de acción: ✅ completadas y verificadas en producción.** Las
  dos credenciales reales filtradas en el historial de git (token de Asana, contraseña de
  un empleado) fueron rotadas y los archivos que las contenían, borrados. Los 8 hallazgos
  de seguridad/performance de esfuerzo bajo-medio (JWT en el body, rate limit de login solo
  por IP, CSV injection, ciclo de alertas bloqueando el event loop, índice faltante,
  contraseña temporal compartida, ruta interna duplicada, falta de `requirements.txt`)
  están resueltos.
- **Fase 2: ⏸️ pausada a pedido.** Quedan pendientes los dos hallazgos que requieren más
  diseño y coordinación externa: autenticar el endpoint de ingesta (`/v1/hospital-status`,
  necesita actualizar los agentes de los hospitales) y limitar el tamaño de payload.
- **Reorganización de `dashboard_app/dashboard.py`: ✅ completa.** Pasó de 2910 a 195
  líneas, repartido en `dashboard_app/core.py` + 10 routers por dominio bajo
  `dashboard_app/routers/`. Mismo comportamiento, mismas 74 rutas — verificado paso a paso,
  local y en producción. Detalle en [08](08-plan-refactor-dashboard.md).
- **Dos hallazgos nuevos, encontrados durante el refactor, sin arreglar a propósito** (el
  refactor fue solo reorganización de código): `/api/v1/nodos-hospitalarios` está roto
  (llama a una función inexistente, devuelve 500 siempre), y hay ~380 líneas de código
  muerto duplicadas contra `generator_report.py`.
- **Fuera del plan de seguridad/performance**: se arreglaron los mapas de Leaflet que
  mostraban el watermark "API KEY REQUIRED" (CARTO empezó a exigir key para uso anónimo;
  se migró a tiles de OpenStreetMap en los 4 mapas de la app).

Ver [07-plan-de-accion.md](07-plan-de-accion.md) para el detalle de qué falta y por qué
está pausado, y [04-seguridad.md](04-seguridad.md) / [05-performance.md](05-performance.md)
para el estado hallazgo por hallazgo.

## Resumen técnico

- **Stack**: Python 3 / FastAPI / SQLAlchemy / SQLite (WAL) / Jinja2 / Uvicorn.
  `requirements.txt` en la raíz, generado con `pip freeze` real desde producción.
- **Proceso único** (`server.py`) que fusiona tres aplicaciones FastAPI: el listener de
  ingesta de agentes (`main.py`), el dashboard web (`dashboard_app/`, ver §8) y,
  opcionalmente, el router de informes con IA (paquete externo `informes_ia`).
- **Un solo hilo de background** (`ciclo_vigilancia`, cada 60s) corre el motor de alertas
  (en `asyncio.to_thread` desde la Fase 1), el mantenimiento de DB (diario) y la limpieza
  de alertas huérfanas (cada 12h).
- **Auth**: JWT en cookie httpOnly + bcrypt + RBAC centralizado en `permissions.py`, con
  scoping por hospital para el rol `Cliente`, y rate limit de login por IP y por cuenta
  desde la Fase 1.
