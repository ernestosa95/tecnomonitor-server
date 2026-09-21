# Última milla de alertas hacia Asana — foco en autoenrute DICOM

**Contexto**: la detección, ingesta y visualización del motor de alertas ya funcionaban
correctamente; el trabajo de esta iteración fue exclusivamente sobre la "última milla":
qué contenido, título y seguidores lleva el ticket de Asana que se crea cuando se levanta
una alerta, tomando como caso piloto el detector de autoenrute DICOM
(`dashboard_app/alerts_engine/software/dicom_autoenrute.py`).

Fecha: 2026-09-17. Ver también [09-plan-refactor-alertas.md](09-plan-refactor-alertas.md)
(arquitectura del motor de alertas) y [02-modelo-de-datos.md](02-modelo-de-datos.md)
(tabla `software_monitoring`, de donde lee este detector).

## 1. Seguidores específicos para autoenrute DICOM (bug corregido)

**Síntoma**: los tickets de autoenrute DICOM nunca tenían seguidores en Asana.

**Causa**: el campo `dicom_responsible_email` existía en el backend (`config.py`,
`routers/alertas_config.py`) pero el panel de configuración (`static/script.js` +
`templates/index_beta.html`) nunca lo cargaba ni lo guardaba — no había buscador de
colaboradores para DICOM, a diferencia de KPI-RAD, Mirth y el selector "Global" (usado por
infraestructura).

**Fix**: se agregó el buscador de colaboradores "Responsables Asana (Autoenrutado DICOM)"
en la UI, replicando el patrón ya usado por Mirth (variable `dicomSelectedUsers`, funciones
`renderDicomRespChips/addDicomResp/removeDicomResp/renderDicomRespList`, wireado en
`cargarConfig()`/`guardarConfig()`). El backend no necesitó cambios — ya estaba completo.

Cada categoría de alerta (KPI, Mirth, DICOM, infraestructura) mantiene su propio
responsable/lista de seguidores — a pedido explícito, no se unificó en un selector único
global porque los colaboradores de cada categoría son personas distintas.

## 2. Título del ticket con los nodos que conecta la ruta

**Antes**: el título salía como `DICOM_ROUTE_<id numérico>` (ej. `DICOM_ROUTE_6`), sin
información legible sobre qué ruta es.

**Causa de un bug relacionado, encontrado en el camino**: `main.py` (ingesta) guarda el
nombre legible de la ruta en `extra_data["label"]` (formato `"NODO-A → NODO-B"`), pero el
detector buscaba una clave `"ruta"` que nunca existió — por eso el mensaje del ticket
también caía siempre al fallback `"regla #<id>"`.

**Fix final** (`dicom_autoenrute.py`):
- El **cuerpo del mensaje** ahora usa `extra_data["label"]` cuando está disponible.
- El **título del ticket** arma `DICOM_ROUTE_<NODOORIGEN>_TO_<NODODESTINO>` a partir de
  `extra_data.from_nickname`/`to_nickname` (con fallback a `from_hostname`/`to_hostname`),
  saneados a mayúsculas con guiones (`_sanear_nodo`). Si el agente no informó nombres de
  nodo (agente viejo), cae al formato original `DICOM_ROUTE_<id numérico>` — sin romper
  nada.
- La clave interna de dedupe/tracking en la tabla `alertas` (`tipo_unico`) **no cambió**:
  sigue siendo `DICOM_ROUTE_<id numérico>`, siempre estable entre ticks. Lo que cambia es
  solo el nombre visible del ticket en Asana, pasado como parámetro nuevo y opcional
  `titulo_visible` en `estado.actualizar_estado_alerta(...)`. Cualquier detector nuevo puede
  usar este mismo mecanismo si quiere un nombre más legible que su clave interna.

## 3. Datos cuantitativos en las notas del ticket

Los mensajes de WARNING/CRITICAL/OK que arma `dicom_autoenrute.py` ahora incluyen
explícitamente: instancias pendientes actuales, pico y mínimo de la ventana evaluada, y el
umbral de drenaje configurado (%). El cierre por "cola por debajo del piso de ruido"
también muestra el piso configurado. Antes esta información estaba parcialmente presente
pero de forma menos estructurada.

## 3bis. Corrección 2026-09-18: una cola que solo crece ahora alerta

