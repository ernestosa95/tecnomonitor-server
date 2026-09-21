# Análisis de seguridad

**Estado — 2026-09-10**: de los 10 hallazgos de este análisis, **7 ya están resueltos**
(Fase 1, ejecutada y verificada en producción el mismo día del análisis) — quedan
pendientes S2 (ingesta sin auth) y S5 (schema sin validar tamaño), que forman la Fase 2 del
plan y están **pausados a pedido** hasta coordinar el rollout con los agentes de los
hospitales. Cada hallazgo dice su estado actual en su propia sección; el resumen priorizado
al final también lo refleja. Ver [07-plan-de-accion.md](07-plan-de-accion.md) para el plan
completo de remediación y su estado por fase.

Metodología: lectura completa de `auth.py`, `permissions.py`, `main.py`, `server.py`,
`database.py`, y del código fuente de `dashboard_app/dashboard.py` (~74 endpoints),
`alerts_engine.py`, `asana_conector.py`, más búsqueda dirigida (grep) de patrones de riesgo
(SQL crudo, `eval`/`exec`/`pickle`, secretos hardcodeados, `|safe`, subida de archivos,
CORS) en todo el repositorio. No se hizo pentesting activo contra un servidor en vivo.

**Lo positivo primero**, porque es real y no es poco: JWT con secreto obligatorio por env
var (falla al arrancar si falta, sin fallback inseguro), passwords con bcrypt, cookie
httpOnly + Secure + SameSite=Lax, RBAC centralizado en un único archivo de datos
(`permissions.py`) del que derivan backend y frontend, scoping por hospital para clientes
externos vía `require_hospital_access`, cabeceras de seguridad (`X-Frame-Options`,
`X-Content-Type-Options`), rate limiting en login y en generación de PDFs, y prácticamente
todo el SQL crudo del repo usa parámetros bindeados (`text(...)` + dict de params) — no se
encontró inyección SQL explotable en el código de producción. Se nota una app mantenida por
alguien que itera sobre sus propios bugs de seguridad (hay comentarios "FIX" a lo largo del
código documentando por qué se arregló algo).

Dicho eso, estos son los hallazgos:

---

## <a name="s1"></a>S1 — CRITICAL — Token de Asana comprometido en el historial de git

**✅ RESUELTO — Fase 0.** Token rotado en Asana y archivo borrado (`Accesorios/limpiar_alertas.py`
ya no existe). Pendiente solo si el repo se comparte/hace público: reescribir el historial de git.

**Dónde (al momento del hallazgo)**: `Accesorios/limpiar_alertas.py:10`
```python
ASANA_ACCESS_TOKEN = '2/1204918676406253/1212721843116117:412402d867f1baad79ddbe7138761cb7'
```
Presente desde el primer commit (`d064328`) y por lo tanto en todo el historial, aunque se
borre el archivo hoy.

**Escenario**: cualquiera con acceso de lectura al repo (incluido en GitLab si el proyecto
se vuelve público, se comparte, o hay una fuga de credenciales de un colaborador) obtiene un
token real de la API de Asana, con el que puede leer/crear/modificar/borrar tareas en el
workspace de Asana usado para gestión de tickets de soporte — incluyendo, potencialmente,
información de clientes en las descripciones de esas tareas.

**Contexto que agrava esto**: la versión "buena" ya existe — `limpiar_alertas.py` en la
raíz del repo fue corregido para leer el token desde `ASANA_ACCESS_TOKEN` (env var) y
fallar explícitamente si no está seteada. La copia vieja en `Accesorios/` (usada como
carpeta de scripts sueltos, ver [06](06-operaciones-y-scripts.md)) nunca se borró.

**Acción recomendada** (en este orden):
1. Rotar/revocar el token en Asana (Admin Console → Apps → Personal Access Tokens) **ya**,
   independientemente de lo que se haga con el repo.
2. Borrar `Accesorios/limpiar_alertas.py` (ya es un duplicado obsoleto del de la raíz).
3. Si el repo se va a compartir o hacer público en algún momento, reescribir el historial
   (`git filter-repo` o BFG) para eliminar el token, o directamente crear un repo nuevo sin
   ese historial.

---

## <a name="s1b"></a>S1b — CRITICAL — Contraseña real de un empleado en texto plano en el historial de git

**✅ RESUELTO — Fase 0.** Contraseña cambiada (vía el panel de Admin, con contraseña
temporal aleatoria) y `user.py`/`Accesorios/user.py` borrados. Pendiente solo si el repo se
comparte/hace público: reescribir el historial de git.

