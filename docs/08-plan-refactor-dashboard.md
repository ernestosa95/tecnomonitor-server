# Plan de refactor — seccionar `dashboard_app/dashboard.py`

**✅ Refactor completo — 2026-09-10.** Los 10 pasos están hechos y verificados (checklist +
producción en cada uno). `dashboard_app/dashboard.py` pasó de **2910 a 195 líneas** (93% de
reducción), repartido en `core.py` + 10 módulos bajo `routers/`. Las 74 rutas de la app
(76 originales, dos ya se habían eliminado en la Fase 1 de seguridad) siguen respondiendo
igual en cada paso, verificado con `TestClient` local y contra producción real. Este
documento queda como referencia de cómo se hizo y qué se encontró en el camino.

`dashboard_app/dashboard.py` tenía 2910 líneas y 76 endpoints (login, dashboard de red,
ABM de hospitales/usuarios/clientes, alertas, exclusiones, informes/PDF, estado de
software, solicitudes de acceso, páginas de marketing...) todo en un solo módulo. Este
documento es el plan que se siguió para partirlo en routers de FastAPI por dominio, sin
cambiar comportamiento.

## 1. Qué NO cambia (restricciones duras)

- `dashboard_app/dashboard.py` sigue existiendo y sigue exportando `app` (una instancia de
  `FastAPI`) con el mismo nombre. Es lo único que `server.py:35` importa
  (`from dashboard_app.dashboard import app as dashboard_app`) — confirmado que **nada más
  del repo** importa símbolos internos de `dashboard.py` (ni `manager`, ni `limiter`, ni los
  caches en memoria) desde afuera del archivo.
- Mismas rutas, mismos métodos, misma auth por ruta, mismas respuestas. Este refactor es
  moving code, no rewriting logic.
- `server.py` sigue montando `dashboard_app` igual que hoy (`master_app.mount("/",
  dashboard_app)`) — no se toca `server.py`.

## 2. Arquitectura objetivo

```
dashboard_app/
  dashboard.py          # SOLO composición: crea `app`, middleware, mounts,
                         # app.include_router(...) x 12. De 2910 líneas a ~150-200.
  core.py                # NUEVO — lo poco que comparten 2+ routers:
                         #   get_db(), templates (Jinja2Templates), limiter (slowapi)
                         #   + su exception handler.
  routers/
    __init__.py
    paginas_publicas.py
    websocket.py
    auth.py
    resumen_red.py
    hospitales_metadata.py
    hospital_detalle.py
    alertas_config.py
    informes.py
    usuarios.py
    clientes.py
    solicitudes_acceso.py
    misc.py
```

`core.py` queda chico a propósito: `get_db`, `templates` y `limiter` son los únicos objetos
que de verdad necesitan 2+ routers al mismo tiempo. Todo lo demás (constantes, DTOs,
helpers) es específico de un dominio y viaja con su router, no con el core — así se evita
convertir `core.py` en un segundo archivo monolítico.

Los middlewares (CORS, GZip, cabeceras de seguridad, el `@app.exception_handler` de
rate-limit) se quedan en `dashboard.py`: en FastAPI los middleware se agregan sobre la
instancia de `app`, no tiene sentido moverlos a un router.

## 3. Mapeo ruta → router

Basado en los 76 `@app.*` actuales (línea exacta al momento de este plan). Los marcados
con ⚠️ son los que conviene confirmar leyendo el cuerpo de la función antes de moverlos
(el nombre sugiere un dominio pero no lo verifiqué línea por línea).

