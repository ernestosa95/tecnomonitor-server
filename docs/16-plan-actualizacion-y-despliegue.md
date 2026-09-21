# Plan de actualización y despliegue — server + agente v4.5.1

**Estado: 🚧 en construcción — 2026-09-19.** Se está juntando el listado completo de
requerimientos; la priorización y las fases (§3) se arman recién cuando estén todos.
**Regla de trabajo: por ahora solo documentación — no se toca código hasta indicación explícita.**

Las referencias `archivo:línea` son de la fecha de este documento (commit `619ac62` del server
y `main` del agente); pueden correrse. Lo marcado *(inferido)* sale de leer el código, no de
reproducirlo.

---

## 1. Contexto y decisiones tomadas

- **Objetivo:** actualizar server y agente (v4.5.1) y redesplegar en todos los hospitales.
- **Repo como fuente de verdad:** se commitea desde el checkout de desarrollo y producción hace
  `pull` (antes se copiaban archivos a mano). Los commits del server están hechos en local, con
  push pendiente de confirmación.
- **Autenticación de ingesta por token (`schema_version 4.5`):** ya implementada en el server de
  producción y **verificada funcionando en P03** ([11](11-plan-auth-ingesta-agente.md)).
- **Solo P03 tiene hoy el agente nuevo.**
- **Despliegue por fases:** A) P03, verificando el checklist · B) un par de hospitales más ·
  C) masivo, por tandas. Cada fase con criterio de avance y de rollback (por definir en §3).
- `dashboard_app/templates/index.html` (`/monitor`) es la interfaz vieja, en desuso: **se retira**
  (decidido 2026-09-19, ver REQ-02).
- **Riesgo transversal — no hay entorno de pruebas.** Hoy los cambios se verifican con
  `TestClient`, reportes sintéticos en local y directamente en producción (así se hizo el
  refactor, [08](08-plan-refactor-dashboard.md)). Afecta a todos los requerimientos de §2. La
  propuesta está en REQ-02, Fase 0.

---

## 2. Registro de requerimientos

| ID | Título | Toca | Estado | Prioridad |
|---|---|---|---|---|
| REQ-01a | Alerta por reinicio de VM | server (motor de alertas) | definido, sin implementar | por definir |
| REQ-01b | Estado de las VMs cuando el hospital está offline | server (API) + frontend | definido, sin implementar | por definir |
| REQ-02 | Dividir los archivos monolíticos del frontend (viabilidad y plan) | server (frontend) | analizado; decisiones parciales tomadas; **retiro de `/monitor` hecho (2026-09-21)** | baja (propuesta) |
| REQ-03 | Reflejar en el server lo que se deja de monitorear en el agente | server + agente (ajuste mínimo, solo KPIs) | analizado; decisiones tomadas | por definir |
| REQ-05 | Chequeo de integridad de bases SQL Server tras un reinicio (`DBCC CHECKDB`) | agente (módulo nuevo) + server (solo ingesta) | implementado (2026-09-21): ingesta en el server y módulo del agente 4.5.2; **falta compilar el agente y validar en P03** | alta: entra en el release 4.5.2 del agente |
| REQ-04 | Mapa de integraciones Mirth: vista de flujo acumulado (ej. últimos 30 min) | server (frontend; API sin cambios en la opción base) | implementado (2026-09-21); el criterio del asterisco se corrigió tras la primera prueba en producción; falta validar la corrección | por definir |

### REQ-01 — Estado de las VMs: reinicios sin alerta y estado engañoso con el hospital offline

En este requerimiento, "VM" son los objetos de `virtual_layer[]` del reporte del agente (`type`
= `vm`, `ws` o `eq`; ver [contrato del agente §5](../../tecnomonitor-agent/docs/CONTRATO_AGENTE.md)):
son las que se ven como etiquetas en la fila del listado y como tarjetas en el detalle.
*Supuesto a confirmar:* las VMs de un hipervisor VMware (`physical_layer.vms`) no las lee ni el
server ni el frontend hoy y quedan fuera.

#### REQ-01a — Alerta por reinicio de VM

**Pedido:** hoy, si una VM se reinicia, el motor de alertas no avisa.

**Situación actual (evidencia)**
- El agente ya manda `virtual_layer[].telemetry.uptime_seconds` (contrato §5) y el frontend lo
  muestra como badge ⏱ (`script.js:1024`). **Ningún detector del server lo evalúa.**
- El motor solo tiene alerta de uptime para el **host físico**: `HOST_UPTIME`, WARNING si
  `uptime < 600 s` y se cierra sola cuando lo supera (`alerts_engine/infra.py:212-218`).
- Para las VMs, `_evaluar_reglas_v3` solo genera `VM_CPU_<id>`, `VM_RAM_<id>` y
  `DISK_<id>_<mount>` (`infra.py:271-293`).
- **Una VM caída tampoco genera alerta.** Cuando la recolección falla, el agente reporta
  `state: "Offline"` con `telemetry: {}` y un `state_reason` (`agent_logic.py:1663-1664`,
  `1857-1858`, `2061-2062`), pero el server no lee `state` ni `state_reason` en ningún detector.
  Con `telemetry` vacía, `VM_CPU_`/`VM_RAM_` salen con nivel OK (uso 0 %), lo que *(inferido)*
  podría hasta cerrar una alerta de CPU/RAM abierta justo cuando la VM se cae — a verificar en
  `estado.actualizar_estado_alerta`.
- `state_reason` (`ok` · `port_closed` · `wmi_error` · `ssh_error` · `wmi_timeout` ·
  `ssh_timeout` · `unknown`; [ENVELOPE_API.md:130](../../tecnomonitor-agent/docs/ENVELOPE_API.md))
  distingue "la VM no responde / está apagada" (`port_closed`) de "no pudimos acceder"
  (`*_error`, ej. credenciales). El server no lo consume.

**Comportamiento esperado (propuesta)**
1. Un hallazgo nuevo `VM_UPTIME_<id>` por cada VM, análogo a `HOST_UPTIME`: se abre cuando la
   VM reporta un uptime por debajo del umbral y se cierra sola al superarlo.
2. *(Por confirmar si entra en este requerimiento)* Un hallazgo `VM_OFFLINE_<id>` para VM caída:
   solo cuando `state == "Offline"` con `state_reason` `port_closed` o `*_timeout`, sostenido
   N reportes consecutivos. Los `*_error` (falla de acceso) **no** significan VM caída: van
   como aviso aparte o quedan fuera.

**Consideraciones de diseño**
- **Detección por umbral (opción 1, recomendada como base):** mismo patrón que `HOST_UPTIME`,
  sin estado nuevo. Límite: el umbral debe ser mayor que el intervalo de reporte del agente
  (`interval_minutes`, default 5 min) o un reinicio puede pasar entre dos reportes sin verse.
  Con 600 s alcanza para el default; un hospital con intervalo ≥ 10 min lo perdería.
- **Detección por evento (opción 2):** comparar el uptime contra el del reporte anterior
  (`actual < previo` ⇒ reinicio). No se pierde con intervalos largos, pero requiere guardar el
  último uptime por VM (o leer el reporte previo) y manejar el primer reporte y el reinicio del
  propio agente. Evaluarla solo si aparecen hospitales con intervalo largo.
- **Ruido:** `ws` (estaciones de trabajo) y `eq` (equipos médicos) se apagan y reinician por
  diseño (turnos, mantenimiento). El motor ya tiene exclusiones por hospital y patrón
  (`alerts_engine/exclusiones.py`, tabla `alert_exclusions`), que funcionarían sobre
  `VM_UPTIME_`/`VM_OFFLINE_` sin código extra.
- **Asana:** `actualizar_estado_alerta` recibe el proyecto y los seguidores, o sea que una alerta
  nueva puede generar ticket. Un reinicio de VM que abra ticket puede ser demasiado ruidoso.

**Decisiones abiertas**
1. ¿Qué tipos alertan? Propuesta: solo `vm`; `ws`/`eq` no, o NOTICE.
2. ¿Severidad? Propuesta: WARNING, igual que `HOST_UPTIME`.
3. ¿Genera ticket en Asana o solo aparece en el dashboard?
4. ¿Umbral fijo de 600 s o configurable desde el panel de alertas?
5. ¿Entra `VM_OFFLINE_<id>` (VM caída) en este mismo requerimiento?