**Dónde (al momento del hallazgo)**: `user.py:14,21` y su duplicado byte a byte `Accesorios/user.py:14,21`.
```python
usuario = db.query(UserModel).filter(UserModel.email == "gianluca.levrero@tecnoimagen.com.ar").first()
...
usuario.hashed_password = get_password_hash("gle.123")
...
usuario.must_change_password = False
```
Es un script ad-hoc ("parche puntual") que quedó commiteado con el email real de un empleado
y la contraseña real en texto plano que se le asignó, para una cuenta con rol
`decision_lider`. Además, `must_change_password` se fuerza explícitamente a `False`, así que
el sistema no iba a obligar a cambiar esa contraseña en el primer login — si nadie la
cambió manualmente después, sigue siendo la contraseña vigente de esa cuenta.

**Escenario**: igual que S1, cualquiera con acceso de lectura al repo (pasado o presente)
tiene un par email/contraseña real y utilizable para intentar iniciar sesión con esa cuenta,
sin necesidad de fuerza bruta.

**Acción recomendada**:
1. Contactar a ese usuario y forzar el cambio de contraseña **ya**, independientemente de lo
   que se haga con el repo.
2. Borrar `user.py` y `Accesorios/user.py` (son scripts de un solo uso, ya ejecutados, sin
   valor operativo — si se necesita repetir la operación, usar `create_user.py` que sí pide
   confirmación interactiva y no hardcodea credenciales).
3. Mismo tratamiento de historial de git que en S1 si el repo se comparte o se hace público.

---

## <a name="s1c"></a>S1c — MEDIUM — Contraseña temporal compartida e igual para todos los usuarios nuevos

**✅ RESUELTO — Fase 1.** `create_user.py` ahora genera una contraseña aleatoria de 14
caracteres por usuario (`secrets.SystemRandom()`, mismo criterio que el panel de Admin) y
fuerza `must_change_password=True` en los 3 flujos (alta, reactivación, reset).

**Dónde (al momento del hallazgo)**: `create_user.py:42` — `PASSWORD_TEMPORAL = "Tecno2026."`, reutilizada para **todo**
usuario nuevo o reseteado por este script (líneas 135, 146, 156, 166, 229-238), y además
impresa por consola/log en cada uso.

**Escenario**: cualquiera que conozca este patrón (visible en el propio repo) puede intentar
loguearse con `Tecno2026.` contra cualquier cuenta recién creada o reseteada, durante la
ventana entre la creación y el primer cambio de contraseña real por parte del usuario. El
riesgo depende de que `must_change_password` se aplique de forma consistente en el flujo de
login (no verificado en este análisis); aun si se aplica, la ventana de exposición no es
cero.

**Acción recomendada**: generar una contraseña temporal aleatoria por usuario con
`secrets.SystemRandom()` (patrón que ya existe en `dashboard_app/dashboard.py` para el flujo
de reseteo desde el panel de Admin) en vez de una constante fija, y evitar imprimirla en
logs persistentes.

---

## <a name="s2"></a>S2 — HIGH — Endpoint de ingesta sin autenticación