| Router nuevo | Contenido | Líneas actuales (aprox.) | Tamaño |
|---|---|---|---|
| `paginas_publicas.py` | `/herramientas`, `/ris-analytics`, `/prov-analytics`, `/hl7-analytics`, `/pacs-capacity`, `/salta-project`, `/renovacion`, `/demo-pacs`, `/tecno-solution`, `/submit-lead` + `_sanear_campo_csv` | 1577-1665 | ~180 |
| `websocket.py` | `ConnectionManager`, `manager`, `/ws/alertas` | 316-357 | ~25 |
| `auth.py` | `/`, `/api/login`, `_registrar_intento_fallido`, `/api/logout`, `/monitor`, `/beta` | 338-483, 359-362, 2248-2253 | ~170 |
| `resumen_red.py` | `_normalizar_provincia`, `_clasificar_proyecto`, `CENTROIDES_PROVINCIAS`, `_CANON_PROVINCIAS`, `_cache_resumen`/`_cache_provincias`, `/api/resumen-hospitales`, `/api/provincias`, `/api/mapa-data`, ⚠️ `/api/v1/nodos-hospitalarios` | 87-130, 489-693, 1113-1165, 2690-2697 | ~400 |
| `hospitales_metadata.py` | `HospitalDTO`, `ManualKPIDTO`, CRUD + 4 toggles de `/api/hospitales-metadata`, manual-kpi | 290-301, 975-1113 | ~250 |
| `hospital_detalle.py` | `/api/hospital/{id}`, `/history`, `/kpi-history`, `_estado_colas_dicom`, `/software` (268 líneas, el más grande de todos), `/api/logs-dictionary/{id}`, `/kpi-settings` (GET/POST) | 693-870, 1762-2248 | ~460 |
| `alertas_config.py` | `ConfigRequest`, `ExclusionRequest`, `/api/alertas`, `/api/config` (GET/POST), `_validar_exclusion`, `_parse_expira`, `/api/exclusiones*` (CRUD+preview), `_cerrar_alertas_por_regla` | 228-286, 860-975, 2720-2910 | ~360 |
| `informes.py` | `ReportePDFRequest`, gráficos (dona/temporal/temperaturas), `/api/informes/pdf`, `/api/informes/historial`, `/v1/generar-reporte-ris`, ⚠️ `/api/users/responsables` | 302-309, 1165-1577, 2113-2169 | ~460 |
| `usuarios.py` | `ChangePasswordRequest`, `validar_password`, `/api/user/change-password`, `/api/usuario/perfil` (GET/PUT), `/api/me/permissions`, ABM `/api/admin/usuarios/*` | 1666-1725, 2254-2343, 2436-2545 | ~300 |
| `clientes.py` | DTOs de cliente, `_generar_password_temporal`, `_serializar_accesos`, `/api/admin/clientes/*`, `/cliente`, `/api/cliente/casos/{id}` | 2310-2436, 2421-2436, 2671-2690 | ~250 |
| `solicitudes_acceso.py` | Los **dos** flujos de alta: `/api/user/request-access` (legacy, dispara Asana directo) + `/api/access-requests`/`/api/hospitales-publico`/`/api/admin/access-requests`/aprobar/rechazar (nuevo, con tabla y cola de aprobación) | 1719-1745, 2545-2671 | ~200 |
| `misc.py` | ⚠️ `/beta/simulador` (y cualquier ruta suelta que no encaje en los anteriores al momento de extraer) | 2697-2751 | ~50 |

**No es un bug, pero avisá si querés que lo toquemos aparte**: `/api/user/request-access` y
`/api/access-requests` son dos sistemas de solicitud de acceso genuinamente distintos que
conviven (uno viejo sin persistencia, uno nuevo con `AccessRequestModel` + aprobación). Este
refactor los deja tal cual, agrupados en el mismo archivo por afinidad temática — consolidarlos
en un solo flujo sería una tarea de producto aparte, no algo que decida un refactor de
organización de código.

## 4bis. Checklist de funcionamiento (se corre después de CADA paso)

No hay suite de tests, así que esto combina lo que se puede automatizar sin infraestructura
extra con una revisión manual mínima. Orden sugerido:

1. **Sintaxis**: `python3 -m py_compile <archivos tocados>`.
2. **Import real** (no solo sintaxis): importar `dashboard_app.dashboard` con
   `JWT_SECRET`/`ASANA_ACCESS_TOKEN` dummy en el entorno, y contar rutas
   (`isinstance(r, (APIRoute, APIWebSocketRoute))`) — confirma que no hay `NameError`,
   import circular, ni ruta que se perdió al registrar.
3. **Import del entrypoint real**: lo mismo pero con `import server` (así se prueba
   `master_app`, el que realmente corre en producción, montaje incluido — no alcanza con
   probar solo `dashboard_app/dashboard.py` suelto).
4. **TestClient**: `fastapi.testclient.TestClient(server.master_app)` pegándole a cada
   endpoint que se movió en este paso, confirmando código 200 (o el que corresponda) y que
   el body no esté vacío/roto. Para endpoints que escriben algo (como `/submit-lead`),
   limpiar el artefacto de la prueba después (no dejar filas de test en CSVs/DB).