#### REQ-01b — Estado de las VMs cuando el hospital está offline

**Pedido:** cuando el hospital pasa a offline, el listado lo marca con la etiqueta roja pero las
VMs siguen en verde; es un mensaje confuso.

**Situación actual (evidencia)**
- El estado del hospital en el listado se calcula **solo por tiempo**: minutos desde el último
  reporte contra `limitOfflineMinutes` (`script.js:556-569`).
- Las etiquetas de las VMs de la fila (`script.js:572-580`) salen de `h.elements`, que arma
  `/api/resumen-hospitales` leyendo `state` de cada VM **del último reporte**, sin mirar cuán
  viejo es (`routers/resumen_red.py:126-131`). Si el último reporte tenía la VM Online, sigue
  verde aunque hayan pasado horas.
- El detalle repite el patrón: `headerColor` sale de `vm.state` del último reporte
  (`script.js:1019-1021`), sin ninguna indicación de que el dato es viejo.
- Resultado: la fila dice "Offline" y las etiquetas dicen "todo bien", y ninguna de las dos es
  información cierta sobre las VMs.

**Comportamiento esperado (propuesta)**
- Con el hospital offline, las VMs **no** se muestran ni verdes ni rojas: se muestran en gris,
  "Sin datos", con el tooltip "Último dato hace X min". El rojo sería igual de incorrecto que
  el verde: no sabemos si la VM cayó o si perdimos la conexión con el hospital.
- Se mantiene la distinción: **rojo = el agente vio la VM caída** (hospital online y VM
  `Offline`), **gris = no hay datos**.
- En el detalle, banner "Sin conexión hace X min — se muestra la última lectura" y cabeceras de
  VM en gris.
- Al volver a reportar, los colores reales vuelven solos (el refresco en vivo ya existe).

**Consideraciones de diseño**
- **Fuente única del estado offline:** hoy la regla está repetida en cinco lugares:
  `infra._verificar_conectividad` (`infra.py:307`), `/api/mapa-data` (`resumen_red.py:337-361`),
  `mirth_mapa._hospital_offline` (`mirth_mapa.py:111`) y dos veces en `script.js` (`:560` y
  `:3526`). Lo ideal es que la API devuelva el dato ya resuelto (`offline` / minutos sin
  reportar, y VMs con estado `unknown`) y que el front no recalcule.
- Alternativa de menor alcance: resolverlo solo en el front con el `diffMinutos` que la fila ya
  calcula. Más rápido, pero deja la regla duplicada.

**Decisiones abiertas**
1. ¿Gris "Sin datos" (propuesta) o rojo?
2. ¿Se resuelve en la API (fuente única) o solo en el front?

#### Alcance e impacto de REQ-01

- **Agente: sin cambios.** `uptime_seconds` y `state_reason` ya llegan. A confirmar: que los
  agentes viejos (`schema_version 3.0–4.3`, la mayoría de los hospitales) manden
  `uptime_seconds` por VM; si no, esos hospitales no tendrán la alerta hasta actualizar.
- **Base de datos:** sin cambios con la opción 1 de REQ-01a.
- **Despliegue:** solo server y frontend; **no depende de las fases del agente**, se puede
  liberar con el server.
- **Riesgo principal:** volumen de alertas y tickets (ver "Ruido" y "Asana").

#### Criterios de aceptación de REQ-01

1. Una VM `type: "vm"` que reporta `uptime_seconds` por debajo del umbral abre
   `VM_UPTIME_<id>` con la severidad definida, visible en alertas activas; con el uptime por
   encima del umbral se cierra sola.
2. Una VM `ws` o `eq` que se reinicia se comporta según la decisión tomada (sin alerta o NOTICE).
3. Una exclusión por hospital y patrón sobre `VM_UPTIME_` silencia la alerta.
4. Un reporte sin `uptime_seconds` (agente viejo o VM caída) no genera falsos positivos ni
   errores.
5. Con el hospital sin reportar más de `offline_minutes`, ninguna VM se muestra verde ni en el
   listado ni en el detalle; el texto indica hace cuánto es el dato.
6. Con el hospital online y una VM `Offline`, la VM sigue en rojo.
7. Al volver a reportar el hospital, los estados reales reaparecen sin recargar la página.

*Cómo verificar:* reportes sintéticos contra el motor y `TestClient` para la API (mismo
enfoque que las pruebas del token de ingesta, [11 §7.3](11-plan-auth-ingesta-agente.md)),
más una prueba manual de la vista en un hospital real.

#### Hallazgos relacionados (no pedidos, para decidir)

1. **Default distinto de `offline_minutes`.** Si la clave no está guardada en `configuracion`, el
   motor usa **15 min** (`alerts_engine/config.py:60`) y `mirth_mapa` también, pero el panel de
   alertas usa **10** (`routers/alertas_config.py:129`), `resumen_red.py:339` usa 10 y
   `script.js:61` usa 10. El listado puede decir "Offline" a los 10 min y el motor alertar a los
   15. Verificar el valor guardado en producción.
2. **`HOST_UPTIME` con uptime 0.** `host_info.get('uptime_seconds') or tele_host.get(...)`
   (`infra.py:212`) trata un `0` legítimo como "sin dato". Al agente le pasa lo inverso: cuando
   el hipervisor no responde manda `uptime_seconds: 0` por defecto (`agent_logic.py:1265`).
   Conviene que REQ-01a no repita el patrón.
3. **`state_reason` sin consumidor** en el server (ver arriba); útil también para el detalle de
   la VM.

### REQ-02 — Dividir los archivos monolíticos del frontend

**Pedido:** analizar si es viable dividir `script.js`, `index_beta.html` y otros archivos
grandes en piezas más granulares, con el objetivo de mejorar la performance.

**Veredicto corto:** **es viable, pero dividir por sí solo casi no mejora la performance.** Lo que
más pesa hoy está en otro lado (imágenes y caché, ver abajo). Dividir vale la pena por
mantenimiento y por poder cargar bajo demanda las partes que no todos usan. Por eso el plan
(§ Plan por fases) arranca por las ganancias rápidas y deja la división de `script.js` para
después, con una red de seguridad de pruebas.

#### Qué hay hoy (medido)

| Archivo | Tamaño | gzip | Qué es |
|---|---|---|---|
| `static/script.js` | 243 KB | 57 KB | 5158 líneas, **161 funciones globales**, 49 variables globales |
| `templates/index_beta.html` (`/beta`) | 256 KB | 48,5 KB | 70 KB de CSS inline + 60 KB de JS en 6 bloques inline + 124 KB de HTML |
| `static/mapa_integraciones.js` | 28 KB | 8 KB | ya es un módulo aparte (`MapaIntegraciones`); solo usa `authFetch` |
| `static/refresco-vivo.js` | 11 KB | 4 KB | usa 12 funciones y 4 globales de `script.js` |
| `static/style.css` | 31 KB | 8 KB | — |
| `templates/index.html` (`/monitor`) | 86 KB | — | interfaz vieja (ver "Otros archivos") |
| `templates/solucion4.html` / `mendoza_project.html` | 5,3 MB / 2,8 MB | — | mapas generados con datos embebidos (ver "Otros archivos") |

Carga inicial del dashboard: **≈ 130 KB comprimidos** de HTML + CSS + JS propio, sin contar
imágenes ni las librerías de CDN.

#### Dónde está la performance y dónde no