**✅ RESUELTO (parcial, escalonado) — 2026-09-11.** Token por hospital (SHA-256, generado
desde el panel), exigido vía `Authorization: Bearer <token>` **solo** para
`schema_version 4.5` en adelante -- las versiones viejas (`3.0`-`4.3`) siguen sin pedir
nada, así los ~80 hospitales migran de a uno a medida que se les actualiza el agente, sin
coordinar una fecha de corte global. Plan completo y detalle de implementación en
[11-plan-auth-ingesta-agente.md](11-plan-auth-ingesta-agente.md); el lado del agente ya
está documentado en [10-contrato-ingesta-agente.md §2bis](10-contrato-ingesta-agente.md#2bis--autenticación-por-token--a-partir-de-schema_version-45-vigente).
Sigue habiendo ~80 hospitales sin token porque el servidor no puede forzarles el upgrade de
agente -- el riesgo baja a cero recién cuando todos migren a 4.5.

**Dónde**: `main.py:82`, `POST /v1/hospital-status`.

Para `schema_version` viejo (`3.0`-`4.3`) sigue sin haber ningún `Depends(...)` de
autenticación ni verificación de un shared secret / API key de agente -- eso no cambia
hasta que esos hospitales migren. Tampoco hay rate limiting todavía (el `Limiter` de
`slowapi` vive en `dashboard_app/dashboard.py` y no se aplica a las rutas de `main.py`; ver
[S5](#s5) y el ítem 2.2 de [07-plan-de-accion.md](07-plan-de-accion.md)).

**Escenario de explotación**: cualquiera que conozca (o adivine — los IDs de hospital
siguen patrones simples: `H01`..`H46`, `P01`..`P26`) la URL pública del endpoint puede:
- Inyectar reportes falsos con `host_status: "OK"` para un hospital real, silenciando
  alertas reales de una caída (el motor de alertas evalúa lo último que llegó).
- Inyectar reportes falsos en estado crítico para generar ruido / tickets espurios en Asana
  contra el equipo de soporte.
- Enviar payloads grandes repetidamente para llenar disco (`full_json_data` no tiene límite
  de tamaño, ver [S5](#s5)) o saturar el proceso.

**Hecho**: agregado un secreto compartido por hospital (`ingest_token_hash` en
`hospitales_metadata`, ver [11](11-plan-auth-ingesta-agente.md)). **Pendiente**: rate
limiting por IP/hospital_id a este endpoint (ítem 2.2 del plan de acción) -- sigue siendo el
endpoint más expuesto de todo el sistema porque, por diseño, tiene que ser alcanzable desde
redes hospitalarias externas.

---

## <a name="s3"></a>S3 — MEDIUM — CSV/Formula Injection + falta de rate limit en `/submit-lead` y `/v1/generar-reporte-ris`

**✅ RESUELTO — Fase 1.** Campos de `/submit-lead` saneados contra formula injection
(comilla simple si empiezan con `=+-@`/tab/CR) y ambos endpoints con `@limiter.limit(...)`
(10/min y 5/min respectivamente). Ambas rutas viven ahora en
`dashboard_app/routers/paginas_publicas.py` e `informes.py` tras el refactor de
organización.

**Dónde (al momento del hallazgo)**: `dashboard_app/dashboard.py:1585-1621` (`/submit-lead`) y
`dashboard_app/dashboard.py:2071-2108` (`/v1/generar-reporte-ris`).

Ambos son endpoints **públicos** (el segundo tiene el `Depends(auth.get_current_user)`
comentado explícitamente — línea 2076) sin `@limiter.limit(...)`.

- `/submit-lead` escribe campos de un formulario público directo a un CSV
  (`leads_evento_links.csv`, `csv.writer`), sin sanitizar. Si algún campo empieza con `=`,
  `+`, `-` o `@`, y ese CSV se abre después en Excel/Sheets (uso típico de un archivo de
  leads), se ejecuta como fórmula (CSV/Formula Injection, CWE-1236) — puede filtrar datos o,
  con macros habilitadas, ejecutar código en la máquina de quien lo abre.
- Ninguno de los dos limita tamaño de request ni frecuencia: se pueden llamar en loop desde
  un script para llenar disco (`leads_evento_links.csv` crece sin cota) o para forzar
  generación repetida de PDFs (matplotlib + reportlab, CPU-intensivo) como vector de
  agotamiento de recursos.

**Acción recomendada**: anteponer los campos con comilla simple si empiezan con
`=+-@` antes de escribirlos al CSV (o migrar a una tabla de DB), y agregar
`@limiter.limit(...)` a ambos endpoints igual que ya se hizo en `/api/informes/pdf` y en
login.

---

## <a name="s4"></a>S4 — MEDIUM — Rate limiting de login solo por IP, no por usuario/cuenta

**✅ RESUELTO — Fase 1.** Nueva tabla `login_attempts_email` (mismo mecanismo que
`LoginAttempt`, pero por cuenta) — el login ahora bloquea por IP **y** por cuenta,
lo que corta el escenario de fuerza bruta distribuida descripto abajo. Lógica de login
ahora en `dashboard_app/routers/auth.py` tras el refactor de organización.

**Dónde (al momento del hallazgo)**: `database.py:202-206` (`LoginAttempt`, PK = `ip`), lógica en
`dashboard_app/dashboard.py:362-451`.

**Escenario**: un atacante con acceso a múltiples IPs (botnet, proxies rotativos, IPv6) puede
hacer fuerza bruta sobre la contraseña de una cuenta específica sin activar nunca el bloqueo,
porque el contador es por IP de origen, no por email/usuario objetivo. Además, dentro de una
misma organización detrás de un NAT (una IP pública compartida), un usuario que falla su
login varias veces puede bloquear sin querer a todos sus compañeros que entran desde la
misma IP.

**Acción recomendada**: agregar un segundo contador por `email`/`username` (o combinado
IP+usuario), con el mismo mecanismo de bloqueo temporal ya implementado.

---

## <a name="s5"></a>S5 — MEDIUM — `AgentReportV4` sin validación estructural real y sin límite de tamaño de payload

**🟡 PARCIAL — 2026-09-21.** Aplicado el límite de tamaño de body en la ruta de ingesta (2 MB,
`main.py::_leer_json_limitado`, responde `413`). **Sigue pendiente** el tipado estricto de
`physical_layer`/`virtual_layer` en V4. Ver
[07-plan-de-accion.md](07-plan-de-accion.md) Fase 2, ítem 2.2.

**Dónde**: `schemas.py:164-171` (`envelope`, `physical_layer`, `virtual_layer` tipados como
`Dict[str, Any]` / `List[Dict[str, Any]]`) + `main.py` (no hay `Content-Length` máximo
configurado en la app ni en el endpoint).

A diferencia de `AgentReportV3` (fuertemente tipado), el schema V4 —el que reciben en la
práctica los agentes modernos— básicamente no valida forma ni tamaño. Combinado con S2 (sin
auth), un payload gigante o profundamente anidado pasa la validación de Pydantic sin
problema y termina completo en la columna `full_json_data` (tipo `JSON`, sin límite) de
`reportes_historicos`.

**Medición (2026-09-21, P03, agente 4.5.1 con `mirth_topology`).** Estimada desde las tablas del
server, porque el agente no registra lo que envía y el server no guarda el `Content-Length`. El
server separa el reporte al guardarlo (`full_json_data` ya no incluye `software_monitoring` ni
`application_metrics`), así que se suman las partes:

| Parte | KB guardados |
|---|---|
| Infraestructura (`full_json_data`; 256 reportes en 24 h, promedio = máximo) | 9,1 |
| KPIs (`application_metrics`; 1 solo reporte con KPIs en 24 h) | 0–0,5 |
| `software_monitoring` (Mirth 2,1 · DICOM 0,2 · SSL 0,1) | 2,4 |
| `mirth_topology` (14 canales) | 21,5 |

Total guardado ≈ 33 KB. Las filas de software y topología no llevan los nombres de clave del JSON
que sí viajan en el body, así que con un factor conservador de ×1,5 sobre esas dos partes el body
real ronda **45–50 KB**. La topología pesa ~1,5 KB por canal (~2,3 KB en el cable) y es ~65 % del
payload; los hospitales con Mirth vistos hoy tienen entre 11 y 14 canales. Según
[13](13-contrato-topologia-mirth.md) se adjunta en cada ciclo desde la copia en caché del agente.

**Límite recomendado: 2 MB** (~40 veces lo medido; equivale a ~870 canales). Con 1 MB también
sobra para P03, pero un rechazo (HTTP 413) es caro: el hospital pasa a verse offline y, según el plan
del agente (§1.3), el agente reintenta el mismo bloque en cada ciclo. Como la ingesta ya exige token,
el riesgo de abuso que cubre el límite es menor que el de un falso rechazo. Aplica a
`POST /v1/hospital-status`.

**Peor caso en el resto de los hospitales (2026-09-21).** KPIs, últimos 30 días: el máximo por
reporte es 3,1 KB (H05; siguen H02 2,9 · P23 2,8 · H03 2,7 · H07 2,6), sin ningún lote que se
dispare. Infraestructura, últimas 300 filas de `reportes_historicos`: el máximo es 12,0 KB (PMMN;
luego H07 10,4). Sumando los máximos con la topología de 14 canales, el peor caso conocido ronda
**50–55 KB**: 2 MB deja ~35 veces de margen (1 MB, ~18 veces, también alcanzaría). Límites de la
medición: la muestra de infraestructura son los reportes más recientes (no el histórico completo;
en P03 el tamaño fue constante en 24 h) y solo P03 tiene hoy el agente con `mirth_topology`, de modo
que los demás hospitales sumarán esa topología cuando se actualicen.

**Acción recomendada**: agregar un límite de tamaño de body a nivel de Nginx/Starlette
(ej. 1-2 MB, generoso para un reporte de telemetría legítimo) y, si es viable, recuperar
algo de tipado estricto para `physical_layer`/`virtual_layer` en V4 en vez de aceptar
`Dict[str, Any]` sin más.

---

## <a name="s6"></a>S6 — LOW — El JWT se devuelve también en el body de `/api/login`

**✅ RESUELTO — Fase 1.** Sacado `"token": token` de la respuesta. De paso se encontró y
sacó una línea muerta en `login.html` que guardaba ese token en `sessionStorage` sin que
nada lo leyera — la sesión real siempre fue por la cookie httpOnly. Ahora en
`dashboard_app/routers/auth.py` tras el refactor de organización.

**Dónde (al momento del hallazgo)**: `dashboard_app/dashboard.py:426-435`.

La cookie httpOnly ya resuelve el caso de uso (el frontend actual, revisado en
`static/script.js`, no lo guarda en `localStorage`/`sessionStorage` — solo persiste ahí
datos de UI como nombre/rol). Pero exponer el token también en el JSON de respuesta anula
parcialmente la protección de httpOnly contra robo vía XSS si en el futuro algún script de
página (analytics de terceros, un bug de XSS) llegara a inspeccionar esa respuesta.

**Acción recomendada**: quitar `"token": token` de la respuesta; el frontend ya no lo
necesita porque opera vía cookie.

---

## <a name="s7"></a>S7 — LOW — Ruta interna de trigger de WebSocket duplicada, protección frágil

**✅ RESUELTO — Fase 1.** Borrada la definición sin protección de `dashboard_app/dashboard.py`
(ahora `dashboard_app/routers/websocket.py` tras el refactor de organización, sin esa
ruta). `server.py` queda como única fuente de verdad.

**Dónde (al momento del hallazgo)**: `server.py:203-210` (chequea `request.client.host == "127.0.0.1"`) vs.
`dashboard_app/dashboard.py:352-356` (mismo path, **sin** chequeo).

Ambas apps definen `POST /api/internal/trigger-ws`. Como `master_app.mount("/",
dashboard_app)` se registra *después* de las rutas propias de `master_app` en `server.py`,
la versión protegida gana en producción — pero es un accidente de orden de registro, no una
garantía explícita. Un refactor futuro que cambie el orden de montaje, o que sirva
`dashboard_app` de forma standalone (como ya soporta hacerlo el propio
`dashboard_app/compiler.txt` para desarrollo: `uvicorn dashboard:app --port 8001`),
expondría la ruta sin protección, permitiendo a cualquiera disparar broadcasts arbitrarios
por WebSocket a todos los clientes conectados.

**Acción recomendada**: borrar la definición sin protección en `dashboard_app/dashboard.py`
y dejar una sola fuente de verdad (la de `server.py`), o mover el chequeo de IP a un
decorator/dependency reutilizable en ambos lugares.

---

## <a name="s8"></a>S8 — LOW — Falta `requirements.txt`/`pyproject.toml`

**✅ RESUELTO — Fase 1.** `requirements.txt` generado y luego reemplazado por la salida
real de `pip freeze` corrida en producción (versiones fijadas de verdad, no el best-effort
inicial basado en imports).

No es una vulnerabilidad de aplicación pero sí de cadena de suministro/reproducibilidad:
sin un manifiesto de dependencias con versiones fijadas, no hay forma de auditar
versiones vulnerables conocidas (CVE) de las ~15 librerías de terceros que usa el proyecto
(`fastapi`, `python-jose`, `passlib`, `sqlalchemy`, etc.), ni de reproducir el mismo entorno
en otra máquina. Ver más en [06-operaciones-y-scripts.md](06-operaciones-y-scripts.md).

---

## Resumen priorizado

| # | Hallazgo | Severidad | Estado |
|---|---|---|---|
| S1 | Token Asana filtrado en git | CRITICAL | ✅ Resuelto (Fase 0) |
| S1b | Contraseña real de un empleado filtrada en git | CRITICAL | ✅ Resuelto (Fase 0) |
| S2 | Ingesta sin auth | HIGH | ⏸️ Pendiente (Fase 2, pausada) |
| S3 | CSV injection + sin rate limit en endpoints públicos | MEDIUM | ✅ Resuelto (Fase 1) |
| S4 | Rate limit de login solo por IP | MEDIUM | ✅ Resuelto (Fase 1) |
| S5 | Schema V4 sin validar forma/tamaño | MEDIUM | 🟡 Parcial: límite de tamaño (2 MB) aplicado; falta el tipado estricto |
| S1c | Contraseña temporal fija compartida para altas nuevas | MEDIUM | ✅ Resuelto (Fase 1) |
| S6 | JWT duplicado en body de login | LOW | ✅ Resuelto (Fase 1) |
| S7 | Ruta interna duplicada sin protección | LOW | ✅ Resuelto (Fase 1) |
| S8 | Sin manifiesto de dependencias | LOW | ✅ Resuelto (Fase 1) |

Detalle de ejecución (fechas, verificación, qué se probó) en
[07-plan-de-accion.md](07-plan-de-accion.md).