5. **Manual en el navegador, después de desplegar**: abrir cada página/endpoint movido en
   este paso desde el dominio real, confirmar visualmente que carga y que no quedó ninguna
   ruta duplicada respondiendo distinto (se puede repetir el diff de rutas contra
   `/openapi.json` en producción si hace falta más confianza).

## 4. Orden de migración (de menor a mayor riesgo)

Cada paso es un router completo, autocontenido, deployable solo. No se arranca el
siguiente paso sin haber verificado el anterior.

1. **`paginas_publicas.py`** — cero auth compleja, cero estado compartido. Sirve para
   validar el mecanismo de extracción (imports, `include_router`, verificación) con el
   riesgo más bajo posible antes de tocar algo importante.
   **✅ Hecho — 2026-09-10.** De paso se creó `dashboard_app/core.py` (necesario ya desde
   este primer paso: sin él, el router nuevo importaría de vuelta desde `dashboard.py` y
   generaría un ciclo). `dashboard.py` pasó de 2910 a 2819 líneas. Checklist corrida
   completa (sintaxis + import de `dashboard_app.dashboard` + import de `server.py` real +
   `TestClient` contra las 10 rutas movidas, las 9 GET en 200 y el POST de `/submit-lead`
   funcionando) — todo OK. Pendiente: la verificación manual en el navegador la hace el
   equipo después de desplegar (punto 5 del checklist).
2. **`websocket.py`** — chico y aislado (`ConnectionManager` no lo usa nadie más).
   **✅ Hecho — 2026-09-10.** Recorte quirúrgico: la ruta `/` (login) estaba físicamente
   en el medio del bloque de `ConnectionManager`/websocket y quedó en `dashboard.py` (le
   toca a `auth.py`, paso 9). `dashboard.py`: 2819 → 2791 líneas. Checklist completa OK
   (sintaxis, import de `dashboard_app.dashboard` con 74 rutas +1 WS sin cambios, import de
   `server.py` real, `TestClient` conectando el websocket + sanity check de `/` y
   `/monitor`). **Verificado en producción**: `/`, `/monitor`, `/herramientas` en 200, y
   conexión real a `wss://tecnomonitor.tecnoimagen.com.ar/ws/alertas` exitosa.
3. **`resumen_red.py`** — usa los caches en memoria; hay que confirmar que siguen siendo
   singletons de módulo (no se reinician en cada request) al moverlos.
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 2791 → 2484 líneas (el corte más grande hasta
   ahora, en 4 bloques no contiguos). Hallazgo aparte, no relacionado con el refactor:
   `/api/v1/nodos-hospitalarios` llama a `obtener_nodos_desde_db()`, una función que **no
   existe en ningún lado del repo** — bug preexistente, el endpoint devuelve 500
   (`NameError`) para cualquier usuario autenticado. Se movió tal cual, preservando el bug
   a propósito (este refactor no corrige comportamiento). Ver nota en el propio
   `resumen_red.py`. Queda pendiente decidir si se arregla aparte.
   Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios), y
   `TestClient` con **login real** (usuario de prueba creado en una DB SQLite local vacía,
   autogenerada) contra las 4 rutas — `/api/resumen-hospitales`, `/api/provincias` y
   `/api/mapa-data` en 200; `/api/v1/nodos-hospitalarios` reprodujo el mismo `NameError` que
   tenía antes de moverse, confirmando que el comportamiento (bug incluido) no cambió.
   **Verificado en producción**: las 4 rutas movidas responden 401 sin sesión (registradas
   y protegidas, sin 404/500 previos), y `/`, `/monitor`, `/prov-analytics`, `/herramientas`
   en 200. Falta la confirmación visual del equipo con datos reales logueado.