| Hallazgo | Evidencia | Peso en la performance |
|---|---|---|
| **Parsear `script.js` cuesta poco.** | 6,8 ms en frío y ~0,2 ms ya caliente, medido en Chromium headless con `new Function()` (solo parseo; no mide la ejecución inicial). Es una PC de desarrollo, no la de un hospital: en equipos lentos puede ser varias veces más, pero sigue siendo del orden de decenas de ms. | **Bajo.** Partir el archivo no lo mejora. |
| **Tres PNG de 2,4 MB cada uno en el fondo del home** (`home_1/2/3.png`, **7,4 MB** en total), cargados por `background-image` inline (`index_beta.html:1788-1790`). | Tamaño de los archivos. | **Muy alto.** Es más que todo el resto junto. |
| **El GZip se aplica también a los PNG.** | Verificado: `home_1.png` sale con `content-encoding: gzip`. Un PNG ya está comprimido; solo se gasta CPU. | Medio-bajo. |
| **Sin `Cache-Control` en `/static`.** Solo `ETag` + `Last-Modified`; sin URLs versionadas. | Verificado en la app (la revalidación con ETag devuelve 304). Si hay Nginx delante puede cambiar los headers: confirmar en producción. | **Alto** para visitas repetidas, y es requisito para dividir sin riesgo. |
| **CSS y JS inline en `index_beta.html`** (130 KB sin comprimir). El HTML se pide en cada visita y esa parte no se cachea. | Medido: sin CSS/JS inline el HTML pasa de 255,6 KB a 124,4 KB (gzip **48,5 → 19,2 KB**). | Medio. |
| **Chart.js en el `<head>` sin `defer`**, desde CDN externo (`index_beta.html:24`); Leaflet también desde CDN. | Bloquea el parseo mientras baja; depende de que el CDN sea accesible. | Medio. |
| **`sw.js` es un pass-through.** No cachea nada. | Leído. | La PWA hoy no aporta a la carga. |

#### Qué se puede diferir (carga bajo demanda)

Del JS propio (≈ 71,5 KB comprimidos entre `script.js` y los bloques inline) hay ≈ **37,5 KB
(≈ 52 %)** que no hace falta en la primera pantalla:

| Bloque | Tamaño | gzip |
|---|---|---|
| Informes IA (`script.js` 1903–2880) | 42 KB | 11,3 KB |
| KPIs globales y donut (2881–3340) | 20 KB | 5,6 KB |
| Software y logs: SSL, Mirth, Elastic, DICOM (3779–5158) | 69 KB | 13,6 KB |
| Administración: usuarios, clientes, solicitudes (bloques inline #1, #2 y #6) | 30 KB | 7,0 KB |

Es ahorro de descarga: se nota en redes lentas, no en PCs y redes buenas.

#### Riesgos y acoplamientos (evidencia)

- **Todo vive en el scope global.** 161 funciones, 49 variables y **63 `onclick=` dentro de HTML
  generado por strings** (37 funciones distintas invocadas desde ahí). No hay módulos.
- **Dependencias cruzadas por globales.** `refresco-vivo.js` llama a 12 funciones de
  `script.js` (entre ellas `cargarEstadoSoftware` y `cargarHistorialKpiGlobal`, que caen en
  los bloques diferibles): si esos bloques se cargan bajo demanda, hay que evitar que se las
  invoque antes de que existan. Los bloques inline usan 11 funciones y 2 globales.
- **Parches y redefiniciones: "la última definición gana".** Hay 6 funciones definidas más de una
  vez: `logout` (`script.js:3504` **y** el bloque inline #6, que dice explícitamente "aplicar
  overrides DESPUÉS de que script.js cargó"), `todasLasFilas` y `renderPagina` (3 veces cada
  una en el bloque inline #3, con un `patchAplicarFiltros`), `abrirModalIA`,
  `cargarUsuariosResponsables` y `ejecutarExportacionExcel` (2 veces cada una dentro de
  `script.js`). **Mover código cambia cuál versión gana**, así que hay que consolidarlas antes
  de cortar.
- **Orden obligatorio de ejecución.** El bloque inline #4 (Chart.js en modo oscuro) tiene que
  correr *antes* de `script.js`, y el #5 (limpiar `?hospital=` de la URL) *antes* de
  `DOMContentLoaded`.
- **No hay pruebas automatizadas del frontend.** Sin ellas no se detecta una rotura al cortar.
- **Versiones mezcladas.** Con más archivos y caché heurístico, tras un despliegue el navegador
  puede quedarse con un JS viejo contra un HTML nuevo. Se resuelve con URLs versionadas
  (Fase 1) *antes* de partir nada.
- **A favor:** `index_beta.html` no tiene ninguna expresión Jinja ni atributos `on*=` en el
  HTML, así que extraer su CSS y JS es mecánico.

#### Opciones evaluadas

| | Opción | Ganancia | Riesgo | Recomendación |
|---|---|---|---|---|
| A | **División plana:** archivos `<script>` clásicos con el mismo scope global, en el orden correcto. | Mantenimiento, caché por archivo, base para carga bajo demanda. | Bajo si se respeta el orden y se consolidan los duplicados. | **Sí.** |
| B | **Módulos ES** (`import`/`export`): quitar globales y reemplazar los `onclick=` por delegación de eventos. | Estructura limpia. | Alto: 161 funciones, 49 globales y 63 handlers, sin pruebas. | No ahora. |
| C | **Bundler** (Vite/esbuild): minificado, hash en el nombre, división automática. | Minificado (estimación típica de ~30–40 % menos de JS sin comprimir, **no medida** acá) y versionado automático. | Medio: agrega Node al flujo de release, hoy basado en `git pull` en el server. | Diferir; depende de una decisión del equipo. |

#### Plan por fases

**Fase 0 — Línea base y red de seguridad** *(esfuerzo bajo)*
- **Armar un entorno local de pruebas** (hoy no existe; ver "Entorno de pruebas" abajo).
- Medir en ese entorno (Playwright/DevTools): bytes transferidos, cantidad de requests, LCP,
  `DOMContentLoaded` y tiempo hasta que se ve la tabla de hospitales; con caché fría y caliente.
  Los tamaños de los estáticos ya están medidos; lo que falta es el comportamiento en el
  navegador. Como el server local no tiene Nginx, lo de red y caché se verifica contra
  producción con `curl -I` (solo lectura).
- Armar una prueba automática de humo: login por rol → recorrer cada pestaña → sin errores de
  consola. Sin esto no se puede afirmar una mejora ni detectar una rotura.
- *Salida:* números de partida registrados y prueba de humo en verde sobre el código actual.

**Fase 1 — Ganancias rápidas, sin partir código** *(esfuerzo bajo-medio; riesgo bajo)*
1. Comprimir `home_1/2/3.png` (PNG → WebP/JPEG; meta orientativa ≤ 300 KB cada una) y cargar
   lazy los slides 2 y 3; optimizar `logo.png` (84 KB).
2. `Cache-Control` explícito en `/static` con URLs versionadas (hash o `?v=`), y excluir imágenes
   del GZip.
3. Sacar el CSS inline (70 KB) y el JS inline a archivos estáticos, respetando el orden (los
   bloques #4 y #5, de 0,3 y 2,9 KB, quedan inline o van primero).
4. `defer` en Chart.js; evaluar alojar Chart.js y Leaflet en el propio server (versión fija, sin
   depender del CDN).
5. Retirar `/monitor` (decidido; ver "Retiro de `/monitor`").
- *Salida:* la prueba de humo sigue en verde y las métricas de la Fase 0 mejoran.

**Fase 2 — Dividir `script.js`** *(esfuerzo medio; riesgo medio)*
- **2a. División plana, sin cambiar comportamiento.** Primero consolidar las 6 funciones
  duplicadas y dejar documentado cuál versión gana. Después cortar por las costuras que ya
  existen (los comentarios de sección), cargando con `<script defer>` en orden. *Salida:*
  prueba de humo en verde, sin diferencias de comportamiento.
- **2b. Carga bajo demanda por pestaña** (Informes IA, KPIs, Software y logs, Administración)
  con un cargador de módulos y guardas para que `refresco-vivo.js` no llame funciones aún no
  cargadas. *Salida:* JS inicial ≈ 34 KB comprimidos (hoy ≈ 71,5 KB).

**Fase 3 — Opcional, más adelante:** módulos ES, delegación de eventos y bundler. Solo si el
equipo decide sumar una herramienta de build.

**Mapa de módulos propuesto para 2a** (los rangos son aproximados; algunas funciones cruzan
los límites y se ajustan al implementar):

| Archivo propuesto | Origen actual | Contenido | Carga |
|---|---|---|---|
| `core.js` | `script.js` 1–278 | `authFetch`, WebSocket de alertas, sidebar | siempre, primero |
| `lista.js` | 279–726 | navegación, configuración global, tabla de hospitales, filtros | siempre |
| `detalle.js` | 727–1902 | detalle del hospital, historial y gráficos, alertas | al abrir un hospital |
| `informes-ia.js` | 1903–2880 | historial y seguimiento de informes IA, borrador | bajo demanda |
| `kpis.js` | 2881–3340 | KPIs globales, donut | bajo demanda |
| `ui.js` | 3341–3570 | tema, tabs, cambio de contraseña, resumen | siempre |
| `mapa-dashboard.js` | 3571–3778 | mapa del home | con el home |
| `software.js` | 3779–5158 | SSL, Mirth, Elastic, DICOM y sus responsables | bajo demanda |
| `admin-usuarios.js`, `admin-clientes.js`, `admin-solicitudes.js` | bloques inline #1, #2, #6 | ABM de usuarios/clientes y solicitudes de acceso | solo rol Admin |
| `alertas-ui.js` | bloque inline #3 | reorganización de columnas de incidentes | con Alertas |

`refresco-vivo.js`, `revision-borrador-ia.js` y `mapa_integraciones.js` no se tocan.

#### Otros archivos revisados

- **`index.html` (`/monitor`, 86 KB): no dividirlo; se retira (decidido).** La ruta sigue activa
  (`routers/auth.py:43`), pero el login ya no la ofrece: el selector "versión clásica / beta"
  existe en `login.html` y **nada lo abre** (`abrirModalVersion` no se invoca en ningún lado).
  Solo se llega escribiendo la URL.
- **`solucion4.html` (5,3 MB) y `mendoza_project.html` (2,8 MB):** no son código escrito a mano
  sino mapas con coordenadas de rutas embebidas (líneas de hasta 931 KB); `mendoza_project`
  *(inferido)* lo genera `Accesorios/mapa_mendoza/generar_mapa_mendoza.py`. Dividir el HTML no ayuda: la mejora
  sería sacar los datos a `.json` cacheable y/o simplificar las geometrías. Son páginas públicas
  de eventos, fuera del dashboard: prioridad baja.
- **`demo-pacs.html` (72 KB), `cliente.html` (45 KB), `solucion1/2/3.html` (59–101 KB):** páginas
  autocontenidas, sin problema; no se recomienda dividirlas.
- **`generator_report.py` (54 KB):** es el único `.py` grande. Es un tema de mantenimiento, no de
  performance (arrastra ~380 líneas de código muerto, ver [08](08-plan-refactor-dashboard.md)).

#### Alcance e impacto de REQ-02

- **Solo server y frontend. Agente: sin cambios.** No depende de las fases del agente.
- **Riesgo para el despliegue:** la Fase 1 es de bajo riesgo; la Fase 2 toca todo el dashboard.
  Recomendación (a confirmar en §3): no mezclar la Fase 2 con el primer despliegue a hospitales,
  para poder atribuir cualquier regresión a un solo cambio.

#### Métricas objetivo (tentativas, a ajustar con la línea base de la Fase 0)

- Primera carga del home: de ≈ 7,5 MB a menos de ≈ 1,2 MB.
- HTML de `/beta`: de 48,5 KB a ≈ 19 KB comprimidos.
- JS inicial propio: de ≈ 71,5 KB a ≈ 34 KB comprimidos (tras 2b).
- Segunda visita: sin descarga de `/static` (caché con versión).
- Prueba de humo en verde en todas las pestañas para cada rol (Admin, Ingeniería, Comercial,
  Visor) y sin errores de consola.

#### Decisiones (2026-09-19)

| # | Tema | Respuesta | Consecuencia |
|---|---|---|---|
| 1 | ¿Dónde se siente lenta hoy? | En ningún lado; se busca mejorar de todos modos. | Es una mejora incremental, no un problema a resolver: **prioridad propuesta baja**, después del despliegue masivo. La Fase 1 sigue siendo la de mejor relación esfuerzo/ganancia. |
| 2 | ¿Nginx sirve `/static`? | No se sabe. | Ver "Cómo averiguar lo de Nginx". |
| 3 | ¿Se acepta un build con Node? | No se sabe. | **Por defecto, no.** Las Fases 0–2 no lo necesitan; la Opción C queda diferida hasta que se decida. |
| 4 | ¿Se retira `/monitor` (`index.html`)? | **Sí.** | Ver "Retiro de `/monitor`". |
| 5 | ¿La Fase 1 entra en el release de los hospitales? | No se sabe. | Propuesta: **después** del despliegue masivo. Sin lentitud percibida, y con un cambio por vez, no hay razón para sumarla a un release ya cargado. A confirmar en §3. |
| 6 | ¿Hay entorno de pruebas? | **No.** | Ver "Entorno de pruebas". |

#### Cómo averiguar lo de Nginx (solo lectura)

Hay un Nginx delante de la app (lo dicen [01](01-arquitectura.md) y [06](06-operaciones-y-scripts.md),
y `server.py:236` confía en él), pero **su configuración no está en el repo**. No se sabe si sirve
`/static` por su cuenta ni si comprime o cachea. Dos comandos lo aclaran:

```bash
# desde cualquier equipo: headers reales que recibe el navegador
curl -sI -H 'Accept-Encoding: gzip' https://tecnomonitor.tecnoimagen.com.ar/static/script.js

# en el servidor: configuración efectiva
sudo nginx -T 2>/dev/null | grep -n -E "location|alias|root|proxy_pass|gzip|expires|add_header|client_max_body_size"
```

Qué mirar: `Cache-Control`, `Content-Encoding`, `ETag` y si la respuesta trae headers de Nginx
que la app no genera. El mismo `nginx -T` responde también lo de `client_max_body_size`, que
necesita el ítem S5 del plan de acción ([07](07-plan-de-accion.md), 2.2). Conviene además
**versionar esa configuración en el repo** (hoy solo existe en el servidor).

#### Retiro de `/monitor`

**Estado (2026-09-21): pasos 1 y 2 hechos, `index.html` borrado y selector "clásica / beta" del login
quitado.** `manifest.json` arranca en `/beta`, `/monitor` responde 302 a `/beta`. Queda el paso 3
(eliminar la redirección cuando los logs de Nginx ya no la muestren). Los cambios locales sin
commitear que tenía `index.html` se descartaron: no había nada de producción en ellos. `script.js`
puede conservar código que solo usaba `index.html`; queda para la Fase 2 de REQ-02.

**Riesgo a resolver antes de quitar la ruta:** `static/manifest.json` tiene
`"start_url": "/monitor"` y lo cargan tanto `index.html` como `index_beta.html`. Quien haya
instalado la PWA (el manifest fija `orientation: portrait`, o sea que apunta a celulares) abre
`/monitor` al tocar el ícono. Borrar la ruta sin más rompe esos accesos.

**Propuesta, en tres pasos:**
1. `manifest.json`: `start_url` pasa a `/beta`.
2. `/monitor` **no se borra de golpe:** pasa a redirigir a `/beta` (302), para que sigan
   funcionando los favoritos y las instalaciones que todavía tengan el manifest viejo en caché.
3. Más adelante, cuando los logs de acceso de Nginx ya no muestren `/monitor`, se elimina la
   ruta del todo.

**Qué se toca cuando se autorice código:**
- `routers/auth.py:43-45` (la ruta) y `static/manifest.json`.
- Borrar `templates/index.html`. Sus cambios locales sin commitear (el tab "Integraciones") se
  descartan; no se pierde nada que esté en producción.
- `login.html`: el selector "versión clásica / beta" está muerto (líneas ≈181–190, 299–309 y
  450–452) y se quita.

**Documentación a actualizar:** `01-arquitectura.md` (líneas 67 y 131–136, que hoy explican por
qué hay que espejar cambios en `index.html`), `03-api-referencia.md:36`,
`12-ultima-milla-alertas-asana.md` (≈321–323), `13-contrato-topologia-mirth.md:59` y
`15-plan-redes-dicom.md:62`. `08-plan-refactor-dashboard.md` es historial y no se cambia.

**Beneficio:** menos 86 KB de HTML sin mantener y se elimina el "cambio espejado en
`index.html`, best-effort" que documenta `01-arquitectura.md`.

#### Entorno de pruebas

No hay entorno de pruebas, y afecta a todo el plan (REQ-01 necesita simular un hospital offline
y una VM que se reinicia; REQ-02 necesita una prueba de humo del frontend). **Propuesta:** un
entorno **local** con SQLite y datos sintéticos (hospitales, reportes con VMs y
`uptime_seconds`, un usuario por rol), levantando `server.py` con un `.env` de prueba.

- **Viabilidad:** se verificó que la app importa y registra las 97 rutas en local (con un
  `JWT_SECRET` de prueba). **Falta verificar** que arranca completa con datos sintéticos.
- **Requiere** el `.env.example` (ítem 2.6 de [07](07-plan-de-accion.md)).
- ⚠️ **Nunca copiar el `.env` de producción.** El motor de alertas crea tickets en Asana; con el
  token real, un entorno de pruebas abriría tickets reales. Hay que verificar cómo se comporta
  el conector sin `ASANA_ACCESS_TOKEN`.
- **Alternativa mínima** (peor): prueba de humo solo de lectura contra producción, con un
  usuario dedicado de rol Visor y en horario controlado.

### REQ-03 — Reflejar en el server lo que se deja de monitorear en el agente

**Pedido:** si en un hospital se monitoreaba un Mirth (o una VM, etc.) y se lo desactiva en el
agente, el server tiene que reflejarlo: dejar de mostrarlo en la interfaz.

**Casos que cubre** (no son lo mismo y se resuelven distinto)

| | Caso | Ejemplo |
|---|---|---|
| S1 | **Módulo apagado** en el agente | `enabled_mirth`, `enabled_vms`, SSL, KPIs por SQL/Elastic, autoenrute DICOM, logs |
| S2 | **Elemento puntual quitado** de la configuración | una VM de `vms[]`, un servidor de `mirth_servers`, una URL de `ssl_urls` |
| S3 | **Elemento que desaparece del sistema monitoreado**, sin tocar el agente | un canal de Mirth borrado o renombrado, una regla de autoenrute eliminada |
| S4 | **Hospital o perfil completo dado de baja** | agente apagado o perfil eliminado; o `is_visible` / `alerts_enabled` desde el server |

#### Situación actual (evidencia)

**Lo que manda el agente.** En cada reporte declara el estado de cada módulo en
`collection_meta.<módulo>` con `enabled` y `status` (`disabled` · `ok` · `partial` · `error`), más
`total` y `errors` (`agent_logic.py:2475-2486`; [contrato §8](../../tecnomonitor-agent/docs/CONTRATO_AGENTE.md)).
Con un módulo apagado **no manda su clave** (ej. no viaja `software_monitoring.mirth`), y con
las VMs apagadas manda `virtual_layer: []`. Cada VM configurada aparece siempre, aunque no
responda (con `state: "Offline"`), así que "presente" equivale a "configurada". Por lo tanto,
**"desactivado" y "con error" ya son distinguibles del lado agente.**

**Lo que hace el server.**
1. **Ignora `collection_meta`.** Solo el frontend la lee, para decidir si hay host físico
   (`script.js:730-737`). El ingreso solo agrega filas a `software_monitoring`
   (`main.py:299-432`) y no registra nunca que algo se apagó.
2. **La pestaña Software muestra lo que dejó de reportarse para siempre.**
   `/api/hospital/{id}/software` (`hospital_detalle.py:286-334`), con `minutos=0`, toma la última
   fila de cada componente que se haya guardado alguna vez, **sin límite de antigüedad**, y también
   cae a ese "último snapshot" cuando la ventana no tiene datos. Un canal de Mirth, un certificado
   SSL, una regla de logs o una cola DICOM que dejaron de reportarse siguen mostrados con su
   último estado (verde, si era verde). Es el mismo problema de fondo que REQ-01b.
3. **Las VMs quitadas sí desaparecen de la vista**, porque sale del último reporte
   (`virtual_layer`). Pero **sus alertas no se cierran** (ver más abajo).
4. **El detector de Mirth re-evalúa canales fantasma.** Lee las últimas 2 filas de cada canal
   **sin filtro de tiempo** (`alerts_engine/software/mirth.py:62-74`). Si el último dato era
   `STOPPED`/`ERROR` sostenido o una cola alta, emite CRITICAL en cada tick para siempre.
   *(Inferido, no reproducido)* si se cierra a mano, el siguiente tick la reabre como
   "reincidencia" (`estado.py`, caso B3).
5. **El detector de DICOM hace lo contrario.** Solo mira una ventana (`win_crit × 1,5`, 180 min por
   defecto), y una regla que deja de llegar se saltea **sin emitir OK a propósito**
   (`dicom_autoenrute.py:230-246`, `288-293`: "ni alerta, ni OK, que cerraría un incidente real
   por falta de datos"). Sus alertas abiertas quedan abiertas indefinidamente.
6. **El detector de infra no cierra nada de una VM que ya no está**: `VM_CPU_`, `VM_RAM_` y
   `DISK_<id>_<mount>` abiertas quedan abiertas, porque ya no se emite ningún hallazgo para ellas
   (`infra.py:271-293`).
7. **Los KPIs de inactividad confunden "sin actividad" con "módulo apagado".** Suman `admitidos`
   de la ventana (24 h por defecto) y si da 0 abren alerta (`kpis_negocio/_runner.py:56-80`).
   *(Inferido)* apagar los KPIs por SQL/Elastic en un hospital con `has_ris` genera, a las ~24 h,
   una falsa alerta de inactividad.
8. **El mapa de integraciones ya resuelve esto, y la pestaña Software no:** usa
   `mirth_stale_minutes` (15 min) medido contra el último timestamp *del hospital* y tiene
   `oculto` por canal y `activo` por nodo (`mirth_mapa.py:133-207`). Dos vistas del mismo Mirth
   se contradicen. *(Parcialmente resuelto el 2026-09-21, solo lectura: `GET /api/hospital/{id}/software`
   ahora devuelve `stale` y `sin_datos_min` por canal de Mirth, con el mismo umbral que el mapa
   (`mirth_stale_minutes`, medido contra la última lectura de Mirth del hospital), y la pestaña
   Software muestra esos canales en gris como "Sin datos hace N min". Sigue pendiente lo demás de
   REQ-03: el detector de alertas, el cierre de tickets y SSL/Elastic/DICOM, que tienen otra cadencia.)*
9. **Una alerta abierta solo se cierra** por un hallazgo OK, por una regla de exclusión o a mano.
   La limpieza de "huérfanas" solo sincroniza con Asana (tickets completados o borrados), y los
   15 días de `DIAS_CADUCIDAD` solo deciden si un incidente se reabre como reincidencia
   (`estado.py`), no cierran nada.
10. **Dar de baja un hospital solo cambia un flag.** `is_visible` y `alerts_enabled` no cierran
    sus alertas abiertas (`routers/hospitales_metadata.py:95-108`), y `/api/alertas` lista todas
    las activas sin filtrar por ellos (`routers/alertas_config.py:105`).
11. **Lo único que existe hoy es manual:** las reglas de exclusión (Admin/Ingeniería) cierran las
    alertas activas que coinciden y su ticket de Asana. Pero no ocultan nada de la interfaz, y si
    se borra la regla las alertas vuelven en el próximo tick.

#### Comportamiento esperado (propuesta)

**Principio:** cada módulo o elemento está en uno de tres estados, y solo el primero se oculta y se
silencia.

| Estado | Significado | Interfaz | Alertas |
|---|---|---|---|
| **Desactivado** | se dejó de monitorear a propósito | no se muestra | se omite y se cierran las abiertas |
| **Con error / sin datos** | se espera monitorearlo pero falla o no llega | se muestra en error o gris (REQ-01b) | se mantienen |
| **Activo** | reporta normalmente | se muestra | se evalúa |

**Cómo se decide que algo está "Desactivado"**
1. **Módulo:** `collection_meta.<módulo>.enabled == false` en el último reporte del hospital
   (declarativo; el agente ya lo manda).
2. **Elemento** *(etapa 2; fuera del alcance inicial)*: ausente del último reporte **completo** de su módulo (`status` ok o partial, sin
   error total) **mientras el hospital sigue reportando**. La ausencia se mide contra el último
   reporte *del hospital*, no contra el reloj (mismo criterio que el mapa). Así, un hospital
   offline **no** hace desaparecer sus componentes: eso es REQ-01b.
3. **Período de gracia** configurable (**6 horas**, decidido) para no reaccionar a un reinicio de
   Mirth, un mantenimiento o un ciclo perdido.
4. **Manual:** una acción de admin "dejar de monitorear" para lo que la regla no resuelva
   (agentes viejos sin `collection_meta`, elementos que siguen reportando pero ya no interesan).
   Reutiliza `oculto` / `activo` del mapa.

**Qué pasa al pasar a Desactivado**
- La interfaz no lo muestra (con una lista opcional de "dados de baja" para auditoría).
- Los detectores lo omiten.
- Sus alertas abiertas se cierran con motivo "Monitoreo desactivado" y su ticket de Asana se
  cierra con un comentario (mismo mecanismo que la exclusión).
- **El histórico no se borra:** gráficos, informes y KPIs siguen consultables.
- Si el módulo de KPIs está apagado, el detector de inactividad se omite.
- Si se **reactiva**, reaparece solo y sus alertas arrancan de cero.
- Al dar de baja un hospital en el server, se cierran sus alertas abiertas y `/api/alertas` deja de
  listarlas.

#### Consideraciones de diseño

- **Dónde vive el estado.** *Derivado:* se calcula en cada consulta desde el último reporte y el
  `last_seen`. Simple, sin migración, pero recalcula siempre y no guarda "desde cuándo".
  *Persistido:* una tabla de componentes monitoreados (hospital, tipo, id, `first_seen`,
  `last_seen`, estado, desactivado en, motivo). Permite mostrar "desactivado desde X", cerrar
  alertas una sola vez y auditar; cuesta una migración y mantener la consistencia. Ya existe un
  precedente parcial: `mirth_channel_topology.last_seen`. **Decidido:** persistido (tabla nueva).
- **Agentes sin `collection_meta`.** No se verificó desde qué versión existe. Para esos hospitales
  aplican solo la regla por ausencia y la baja manual.
- **Falso "desactivado".** Un reporte con `status: error` y datos vacíos **no** es una baja: se
  exige `enabled == false` declarado, o un reporte completo con `status: ok`, más la gracia.
- **Cierre masivo de tickets.** Al activar la regla por primera vez podría cerrar de golpe muchos
  tickets "fantasma" viejos. Conviene una corrida en modo simulación que liste lo que cerraría,
  igual que `/api/exclusiones/preview`.
- **Agente: sin cambios obligatorios, salvo para los KPIs** (ver "`collection_meta.sql` no es
  confiable"). Mejora opcional: que `collection_meta.<módulo>` incluya la
  lista de elementos **configurados**, para distinguir "configurado pero sin respuesta" de "quitado
  de la configuración" sin inferirlo por ausencia. Tocaría el agente (rollout por hospital), así que
  solo si la inferencia por ausencia resulta insuficiente.
- **Perfil eliminado en un agente multi-hospital (S4):** el agente no puede avisar que se borró; el
  hospital queda offline para siempre y hay que darlo de baja a mano en el server.

#### Decisiones tomadas (2026-09-19)

| # | Tema | Decisión |
|---|---|---|
| 1 | Alcance inicial | **Módulos completos.** Los elementos sueltos (un canal, una VM, una URL de SSL: casos S2 y S3) quedan para una segunda etapa. |
| 2 | Tickets de Asana | **Se cierran automáticamente todos** los tickets de las alertas abiertas del módulo dado de baja. *(Interpretación: cada alerta abierta del módulo y su ticket; confirmar si además se quiere barrer tickets viejos que ya estén cerrados en local.)* |
| 3 | Dónde vive el estado | **Tabla nueva** (persistido). |
| 4 | Interfaz | **Lista de "dados de baja":** no aparecen en las vistas normales, pero queda una lista consultable. |
| 5 | Período de gracia | **6 horas**, configurable. |

**Todavía abiertas**
6. ¿Reactivar un módulo cuenta como reincidencia dentro de los 15 días, o arranca de cero?
   Propuesta: arranca de cero.
7. ¿Dar de baja un hospital en el server (`is_visible` / `alerts_enabled`) cierra sus alertas
   abiertas? Propuesta: sí.
8. **KPIs:** ¿se libera un ajuste mínimo del agente, o la baja de KPIs es manual en la etapa 1?
   Propuesta: manual ahora (ya existe `has_ris`); el ajuste del agente, con la próxima versión que
   se libere. Ver el hallazgo de abajo.
9. ¿Se hace una **vista previa** de lo que se cerraría antes de activar la regla por primera vez?
   Recomendado: sí, porque el primer barrido puede cerrar muchos tickets de una vez.

#### Alcance de la etapa 1: módulos y qué afecta cada uno

Un módulo dado de baja se define por su clave en `collection_meta`. Los nombres de alerta son los
que emiten hoy los detectores; se confirman al implementar.

| Módulo (`collection_meta`) | Qué deja de mostrarse | Alertas que se cierran | ¿Señal confiable? |
|---|---|---|---|
| `mirth` | canales en la pestaña Software y en el mapa de integraciones | `MIRTH_*` | sí |
| `wmi` (VMs, estaciones, equipos) | tarjetas de `virtual_layer` | `VM_CPU_*`, `VM_RAM_*`, `DISK_*` (y `VM_UPTIME_*` / `VM_OFFLINE_*` si se implementa REQ-01a) | sí |
| `proxmox` / `idrac` | tarjeta del host físico (ya se oculta si ambos están apagados, `script.js:735`) | `HOST_*` (hipervisor); `TEMP_*`, `FAN_*`, `PSU_*`, `RAID_*` (iDRAC) | sí |
| `dicom_routing` | colas de autoenrute | `DICOM_ROUTE_*`; los pisos de `dicom_regla_baseline` se conservan | sí (cubre SQL y Elastic) |
| `ssl_monitoring` | certificados SSL | ninguna: hoy no hay detector | sí |
| `suitestensa_logs` | reglas de logs | ninguna: hoy no hay detector | sí |
| `sql` (KPIs) | KPIs de uso | alertas de inactividad de RIS y Mamografía | **no** (ver abajo) |

`NETWORK_LATENCY` no entra en ningún módulo: el agente siempre recolecta la red.

#### Hallazgo: `collection_meta.sql` no es confiable para los KPIs

- `collection_meta.sql.enabled` sale de `enabled_sql` (`agent_logic.py:2479`), pero los KPIs también
  pueden llegar por **Elastic** (`elastic.enabled_ris_metrics`, `headless_service.py:326-339`), que
  rellena el mismo `_sql_data_payload` con `enabled_sql` apagado. Un hospital con KPIs por Elastic
  reporta `sql.enabled: false` **y** `status: "ok"` en los ciclos que traen un bloque.
- Además, `status` arranca en `"disabled"` y solo pasa a `"ok"` cuando ese ciclo trae un bloque de
  KPIs (`agent_logic.py:2566-2572`). El bloque no sale en todos los ciclos (depende de
  `executions_per_day`), así que un módulo activo aparece con `enabled: true` y `status: "disabled"`
  en los demás.
- **Consecuencia:** si el server usara `collection_meta.sql`, daría bajas falsas. El resto de los
  módulos declara `enabled` a partir de su propio flag de configuración.
- **Opciones:** (A) un ajuste mínimo en el agente: `enabled` = Elastic-RIS **o** SQL, con `status`
  correcto. Requiere liberar una versión nueva, y en los hospitales que no actualicen queda la baja
  manual. (B) etapa 1 sin señal declarativa para KPIs: baja **manual**, apoyada en `has_ris` de
  `hospitales_metadata`, que el detector de inactividad ya respeta (`_runner.py:49`).
  **Propuesta: B ahora y A oportunistamente** (decisión 8).

#### Modelo de datos (borrador): tabla nueva `monitoreo_modulos`

| Columna | Tipo | Descripción |
|---|---|---|
| `hospital_id` | texto, índice | |
| `modulo` | texto | `proxmox`, `idrac`, `wmi`, `sql`, `mirth`, `ssl_monitoring`, `suitestensa_logs`, `dicom_routing` |
| `estado` | texto | `pendiente_baja` · `desactivado` (**sin fila = activo**) |
| `declarado_off_desde` | fecha | timestamp del primer reporte con `enabled == false` de la racha actual |
| `desactivado_desde` | fecha | cuándo se cumplió la gracia (o se dio de baja a mano) |
| `ultima_vez_activo` | fecha | último reporte con `enabled == true` |
| `origen` | texto | `agente` · `manual` |
| `motivo`, `actualizado_en`, `actualizado_por` | | auditoría |

Única por (`hospital_id`, `modulo`). La etapa 2 le suma un `elemento_id` opcional.
`Base.metadata.create_all()` la crea sola al arrancar (igual que las tablas nuevas de Mirth), sin
script de migración. Como "sin fila = activo", **no hace falta rellenar datos** para los hospitales
existentes. El histórico (`software_monitoring`, `reportes_uso`, etc.) no se toca.

#### Estados y período de gracia (6 h)

Para cada reporte que traiga `collection_meta`, y para cada módulo confiable:
- `enabled == true` → activo. Si estaba `pendiente_baja`, se cancela. Si estaba `desactivado` con
  origen `agente`, se reactiva.
- `enabled == false` → si no hay una racha en curso, pasa a `pendiente_baja` con
  `declarado_off_desde` = timestamp de ese reporte. Si la racha ya lleva **6 h o más**, pasa a
  `desactivado` y se ejecutan las acciones de baja.
- **La gracia se mide con los timestamps de los reportes del hospital, no con el reloj del server.**
  Así un hospital que deja de reportar no "cumple" la gracia por estar offline (ver REQ-01b).
- Durante `pendiente_baja` no cambia nada en la interfaz ni en las alertas.
- **Acciones de baja** (una sola vez, al cambiar de estado): cerrar las alertas abiertas del módulo con
  el motivo "Monitoreo desactivado", cerrar sus tickets de Asana con un comentario (mismo mecanismo
  que la exclusión), y dejar de mostrar y de evaluar el módulo.
- **Baja manual** (Admin/Ingeniería): pasa directo a `desactivado` con origen `manual`, sin gracia, y
  se puede revertir. Cubre los KPIs y los agentes que no mandan `collection_meta`.
- Parámetro nuevo `monitoreo_gracia_horas` (por defecto 6) en el panel de configuración de alertas.

#### Interfaz: lista de "dados de baja"

- En el detalle del hospital, una sección **"Monitoreo desactivado"** con: módulo, desde cuándo,
  origen (agente o manual), motivo y cuántas alertas se cerraron. Si no hay ninguno, no se muestra.
- Acciones para Admin/Ingeniería: **"Dar de baja"** (manual) y **"Revertir"** (solo las manuales; las
  del agente se revierten reactivando el módulo en el agente).
- En las vistas normales (Software, VMs, mapa de integraciones) el módulo no aparece.

#### Criterios de aceptación

1. Con `collection_meta.mirth.enabled == false`, pasado el período de gracia: la pestaña Software
   no muestra canales de Mirth, el detector no los evalúa y las alertas `MIRTH_*` abiertas quedan
   cerradas con motivo.
2. Un canal borrado en Mirth, con el módulo activo y `status: ok`, recibe el mismo tratamiento; los
   demás canales siguen igual.
3. Con Mirth en `status: error` (no responde), **nada se da de baja**: se muestra el error y las
   alertas se mantienen.
4. Con el hospital offline, ningún componente se da de baja por ausencia.
5. Una VM quitada de `vms[]` con el módulo WMI activo cierra sus `VM_*`/`DISK_*`; una VM apagada
   (`state: Offline`) **no** se da de baja.
6. Con los KPIs dados de baja (manualmente, o por el agente ya corregido) no se abre la alerta de
   inactividad a las 24 h, y si estaba abierta se cierra.
7. Al reactivar el módulo, los componentes reaparecen sin intervención.
8. El histórico de un canal dado de baja sigue consultable en los gráficos por rango.
9. Un agente sin `collection_meta` no rompe nada.
10. Antes de activar la regla existe una vista previa de lo que se cerraría.

*Cómo verificar:* reportes sintéticos contra el motor y la API (mismo enfoque que REQ-01), con un
caso por cada fila de arriba, más una prueba manual con un hospital real que apague Mirth.

#### Alcance e impacto

- **Server:** ingreso (registrar el estado), detectores (omitir y cerrar), `/api/hospital/{id}/software`
  y `/api/alertas`, frontend (pestaña Software, coherente con el mapa), base de datos (probable tabla
  nueva y script de migración) y un parámetro de configuración para la gracia.
- **Agente:** sin cambios obligatorios, salvo un ajuste mínimo para el módulo de KPIs (ver
  arriba). Mientras no se libere, la baja de KPIs es manual.
- **Despliegue:** solo server; **no depende del rollout de agentes**, aunque su efecto crece a medida
  que más hospitales tengan un agente que declare `collection_meta`.
- **Riesgo principal:** cierre masivo de tickets o un falso "desactivado". Se mitiga con la vista
  previa, la gracia y exigir `status` correcto.
- **Relación con otros requerimientos:** REQ-01a (si se agregan `VM_UPTIME_`/`VM_OFFLINE_`, esos
  tipos también se cierran al dar de baja la VM) y REQ-01b (distinguir "offline" de "desactivado"
  en la interfaz).

#### Hallazgos relacionados (no pedidos, para decidir)

1. **Los detectores tratan distinto el dato viejo:** Mirth lo re-evalúa sin límite, DICOM lo ignora y
   el de infra por hospital lo lee sin mirar su antigüedad. Conviene un criterio único.
2. **`/api/alertas` lista alertas de hospitales ocultos o con alertas desactivadas.**
3. *(No medido)* La consulta "última fila por componente" recorre todo el histórico del hospital;
   agregar un límite de antigüedad, además de corregir lo funcional, achica lo que se escanea
   (relacionado con P3/P5 y con la base de 18,7 GB).

### REQ-04 — Mapa de integraciones Mirth: flujo acumulado además de la barra temporal

**Pedido (2026-09-21):** la barra temporal del mapa (pestaña "Integraciones", `index_beta.html`)
funciona bien y se mantiene. Se quiere sumar, **como opción secundaria**, ver lo **acumulado**: por
ejemplo un botón "Últimos 30 min" que haga que el gráfico muestre el flujo total que pasó por cada
canal en esos 30 minutos, en vez del tráfico del tramo puntual de 5 minutos.

#### Cómo funciona hoy

- `GET /api/hospital/{id}/mirth/mapa` (`routers/mirth_mapa.py`) ya devuelve, por canal y por tramo
  (`paso`, 5 min por defecto, ventana de 180 min), los campos `trafico` (= `rx` + `tx`), `rx`, `tx`,
  `err`, `cola`, `estado` y `fresco`. Son **deltas** entre lecturas consecutivas de los contadores
  acumulados de Mirth (`_bucketizar`), no totales.
- El frontend (`static/mapa_integraciones.js`) solo muestra el tramo elegido con la barra
  (`TL[paso]`): etiqueta del canal ("8 msg"), grosor del enlace (`grosor()`), columna de la vista
  Lista y la métrica "Tráfico / 5 min" del panel de detalle.

#### Propuesta

Lo acumulado es **la suma de los deltas de los tramos que caen dentro de la ventana**, así que no
hace falta un endpoint nuevo: el default de 180 min ya cubre 30 min (y hasta 3 h). El servidor
tiene un tope de 288 tramos (24 h a paso 5).

- **Selector secundario** junto a la barra temporal, con dos modos: "Instantáneo" (hoy, por defecto)
  y "Acumulado" con la ventana elegida ("Últimos 30 min" primero; 1 h / 3 h después si se
  quiere).
- En modo acumulado cambian las cifras de flujo: etiqueta del enlace ("142 msg · 30 min"),
  columna de la Lista, métrica del panel de detalle y la leyenda. Estado, cola y criticidad siguen
  saliendo de la lectura del momento, no se acumulan.
- **La escala del grosor hay que recalibrarla:** `grosor()` satura en unos 56 msg por tramo
  (`1,1 + tr/13`, tope 5,4). Con 30 min los totales serían ~6 veces mayores y todos los enlaces
  quedarían del mismo grosor. Opción: escala relativa al máximo de la vista o logarítmica.
- "Sin tráfico" pasa a significar "0 mensajes en toda la ventana", que es una señal más útil que
  el tramo suelto en cero.

#### Cuidados

1. **Datos incompletos:** si el hospital dejó de reportar hace un rato, la suma queda por debajo de
   la realidad sin avisar. Se marca con un asterisco y la nota bajo la barra (ver Implementación).
2. **Reinicio de contadores:** si Mirth reinicia o se resetean sus estadísticas, `_bucketizar`
   descarta ese delta (cuenta 0). En una ventana de 30 min ese tramo se pierde; conviene marcarlo
   o documentarlo.
3. **Intervalo de reporte:** si el hospital reporta cada 10 min, los tramos sin fila tienen 0 pero el
   delta se conserva en el siguiente, así que la suma sigue siendo correcta; solo la vista por tramo
   es irregular.
4. **`trafico` cuenta `rx + tx`:** un mensaje que entra y sale de un mismo canal suma dos veces.
   Para el acumulado hay que decidir qué se muestra (ver decisiones abiertas).

#### Implementación (2026-09-21)

Hecho en `static/mapa_integraciones.js`, `static/mapa_integraciones.css` y el bloque de la barra
temporal de `index_beta.html` (botón `#mi-acum` "Últimos 30 min" y nota `#mi-acum-nota`).
`index.html` no se tocó. Se tomaron los valores por defecto propuestos abajo (ventana de 30 min
que termina en la barra; el mapa muestra `rx + tx`; recibidos y enviados por separado en el
detalle; errores solo en el detalle). Detalles que difieren o precisan lo propuesto:

- **Grosor de los enlaces:** en lugar de una escala relativa o logarítmica, usa la **tasa media por
  tramo** (total ÷ tramos de la ventana) con la misma fórmula de siempre. El mismo grosor significa
  el mismo caudal en ambas vistas y no satura.
- **Datos parciales:** el asterisco (`60*`) aparece solo cuando a un canal le faltan lecturas **justo
  antes del tramo actual** (más tramos consecutivos sin lectura que la cadencia de reporte del
  hospital, estimada como la mediana de los huecos entre lecturas). Es lo único que puede dejar el
  total corto: un hueco a mitad de la ventana no pierde tráfico, porque el delta de la lectura
  siguiente lo recoge. *(Primera versión, corregida el mismo día: marcaba cualquier lectura faltante
  en la ventana y, con lecturas algo corridas respecto de los tramos de 5 min, salían marcados casi
  todos los canales en producción.)* Datos de producción (2026-09-21, filas por canal en 24 h):
  P03 257 (~5,6 min entre lecturas), PMMN 246 (~5,9 min), OSECAC_GMS 222 (~6,5 min): el agente reporta
  algo más lento que el tramo de 5 min, así que ~1 de cada 9 tramos queda sin lectura. Nota: los
  `component_id` de Mirth llevan el nombre de la instancia (`[MIRTH_SE] OUT`, no `OUT`).
- **Ventana acotada:** cerca del inicio del historial la barra suma solo los tramos disponibles y la
  nota lo dice ("Solo hay 15 min de historial hasta este punto").
- **Reinicio de contadores** (cuidado 2): no se detecta en el frontend, porque el server ya lo
  entrega como tráfico 0. Sigue sin marcarse.
- **Fuera de esta entrega:** otras ventanas (1 h / 3 h) y errores sobre el enlace.

Verificado con Chromium (Playwright) sobre el HTML, CSS y JS reales, con una API simulada de tres
canales (estable, con huecos aislados de lecturas, con un hueco al final y sin tráfico): totales,
lista, detalle, ventana acotada y vuelta a instantáneo dieron lo esperado, sin errores de JS. La
primera prueba en producción mostró el problema del asterisco descrito arriba; el criterio corregido
**no se volvió a probar con datos reales.**

#### Decisiones abiertas (propuestas ya aplicadas por defecto)

- ¿Qué se suma: `rx` + `tx` como hoy, solo recibidos, solo enviados, o los tres (rx / tx / errores)
  en el panel de detalle? *(Propuesta: mantener `trafico` en el mapa y abrir rx/tx/err en el detalle.)*
- ¿La ventana termina en "ahora" o en la posición de la barra? *(Propuesta: en la posición de la
  barra, así que en vivo son los últimos 30 min reales y al retroceder se ve cualquier ventana.)*
- ¿Qué ventanas se ofrecen además de 30 min? Más de 3 h obliga a pedir `minutos` mayor.
- ¿Se muestran también los errores acumulados (`err`) sobre el enlace o solo en el detalle?

#### Alcance e impacto

- **Server:** sin cambios en la opción base (la API ya entrega los deltas). Solo si se piden ventanas
  de más de 3 h habría que subir `minutos` desde el frontend (tope 288 tramos, 20 000 filas por
  consulta en `mirth_mapa.py`).
- **Frontend:** `static/mapa_integraciones.js`, `mapa_integraciones.css` y el bloque de la barra
  temporal en `index_beta.html`. `cliente.html` queda fuera, igual que el resto del mapa.
- **Agente / base de datos:** sin cambios. Independiente del rollout de agentes.
- **Riesgo:** bajo; es una vista de solo lectura. El riesgo real es mostrar un total engañoso con
  datos parciales (cuidado 1).

*Cómo verificar:* con una serie sintética de lecturas (incluidos huecos y un reinicio de contadores)
comprobar que la suma de los últimos 6 tramos coincide con el total esperado y que el indicador de
datos parciales aparece cuando corresponde.

### REQ-05 — Chequeo de integridad de bases SQL Server tras un reinicio

**Pedido (2026-09-21):** cuando el SQL Server de Extensa se reinicia (típicamente por un corte de
energía abrupto), ejecutar un `DBCC CHECKDB` sobre las bases y reportar si alguna está corrupta o con
problemas. Es una consulta costosa: solo debe correr ante un reinicio.

**El plan vive en el repo del agente:**
[PLAN_CHECKDB_POST_REINICIO.md](../../tecnomonitor-agent/docs/PLAN_CHECKDB_POST_REINICIO.md), con las
decisiones, el diseño, el contrato y las fases. Resumen de lo que toca al servidor:

- **Estado:** el agente 4.5.2 ya lo implementa (Elastic y SQL directo, GUI, tests; falta compilarlo en Windows y
  validarlo en P03, ver la guía del plan del agente §7).
- **Solo ingesta por ahora** (F1, hecha): `software_monitoring.sql_integrity` se guarda como filas
  `app_name='sql_integrity'`, una por base y por reinicio, idempotente ante reenvíos, sin cambios de
  esquema. Contrato en [10 §7.5](10-contrato-ingesta-agente.md). Ver `main.py::_ingerir_sql_integrity`.
- **Visualización y alertas: después**, con datos reales. Los demás consumidores de
  `software_monitoring` filtran por `app_name`, así que no se ven afectados; la pestaña Software no
  lo muestra todavía.
- **Orden de despliegue:** el servidor primero. Un servidor viejo descarta la clave sin error, pero
  pierde el dato.
- El agente se entrega como **4.5.2** (no 4.6: `schema_version` "4.6" no lo reconoce este servidor y
  lo trataría como formato legacy).

---

## 3. Priorización y fases

*Por definir cuando estén todos los requerimientos.* Entrada prevista: los requerimientos de §2 y
los pendientes ya documentados (checklist §8 del plan v4.5 del agente, rollout de tokens por
hospital, migraciones de base de datos del server, S5/P2/P3/P5). Para cada ítem: fase A/B/C,
si bloquea el despliegue masivo, y criterio de avance / rollback.