**Bug encontrado en producción**: la regla #126 (origen nulo → `CORD-PACS-BK`) acumuló 1,4 M
de instancias pendientes en 4 días y nunca alertó (se detectó por un disco lleno). Causa: el
criterio 3 de `_drena` ("subió al menos 300 instancias desde el piso de la ventana = está
recibiendo un estudio masivo") daba por sana a cualquier cola en crecimiento, en ambas
ventanas. El detector solo atrapaba colas planas, no las que crecen sin parar. El nodo de
origen nulo **no** era la causa.

Cambio (`dicom_autoenrute.py`, y el panel de detalle que reusa `_drena`): en la **ventana
crítica**, el crecimiento solo cuenta como llegada de un estudio masivo si la cola **tocó el
piso de ruido dentro de esa ventana** (`_crecimiento_permitido_critica`), o sea, venía vacía
y recién se llenó. Una cola que estuvo toda la ventana (120 min por defecto) por encima del
piso sin bajar queda en CRITICAL ("creciendo ... sin descender"). La ventana corta (warning)
sigue tolerando el crecimiento.

Efecto a tener en cuenta: un import masivo legítimo que se sostenga más de 2 horas seguidas
por encima del piso sin drenar abre un ticket CRITICAL (se prefirió avisar de más antes que
no avisar).

Cosmético en la misma corrección: una regla sin nodo de origen (`FROMNODE` nulo en el PACS =
toma todos los equipos) ahora se muestra como `TODOS → <destino>` en el panel (antes
`? → <destino>`) y el título del ticket pasa a `DICOM_ROUTE_TODOS_TO_<DESTINO>`. El
identificador de la alerta (`DICOM_ROUTE_<id>`) no cambia.

## 3ter. Corrección 2026-09-18 (etapa 1): reaperturas por "pico del serrucho"

**Problema**: el ticket de H45 (regla 39) acumuló 116+ comentarios en 15 días
(cierra/reabre en minutos). Backtest sobre 10 días de series de todos los hospitales
(2026-09-08 → 09-18):

- H45/39, 42 y 53 tienen un **piso residual inmóvil** (1164 / 2490 / 2071 pendientes durante
  días) con picos que drenan en 5–15 min. Piso plano = "sin drenaje" (abre), llega un pico que
  drena (cierra), vuelve el piso (reabre). Ya ocurría con la lógica original (50–76 ciclos
  abrir→cerrar en 10 días).
- La corrección 3bis lo empeoró (127–148 ciclos) y generó 9 ciclos falsos en HUPC/126 durante
  5 días de serrucho sano: `_drena` medía "¿bajó respecto del pico?" comparando el valor
  ACTUAL con el pico de la ventana, y cuando llega una tanda nueva el actual **es** el pico.

**Cambio (`dicom_autoenrute.py`, único archivo; el panel de detalle lo hereda porque usa la
misma `_drena`)**: la cola está viva si **drenó en algún momento de la ventana** (mayor caída
pico → valor posterior ≥ 300 instancias o ≥ `100 - dicom_drain_percent`% del pico), no si está
por debajo de su pico ahora. El permiso de crecimiento (ventana corta siempre; crítica solo si
tocó el piso de ruido) no cambia.

**Resultado del backtest** (ventana crítica 180 min): HUPC/126 pasa de 9 ciclos falsos a 1
(el incidente real, detectado 14/09 14:18, ≈3 h después de la última bajada real); H45
39/42/53 baja de 127–148 a 45–72 ciclos; P12/4, P03/1 y P12/2 siguen alertando igual.

**La etapa 2 (piso adaptativo por regla) está en §3quater.**

## 3quater. Corrección 2026-09-18 (etapa 2): piso habitual adaptativo por regla

**Problema que queda tras la etapa 1**: H45/39, 42 y 53 tienen un residuo constante (1164 /
2490 / 2071 pendientes inmóviles durante días). Mientras no llega una tanda, ese piso plano
sigue viéndose como "sin drenaje" y abre el ticket; la tanda drena, cierra; vuelve el piso,
reabre (45–72 ciclos en 10 días incluso con la etapa 1).

**Cambio**: cada regla con actividad demostrada tiene un **piso habitual** (percentil 10 de sus
últimos 7 días, tabla `dicom_regla_baseline`) y solo se evalúa el **exceso** sobre ese piso:

- Cola por debajo de `piso + tolerancia` (`max(300, 15% del piso)`) → OK ("en su piso
  habitual"). Cierra un incidente abierto.
- Cola por encima → mismo criterio de drenaje de la etapa 1, con un cambio: el permiso de
  crecimiento de la ventana crítica exige haber tocado `max(dicom_min_instances, piso + tolerancia)`
  dentro de la ventana (antes solo `dicom_min_instances`). Sin esto, una tanda que arranca desde
  un piso de 1164 se declaraba trabada en su flanco de subida.
- Todo el criterio vive en una sola función, `evaluar_cola()` (`dicom_autoenrute.py`), que usan
  el detector y el panel de detalle del hospital: ya no hay dos copias que puedan
  desincronizarse.

**Guardas** (para no "aprender" un incidente como normal):

| Guarda | Efecto |
|---|---|
| Solo reglas con **actividad demostrada** (≥ 5 bajadas ≥ 300 entre muestras en 7 días) | Una regla que nunca drenó (P12/4 plana en 15 800 durante 10 días) no tiene piso y sigue alertando |
| Necesita ≥ 23 h y ≥ 150 muestras de historia | Reglas nuevas usan solo el criterio de la etapa 1 hasta tener baseline |
| **Congelado** con alerta abierta | Una regla que está alertando no se re-aprende |
| El piso **sube** máx. 25 % (o +100) por vez y una vez cada 24 h; **baja** de inmediato | Si el residuo de una regla se triplica, la regla alerta hasta que el piso lo alcance (~4 días). Se prefiere avisar antes que aceptarlo en silencio |
| Interruptor `dicom_baseline_enabled` | `INSERT OR REPLACE INTO configuracion (clave, valor) VALUES ('dicom_baseline_enabled', '0');` vuelve al comportamiento de la etapa 1 en el próximo tick (sin UI a propósito) |

**Costo operativo**: `refrescar_baselines()` corre dentro del tick de autoenrute (cada 60 s)
pero recalcula como máximo 3 hospitales por tick y cada uno cada 6 h (una query indexada de 7
días por hospital, sin `extra_data`). Tras un reinicio parte de la última fecha grabada en la
tabla, no recalcula todo de golpe.

**Backtest** (10 días, todos los hospitales, ventana crítica 180 min, con el código real):

| Regla | Original | Etapa 0 (3bis) | Etapa 1 | Etapa 2 |
|---|---|---|---|---|
| H45/39 | 50 ciclos | 127 | 45 | **0** (tras las primeras 24 h sin baseline) |
| H45/42 | 66 | 148 | 60 | **0** |
| H45/53 | 76 | 143 | 72 | **0** |
| HUPC/126 | 0 (ciego) | 9 falsos | 1 | 1 (incidente real, 14/09 14:18) |
| P12/4, P03/1, P12/2 | 1 c/u | 1 | 1 | 1 |

**Límites conocidos**: (1) la latencia de detección de un incidente es la ventana crítica
contada desde la última bajada real (HUPC/126: ~3 h); acortarla exige bajar
`dicom_stall_critical_minutes` o adelantar el WARNING. (2) El residuo constante ya no genera
ticket: se ve en el panel (`piso_habitual` en el estado de la regla) pero no hay todavía un
aviso informativo aparte. (3) Se probaron y descartaron ventanas adaptativas por duración de
tandas (empeoraron a 73–77 ciclos) y un umbral de caída relativa mínima (sin efecto).

## 4. Mecanismo de protocolos de atención ("runbooks")

**Objetivo**: que el ticket de Asana lleve un link al protocolo de atención de ese tipo de
alerta, para que el colaborador que lo recibe sepa qué hacer sin buscar en otro lado.

**Decisión de diseño**: el contenido vive en archivos `.md` versionados en el repo (no
editable desde la UI) — se prioriza simplicidad ahora; si en el futuro hace falta editar el
protocolo sin pasar por un deploy, se puede migrar a un modelo editable desde el dashboard.

**Piezas**:

| Archivo | Rol |
|---|---|
| `docs/runbooks/<slug>.md` | Contenido del protocolo. Hoy solo existe `dicom-autoenrute.md`, como borrador con la estructura sugerida (síntoma, diagnóstico rápido, mitigación, cuándo escalar, cierre) — **falta completarlo con el conocimiento operativo real**. |
| `dashboard_app/alerts_engine/runbooks.py` | Mapeo prefijo-de-`tipo_unico` → slug (hoy: `"DICOM_ROUTE_" → "dicom-autoenrute"`). `runbook_url_para(tipo_unico)` devuelve el link completo o `None` si la categoría todavía no tiene protocolo escrito. |
| `dashboard_app/routers/runbooks.py` | Ruta `GET /runbooks/{slug}`, autenticada (roles Admin/Ingeniería, igual que el resto de la config de alertas). Lee el `.md`, lo convierte a HTML con la librería `markdown`, lo renderiza con `templates/runbook.html`. Slug validado con regex estricta (solo minúsculas/números/guiones) para evitar path traversal. |
| `dashboard_app/templates/runbook.html` | Plantilla mínima, mismo esquema de colores oscuro que `index_beta.html`/`login.html`. |
| `dashboard_app/alerts_engine/estado.py` | Resuelve `runbook_url_para(tipo_unico)` en el gestor de incidentes (el único punto por el que pasan todos los detectores) y lo pasa a `asana_conector.crear_tarea_alerta(..., runbook_url=...)`. |
| `dashboard_app/asana_conector.py` | `crear_tarea_alerta` agrega la línea `📖 Protocolo de atención: <link>` en las notas cuando `runbook_url` no es `None`. Si es `None` (categoría sin protocolo todavía), el comportamiento es idéntico al de antes. |

**El link apunta al propio dashboard, no a GitHub**: los seguidores configurados en Asana
ya son usuarios de TecnoMonitor (se resuelven por email vía `UserModel`), pero no
necesariamente tienen acceso al repositorio privado.

**Dominio usado en el link**: variable de entorno `PUBLIC_BASE_URL`, con default
`https://tecnomonitor.tecnoimagen.com.ar` (tomado de `ORIGINES_PERMITIDOS` en
`dashboard.py`, la lista de CORS). Si el dominio público real difiere, setear
`PUBLIC_BASE_URL` en el `.env` del servidor.

### Cómo agregar un protocolo para otra categoría de alerta

1. Escribir `docs/runbooks/<slug-nuevo>.md`.
2. Agregar una línea en `_RUNBOOKS_POR_PREFIJO` en
   `dashboard_app/alerts_engine/runbooks.py`, mapeando el prefijo de `tipo_unico` de esa
   categoría (ej. `"MIRTH_"`) al slug nuevo.
3. Nada más — `estado.py` ya resuelve el link automáticamente para cualquier `tipo_unico`
   que matchee, sin tocar el detector.

## 5. Segunda vuelta (mismo día, a la tarde): gap de reaperturas + protocolo real

Disparador: se imprimió un ticket real ya reabierto varias veces
(`H45 | DICOM_ROUTE_CT140938_TO_SIPROSAPAD`, 14 días de vida, 138 comentarios) y no tenía
ni colaboradores ni link al protocolo, pese a que ambos ya estaban implementados (§1 y §4).

**Causa**: `asana_conector.actualizar_tarea_asana()` — la función que se usa tanto para
"cambio de gravedad" (caso B2) como para reapertura (caso B3, `reabrir=True`) — nunca
tocaba followers ni notas, solo título y un comentario. Los colaboradores y el link al
protocolo solo se escribían en `crear_tarea_alerta()`, que corre una única vez, la primera
vez que se abre el ticket. Un incidente que se reabre reutiliza el mismo `asana_task_gid`
para siempre — nunca vuelve a pasar por `crear_tarea_alerta`, así que nunca los recibía,
sin importar qué se configure después.

**Fix** (`asana_conector.actualizar_tarea_asana`): ahora acepta `extra_followers` y
`runbook_url`, igual que `crear_tarea_alerta`, y en cada actualización/reapertura:
- Agrega como seguidores a los colaboradores *configurados actualmente* para esa categoría,
  vía `add_followers_for_task` (aditivo — no duplica ni pisa a nadie ya presente).
- Agrega la línea `📖 Protocolo de atención: <link>` al comentario.

Esto cierra el gap para tickets viejos (como el de H45) y para el caso general: si el día
de mañana cambia la lista de colaboradores de una categoría, los incidentes ya abiertos
también se actualizan, en vez de quedar congelados con la lista vigente al momento de su
creación.

### Contador de reaperturas

El mismo ticket real mostró un patrón de "flapping": se cerró y volvió a abrir varias veces
en el mismo día sobre la misma ruta, indistinguible en el ticket de un incidente nuevo.

- Columna nueva `reaperturas` (Integer, default 0) en `AlertaModel` — migración en
  `Accesorios/agregar_reaperturas_alertas.py` (mismo patrón que el resto de `Accesorios/`,
  idempotente).
- `estado.py`, caso B3 (reincidencia dentro de los 15 días): incrementa
  `alerta.reaperturas` antes de reabrir.
- El comentario de reapertura en Asana ahora dice `INCIDENTE REABIERTO -- van N veces`
  cuando `N > 1`, para que quien atiende note de entrada si es la primera vez o si la ruta
  viene reincidiendo.
- **No implementado a propósito** (quedó solo anotado en el protocolo, §6 del runbook,
  como criterio de escalamiento): auto-escalar la severidad o notificar a alguien extra
  cuando el conteo de reaperturas supera un umbral en una ventana de días. Es una decisión
  de comportamiento de alertado, no solo de contenido del ticket — requiere definir el
  umbral y a quién notificar antes de programarlo.

### Protocolo de atención real cargado

`docs/runbooks/dicom-autoenrute.md` dejó de ser el borrador esqueleto y ahora tiene el
protocolo real: introducción al funcionamiento del autoenrute en Suitestensa, síntomas
comunes, las tablas de `ExtensaPACS` relevantes (`DICOMAUTOROUTINGRULES`,
`DICOMAUTOROUTINGQUEUE`, `DICOMAUTOROUTINGDELIVERED`) con sus queries de diagnóstico,
mitigación, cuándo escalar (incluyendo el criterio de reincidencia recién agregado), y una
plantilla de notificación de cierre para el referente del hospital.

### Resumen visual para el equipo

`docs/runbooks/resumen-dicom-autoenrute.png` — una plaquita de una página con el flujo
completo (agente → ingesta → detector → gestor de incidentes → Asana), qué dispara cada
nivel de alerta (ventanas warning/critical, piso de ruido, % de drenaje requerido, con el
contraste serrucho-sano vs. cola-trabada), y el changelog de esta iteración. Pensada para
compartir con el equipo, no para el flujo de la app.

## 6. Ideas abiertas, no implementadas (quedaron solo anotadas)

- **Umbrales por ruta**: el detector ya documenta que la línea de base de instancias
  pendientes varía muchísimo por ruta (de cientos a ~14.000); una ruta puntual puede oscilar
  justo alrededor del umbral configurado y generar flapping. No se tocó ningún umbral —
  quedó como una observación operativa a revisar caso por caso, no como código.
- **Responsable (assignee) fijo**: el campo "Responsable" de la tarea de Asana sigue siendo
  siempre la cuenta genérica `RESPONSABLE_GID` (aparece como "TecnoMonitor"), no una persona.
  Los colaboradores sí son configurables por categoría; la asignación real no. Señalado como
  posible mejora de proceso a futuro, no de esta iteración.
- **Auto-escalamiento por reincidencia** — ver nota en §5 arriba.

## 7. Hallazgo colateral, sin resolver (fuera de alcance de esta iteración)

Los defaults de `dicom_min_instances` y `dicom_drain_percent` están desalineados entre
`alerts_engine/config.py` (1000 / 90%) y `routers/alertas_config.py` (50 / 70%) — el mismo
tipo de bug que ya documenta el comentario `FIX BUG 3` en `config.py` para otros campos.
No afecta lo hecho en esta iteración (son umbrales de detección, no de la última milla),
pero conviene alinearlos en algún momento.

## 8. Checklist de despliegue (deploy manual, no por git)

Como el despliegue de este servidor es por copia manual de archivos (no `git pull`), los
**archivos nuevos** (no solo los modificados) hay que copiarlos a mano. Lista completa de
lo tocado en las dos vueltas de esta iteración:

**Modificados**:
- `dashboard_app/alerts_engine/estado.py`
- `dashboard_app/alerts_engine/software/dicom_autoenrute.py`
- `dashboard_app/asana_conector.py`
- `dashboard_app/dashboard.py`
- `dashboard_app/static/script.js`
- `dashboard_app/templates/index_beta.html`
- `database.py` (columna `reaperturas` en `AlertaModel`)
- `requirements.txt`

**Nuevos**:
- `dashboard_app/alerts_engine/runbooks.py`
- `dashboard_app/routers/runbooks.py`
- `dashboard_app/templates/runbook.html`
- `docs/runbooks/dicom-autoenrute.md` (contenido reemplazado en la segunda vuelta: pasó de
  borrador a protocolo real)
- `docs/runbooks/resumen-dicom-autoenrute.png` (solo para comunicar al equipo, no lo usa la
  app)
- `Accesorios/agregar_reaperturas_alertas.py` (script de migración, ver abajo)
- `docs/12-ultima-milla-alertas-asana.md` (este archivo)

**Orden de despliegue importante**: correr
`python3 Accesorios/agregar_reaperturas_alertas.py` **antes** de reiniciar `server.py` con
el código nuevo. `estado.py` ya lee/escribe `alerta.reaperturas` — si la columna no existe
todavía en la base, cualquier consulta a la tabla `alertas` va a fallar en cuanto se
levante el server con el código nuevo.

**Dependencia nueva**: `pip install markdown` (o `pip install -r requirements.txt`) antes
de levantar `server.py`, si no se instaló ya.

**Nota sobre `/monitor` vs `/beta`**: el selector de responsables de DICOM se agregó en
`templates/index_beta.html` (servido en `/beta`), que es la plantilla activamente
mantenida (`templates/index.html`, que se servía en `/monitor`, se retiró el 2026-09-21).