4. **`hospitales_metadata.py`** — ABM directo, sin lógica compleja.
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 2484 → 2334 líneas. Checklist: sintaxis OK,
   import de `dashboard_app.dashboard` (74 rutas sin cambios), y esta vez un ciclo CRUD
   funcional completo con `TestClient` + login real contra un hospital de prueba (crear →
   listar → editar → 3 toggles → get/set KPI manual → toggle-manual → borrar) — las 11
   operaciones respondieron 200 con el body esperado en cada paso.
   **Verificado en producción**: las 10 rutas movidas responden 401 sin sesión (registradas
   y protegidas), sanity check de `/`, `/monitor`, `/api/resumen-hospitales` OK. No se probó
   el ciclo de escritura contra producción (sin credenciales, y no corresponde crear/borrar
   datos reales) -- queda la confirmación visual del equipo con sesión real.
5. **`alertas_config.py`** — CRUD de exclusiones + config; ya lo tocamos hoy en Fase 1, así
   que está fresco.
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 2334 → 1966 líneas (primera vez por debajo de
   2000). Detalle no trivial encontrado al mover `_cerrar_alertas_por_regla`: el archivo
   original tenía la última línea (`return n`) sin salto de línea final, por lo que `wc -l`
   no la contaba -- casi se pierde en el corte. Se verificó con `tail`/`cat -A` antes de
   escribir el router nuevo, así que se preservó igual. De paso se confirmó que todo
   `dashboard.py` usa CRLF de punta a punta (detalle de codificación del archivo original,
   sin relación con el refactor; los routers nuevos quedan en LF, mezcla de estilos entre
   archivos que no afecta la ejecución).
   Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios), y
   ciclo funcional completo con `TestClient` + login real: `/api/alertas`, `/api/config`
   (GET + POST), y `/api/exclusiones` (preview → crear → editar → borrar) — las 9 llamadas
   en 200, incluyendo `alertas_cerradas: 0` (no `None`) confirmando que el `return n` quedó
   bien.
   **Verificado en producción**: las 8 rutas movidas responden 401 sin sesión, sanity check
   de `/`, `/monitor`, `/api/hospitales-metadata` OK.
6. **`usuarios.py`** y **`clientes.py`** — se hacen juntos porque comparten el patrón de
   `_generar_password_temporal` (mismo criterio que ya usamos en `create_user.py`, ver
   [04-seguridad.md#s1c](04-seguridad.md#s1c)); van en pasos separados pero uno después del
   otro.
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 1966 → 1614 líneas. Se descubrió una
   dependencia cruzada de tres routers, no solo dos: `_generar_password_temporal` y
   `ROLES_INTERNOS_VALIDOS` los usan `usuarios.py`, `clientes.py` **y** el todavía-no-movido
   flujo de `solicitudes_acceso` (paso 7) dentro de `dashboard.py`. Se resolvió moviendo
   ambos a `core.py` (encaja con su criterio: compartido por 2+ routers) y agregando en
   `dashboard.py` los imports que el código de `solicitudes_acceso` necesita para seguir
   funcionando sin cambios hasta que se mueva en el paso 7 (`from core import
   generar_password_temporal as _generar_password_temporal`, `ROLES_INTERNOS_VALIDOS`, y
   `_AccesoDTO` desde `routers.clientes`, porque `_AprobarClienteDTO` reutiliza ese DTO).
   Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios,
   incluidas las de `solicitudes_acceso` que dependen de las importaciones nuevas), y
   19 llamadas con `TestClient` + login real cubriendo perfil, cambio de contraseña, ABM de
   usuarios internos, ABM de clientes, y — como prueba específica de la dependencia
   cruzada — crear una solicitud de acceso y aprobarla (`aprobar-interno`, que usa
   `core.generar_password_temporal` y `core.ROLES_INTERNOS_VALIDOS`). Las 19 en 200.
   **Verificado en producción**: las 15 rutas API movidas responden 401 sin sesión, `/cliente`
   responde 307 (redirect, comportamiento correcto de esa ruta puntual), sanity check de
   `/`, `/monitor`, `/api/hospitales-metadata`, `/api/alertas` OK.
7. **`solicitudes_acceso.py`** — antes de moverlo, confirmar los dos flujos como se
   describió arriba (leer los cuerpos completos, no solo la firma).
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 1614 → 1455 líneas. Se confirmaron los dos
   flujos tal como se describieron en la sección 3 (no es un bug, son dos features): el
   legacy dispara Asana directo sin persistir nada, el nuevo persiste en
   `AccessRequestModel` con cola de aprobación. Se movieron los dos juntos al mismo router.
   De paso se sacaron de `dashboard.py` los imports puente que había agregado en el paso 6
   (`ROLES_INTERNOS_VALIDOS`, `generar_password_temporal`, `_AccesoDTO`) porque ya no los
   usa nada ahí -- quedaron encapsulados dentro de `solicitudes_acceso.py`, que importa
   `_AccesoDTO` directamente desde `routers/clientes.py`.
   Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios), y
   10 llamadas con `TestClient` + login real cubriendo el ciclo completo de ambos flujos
   (legacy, y nuevo con interno→aprobar, cliente→aprobar, y uno rechazado) — 9 en 200 y 1 en
   500 esperado (el legacy intenta de verdad la API de Asana, que en este entorno de prueba
   tiene un token falso; el 500 "Error al conectar con Asana" es el comportamiento correcto
   del código original ante esa falla, no una regresión).
   **Verificado en producción**: los dos POST públicos devuelven 422 con body vacío (validan
   antes de ejecutar Asana/DB, sin generar efectos reales), `/api/hospitales-publico` en
   200, las 4 rutas de admin en 401 sin sesión, sanity check de `/`, `/monitor`,
   `/api/admin/clientes` OK.
8. **`informes.py`** — el más pesado; considerar en un pase posterior (no en este refactor)
   empujar más lógica de generación de PDF/gráficos hacia `generator_report.py`, que ya
   existe como módulo separado, en vez de duplicar esa responsabilidad entre dos archivos.
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 1455 → **964 líneas** (por primera vez debajo
   de 1000). Hallazgo aparte, no relacionado con el refactor: `generar_grafico_dona`,
   `generar_grafico_temporal`, `generar_reporte_infra_pdf` y
   `generar_grafico_temperaturas_infra` están **duplicadas** en `generator_report.py` (con
   el mismo nombre), y las copias que vivían en `dashboard.py` no las llamaba nadie —
   `generar_reporte_pdf` delega directo a `generator_report.generar_pdf_infra/_clinico`, no
   a la copia local. Es código muerto preexistente (~380 líneas). Se movió tal cual a
   `informes.py`, preservando el comportamiento (o falta de uso) exacto; queda pendiente
   decidir si se borra en una limpieza aparte, ahora que ya no complica la lectura de
   `dashboard.py`.
   Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios), y
   4 llamadas con `TestClient` + login real: `/api/informes/historial` y
   `/api/users/responsables` en 200, `/api/informes/pdf` con un hospital sin datos devolvió
   el 400 "No hay datos para el periodo" esperado (confirma que delega bien a
   `generator_report.py`), y `/v1/generar-reporte-ris` generó un PDF real de ~2.9 MB de
   punta a punta (matplotlib + reportlab funcionando a través del router nuevo).
   Verificado en producción: las 3 rutas auth-protegidas en 401, `/v1/generar-reporte-ris`
   con body vacío en 422 (sin generar PDF real en prod), sanity check de `/`, `/monitor`,
   `/api/admin/access-requests` OK.
9. **`auth.py`** — se deja para el final a propósito: es el módulo de sesión/login, lo
   tocamos hoy mismo (rate limit por email, etc.) y conviene que ese código decante antes
   de moverlo de lugar.
   **✅ Hecho — 2026-09-10.** `dashboard.py`: 964 → 810 líneas. Detalle de nombres: este
   router se llama igual que `dashboard_app/auth.py` (el módulo de JWT/bcrypt/roles, que
   sigue existiendo tal cual y `dashboard.py` todavía usa en el resto del archivo). Se
   importó con alias (`from routers import auth as auth_router`) para no pisarlo -- se
   verificó explícitamente en runtime que `dashboard.auth` sigue siendo el módulo `auth` y
   `dashboard.auth_router` es `routers.auth`, sin colisión.
   Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios), y con
   `TestClient` + `server.py` real se probó el ciclo completo: páginas `/`, `/monitor`,
   `/beta` en 200; login con password incorrecta rechazado; login correcto genera sesión
   válida (confirmado con una llamada autenticada después); logout invalida la sesión de
   verdad (confirmado con un 401 en la misma llamada después del logout). Es el router más
   sensible de los nueve movidos hasta ahora y el que más se probó en profundidad.
   Verificado en producción: `/`, `/monitor`, `/beta` en 200; login con credenciales
   inválidas devuelve `{"success": false}` (no rompe, no bloquea nada real); logout sin
   sesión responde 200 sin efectos; sanity check de `/api/hospitales-metadata` e
   `/api/informes/historial` OK.
10. **`hospital_detalle.py`** — al final porque incluye `obtener_estado_software`, la
    función más grande del archivo (268 líneas). Extraerla del archivo es solo mover
    código; **partir esa función en helpers más chicos es una mejora aparte**, que se puede
    proponer como tarea siguiente una vez que ya esté en su propio router.
    **✅ Hecho — 2026-09-10. Último paso del refactor.** `dashboard.py`: 810 → **195
    líneas** (era un solo archivo de 2910 al empezar). Este era el bloque más grande y
    prácticamente todo lo que quedaba del archivo original, así que se extrajo con `sed`
    directo del archivo (no retipeado a mano) para no arriesgar un error de transcripción
    en la función de 268 líneas -- se verificó con `diff` que el contenido movido es
    **byte a byte idéntico** al original, salvo el decorador `@app.` → `@router.`.
    Quedó una sola ruta suelta en `dashboard.py` (`/beta/simulador`, un iframe embebido de
    17 líneas) que no ameritaba su propio router -- es la única excepción al "todo movido a
    `routers/`", documentada acá a propósito.
    Checklist: sintaxis OK, import de `dashboard_app.dashboard` (74 rutas sin cambios,
    mismo total que al principio del refactor), y con `TestClient` + login real + datos de
    prueba insertados a mano (un hospital, un reporte, un canal Mirth) se probaron los 7
    endpoints incluyendo `obtener_estado_software` con `minutos=0` y `minutos=60` -- los 9
    en 200 con contenido real, no solo resultados vacíos.
    **Verificado en producción**: las 6 rutas de hospital_detalle + `/beta/simulador` en 401
    sin sesión, sanity check de `/`, `/monitor`, `/beta`, `/api/hospitales-metadata` OK.
    **Refactor completo y confirmado en producción de punta a punta.**
    **Nota de scope**: los imports del tope de `dashboard.py` quedaron con bastante código
    muerto (varios ya no se usan tras mover todo su contenido a los routers, ej.
    `matplotlib`, `reportlab`, `numpy`, `generator_report`, etc. importados pero sin uso).
    Se dejaron tal cual a propósito -- limpiarlos es zero-risk en teoría pero exige trazar
    con cuidado el orden de carga (`sys.path.append` tiene que seguir corriendo antes que
    cualquier `import database` transitivo), y no es necesario para el objetivo de este
    refactor (ya se logró: de 2910 a 195 líneas). Queda como tarea de limpieza aparte si se
    quiere.

## 5. Cómo verificar que no se rompió nada (no hay test suite)

Al no haber tests automatizados, la verificación más barata y confiable es comparar el
esquema OpenAPI que FastAPI genera solo, antes y después de cada paso:

```bash
# Antes de tocar nada (o antes de cada paso):
curl -s http://localhost:8001/openapi.json | python3 -c "
import json,sys
d = json.load(sys.stdin)
for path, methods in sorted(d['paths'].items()):
    for m in methods:
        print(m.upper(), path)
" > /tmp/rutas_antes.txt

# Después del paso:
curl -s http://localhost:8001/openapi.json | python3 -c "..." > /tmp/rutas_despues.txt
diff /tmp/rutas_antes.txt /tmp/rutas_despues.txt
```

Un `diff` vacío significa que ninguna ruta se perdió, cambió de método o quedó duplicada —
que es exactamente el tipo de error mecánico que puede pasar al mover 76 endpoints entre
archivos. Además de esto, conviene un smoke test manual mínimo por paso: login, ver el
mapa/resumen de red, abrir un hospital, abrir el panel de admin correspondiente al router
que se acaba de mover.

## 6. Fuera de alcance de este refactor (anotado para después)

- Partir `obtener_estado_software` (268 líneas) en funciones más chicas — ver punto 10.
- Consolidar los dos flujos de solicitud de acceso en uno — ver nota del punto 3.
- Mover más lógica de PDF/gráficos de `dashboard.py` hacia `generator_report.py` — ver
  punto 8.
- Cualquier cambio de comportamiento, validación nueva, o fix — este refactor es
  exclusivamente reorganización de código.
