# Plan — reestructurar el motor de alertas para agregar detectores más fácil

**✅ Ejecutado y verificado — 2026-09-10.** A pedido explícito, esta vez se hizo todo de
una sola vez (no paso a paso con revisión intermedia, como el refactor de
`dashboard.py`). Sin detecciones nuevas -- solo reorganización, tal como se pidió. Ver §8
para el detalle de qué se verificó.

**✅ Confirmado con el usuario**: el dolor es (B) — agregar un tipo de chequeo/integración
nuevo — y la idea es exactamente un **orquestador**: un solo archivo nuevo por algoritmo de
detección, que analiza sus métricas, decide el nivel de alerta, y se lo pasa al
orquestador central (que ya existe como concepto: es `actualizar_estado_alerta`, el
gestor de incidentes que crea/cierra/reabre en Asana). Ver §7 con las preguntas que
todavía quedan abiertas.

## 1. El problema, concreto

`dashboard_app/alerts_engine.py` (1226 líneas) hoy mezcla en un solo archivo: config,
el motor de exclusiones, el "gestor de incidentes" (que crea/cierra/reabre tickets de
Asana), y **cinco** categorías de detección con formas bastante distintas entre sí:

| Categoría | Función(es) | Dispara desde |
|---|---|---|
| Infraestructura (CPU/RAM/temp/fans/PSU/RAID/latencia/uptime) | `_evaluar_reglas_v3` | Loop por hospital dentro de `procesar_offline()` |
| Conectividad | `_verificar_conectividad` | `procesar_offline()` |
| KPIs de negocio (RIS, Mamografía) | `verificar_actividad_ris`, `verificar_actividad_mamo` | `verificar_kpis_programados()`, una vez al día a una hora configurada |
| Mirth Connect | `_verificar_mirth` | `verificar_estado_software()` |
| Autoenrute DICOM | `_verificar_autoenrute_dicom` | `verificar_estado_software()` |

**Por qué agregar una categoría nueva hoy es molesto**, con ejemplos reales del propio
archivo:

1. **No hay un solo lugar de registro.** Según qué tipo de chequeo sea, hay que enganchar
   el código en uno de tres sitios distintos y con forma distinta: el loop inline de
   `procesar_offline()` (infra), la lista hardcodeada de `verificar_kpis_programados()`
   (KPIs — literalmente un comentario dice `# Cuando agregues más, irán aquí`), o el
   `if config.get(...)` de `verificar_estado_software()` (software). No hay una función
   "agregar detector" — hay que saber cuál de los tres patrones te toca y copiarlo a mano.
2. **`verificar_actividad_ris` y `verificar_actividad_mamo` son casi el mismo código
   duplicado** (~35 líneas cada una): cargar config, resolver responsables, listar
   hospitales con RIS, filtrar por el switch granular del hospital, sumar admisiones por
   modalidad en una ventana de tiempo, comparar contra cero, abrir o cerrar. Un tercer KPI
   de este tipo (ej. "cero estudios de TC en 12 horas") sería copiar y pegar esa función
   una vez más, cambiando 5 valores.
3. **Los switches de habilitación y sus defaults viven en 3 lugares que tienen que
   coincidir a mano**: `cargar_config()` (el default del backend), `KPI_DEFAULTS` (el
   default específico de KPIs granulares por hospital), y la UI (`dashboard.get_kpi_settings()`
   + `script.js`). El propio archivo tiene comentarios documentando **dos bugs reales ya
   ocurridos** por esta desincronización (`FIX BUG 3`: defaults invertidos entre motor y
   UI — una alerta que nunca disparaba, y otra que disparaba siempre aunque el switch
   estuviera apagado).
4. **Hallazgo concreto que confirma el problema**: `main.py` ya ingesta y guarda en
   `software_monitoring` los certificados SSL (`app_name='ssl_certificate'`, con
   `metric_value` = días restantes) y los eventos de log de Elasticsearch/Suitestensa
   (`app_name='elasticsearch'`, con `status_value` = severidad ya resuelta contra
   `log_dictionary`) — **pero `alerts_engine.py` no tiene ninguna lógica que lea esas filas
   y genere una alerta.** Los datos están, la integración con Asana está, el patrón de
   Mirth/DICOM ya resuelve el mismo tipo de problema (leer `software_monitoring`,
   comparar, alertar) — pero nadie conectó los puntos, probablemente por la fricción de
   sumar una categoría nueva. Es el ejemplo más concreto de "necesito agregar una alerta
   para una métrica que ya mido" que tenemos disponible ahora mismo.

## 2. Dos tipos de "agregar una alerta nueva" — importante no confundirlos

- **(A) Una métrica nueva dentro de un reporte que YA se procesa** (ej. un sensor nuevo
  que manda el agente, un campo nuevo en `physical_layer`): esto **ya es fácil hoy**.
  Es agregar 2-3 líneas dentro de `_evaluar_reglas_v3()` (calcular un valor, decidir un
  nivel, `hallazgos["ALGO_NUEVO"] = (nivel, mensaje)`) — no hace falta tocar nada de lo
  que sigue en este documento.
- **(B) Un tipo de chequeo/integración nuevo** (una fuente de datos nueva, un algoritmo de
  detección distinto — como los certificados SSL o Elasticsearch del punto 1.4, o algo
  futuro que hoy no existe): esto es lo que **hoy es difícil** y lo que ataca este plan.

Asumo que el pedido apunta a (B) por cómo lo describiste ("algoritmos de detección",
"estructuras generales"), pero decime si en realidad el dolor es (A) — si es así el plan
es mucho más chico (ni hace falta reestructurar nada).

## 3. Arquitectura objetivo

Mismo criterio que el refactor de `dashboard.py`: separar por dominio, sin cambiar
comportamiento, y dejar un solo lugar donde "ver todo lo que corre". Concretamente,
convertir `dashboard_app/alerts_engine.py` en un paquete:

```
dashboard_app/alerts_engine/
    __init__.py          # Re-exporta todo lo que usan server.py, alertas_config.py y
                          # hospital_detalle.py hoy (cargar_config, procesar_offline,
                          # verificar_kpis_programados, verificar_estado_software,
                          # _match_patron, cargar_exclusiones, _serie_de, _ventana,
                          # _drena) -- "import alerts_engine" sigue funcionando igual,
                          # ningún otro archivo se entera del cambio.
    config.py              # cargar_config, KPI_DEFAULTS, _kpi_habilitado, _followers_de
    estado.py               # actualizar_estado_alerta (el gestor de incidentes),
                             # _parsear_timestamp, _ORDEN_NIVEL — el "sink" común por
                             # el que TODO detector, nuevo o viejo, reporta un hallazgo
    exclusiones.py            # cargar_exclusiones, evaluar_exclusion, _match_patron,
                               # _registrar_hit
    infra.py                   # _evaluar_reglas_v3 + los _nivel_* + _verificar_conectividad
    kpis_negocio.py              # verificar_actividad_ris, verificar_actividad_mamo,
                                  # _crear_alerta_kpi_generica, MÁS un runner común
                                  # (ver §4) para que el próximo KPI sea datos, no código
    software/
        __init__.py
        mirth.py                  # _verificar_mirth
        dicom_autoenrute.py         # _verificar_autoenrute_dicom + _serie_de/_drena/_ventana
    orquestador.py                  # procesar_offline, verificar_kpis_programados,
                                     # verificar_estado_software -- el "orquestador": la
                                     # lista explícita de qué detectores existen, de qué
                                     # categoría son (infra / kpi diario / software), y
                                     # los dispara en el tick de 60s. Un solo lugar para
                                     # ver -- y agregar -- todo el motor de un vistazo.
```

El **orquestador** (`orquestador.py`) es el punto de entrada que arma `server.py`. Cada
detector (un archivo dentro de `infra.py`, `kpis_negocio/`, `software/`, o uno nuevo que
vos agregues) analiza sus propias métricas y termina llamando a
`estado.actualizar_estado_alerta(...)` -- ese es el único contrato que un detector nuevo
tiene que cumplir: "acá está el hallazgo (hospital, tipo, nivel, mensaje)", y el
orquestador/gestor de incidentes se encarga del resto (dedup, Asana, reapertura,
exclusiones).

**Nada de esto cambia el comportamiento** — mismos umbrales, mismas queries, mismo
gestor de incidentes, mismos endpoints externos que consumen `alerts_engine`. Es
reorganización + un lugar central de registro, exactamente como hicimos con
`dashboard.py`.

### Dependencia externa a tener en cuenta

`dashboard_app/routers/hospital_detalle.py` importa y usa directamente `_serie_de`,
`_ventana` y `_drena` (los primitivos del algoritmo de "drenaje" de DICOM) para mostrar el
mismo estado de salud en el panel de detalle del hospital. Si esas funciones se mueven a
`software/dicom_autoenrute.py`, hay que actualizar ese import — es el mismo tipo de ajuste
que ya hicimos varias veces en el refactor de `dashboard.py` (símbolo que se usa desde
otro archivo, se re-exporta o se actualiza el import).

## 4. Caso especial dentro del mismo patrón: RIS y MAMO comparten casi todo

El contrato general es "un archivo por algoritmo de detección, que le reporta al
orquestador" (§3). Los KPIs de negocio son un caso particular DENTRO de ese mismo
patrón: `verificar_actividad_ris` y `verificar_actividad_mamo` no son dos algoritmos
distintos, son el mismo algoritmo con 5 parámetros distintos (qué config habilita el
chequeo, ventana de tiempo, en qué unidad, qué modalidades contar, a quién avisar).

Para no copiar y pegar esas 35 líneas una tercera vez, propongo un helper chiquito y
compartido *dentro* de `kpis_negocio/` (no expuesto al resto del motor) del que cada KPI
tira: `kpis_negocio/ris.py` y `kpis_negocio/mamo.py` quedarían como archivos cortos que
solo declaran sus 5 parámetros y llaman al helper — siguen siendo "un archivo por
detector", pero sin repetir la lógica. Un tercer KPI de este tipo (ej. "cero estudios de
TC en 12 horas") sería un archivo nuevo igual de corto.

Para Mirth/DICOM no propongo un helper compartido: son algoritmos genuinamente distintos
entre sí (debounce de 2 ticks vs. heurística de drenaje sobre una serie temporal) y de los
KPIs, forzar código compartido ahí sería una abstracción artificial. Cada uno queda como
su propio archivo autocontenido.

## 5. Cómo se vería agregar el caso concreto del punto 1.4 (certificados SSL), una vez hecho esto

Sin escribir código todavía, para que quede claro qué gana la reestructuración: hoy,
agregar la alerta de certificados SSL por vencer significaría escribir una función nueva
parecida a `_verificar_mirth` (query a `software_monitoring` filtrando
`app_name='ssl_certificate'`, comparar `metric_value` — días restantes — contra un
umbral configurable, armar el mensaje, llamar a `actualizar_estado_alerta`), agregar sus
claves de config a `cargar_config()`, agregarla al `if` de `verificar_estado_software()`,
y agregar los campos correspondientes al panel de configuración. Con la reestructuración,
sigue siendo básicamente ese mismo trabajo (no hay magia que lo evite — es lógica de
negocio real), pero: sabés exactamente en qué archivo nuevo escribirla
(`software/ssl_certificados.py`), no tenés que tocar tres lugares para conectarla (se
agrega al `orquestador.py` una sola vez), y el archivo de 1226 líneas no crece más — queda
un archivo nuevo, chico, fácil de revisar.

## 6. Orden de migración propuesto (si se aprueba)

Mismo criterio que el refactor de `dashboard.py`: de menor a mayor riesgo, un paso a la
vez, con checklist (sintaxis + import real + `TestClient` con datos de prueba) después de
cada uno, y confirmación en producción antes de seguir al siguiente.

1. `exclusiones.py` — autocontenido, sin dependencias de otras categorías.
2. `config.py` — `cargar_config`, `KPI_DEFAULTS`, `_kpi_habilitado`, `_followers_de`.
3. `estado.py` — `actualizar_estado_alerta` y sus helpers. El más sensible de los
   "compartidos" porque lo llama todo detector; se prueba a fondo (crear, cerrar, reabrir,
   excluir) antes de seguir.
4. `infra.py` — `_evaluar_reglas_v3` + `_verificar_conectividad`.
5. `software/mirth.py` y `software/dicom_autoenrute.py` — incluye actualizar el import en
   `hospital_detalle.py`.
6. `kpis_negocio.py` — acá se construye el runner común y se migran RIS y MAMO a usarlo.
   Es el paso de mayor esfuerzo de diseño, no de riesgo (misma lógica, reorganizada).
7. `orquestador.py` — último paso: mover la orquestación del tick (hoy repartida entre
   `procesar_offline`, `verificar_kpis_programados`, `verificar_estado_software`) a que
   recorra una lista explícita de detectores en vez de tener las llamadas hardcodeadas.

## 7. Preguntas para vos antes de aprobar

**Resueltas**: el dolor es (B), y el patrón es "un archivo por algoritmo → reporta al
orquestador" (confirmado). Quedan estas dos:

1. ¿Querés que la primera prueba real del patrón nuevo sea justamente cerrar el hueco de
   SSL/Elasticsearch (§1.4/§5) — es decir, que el refactor termine con dos detectores
   nuevos funcionando de verdad, no solo el esqueleto reorganizado? ¿O preferís que este
   trabajo sea solo la reestructuración, y vos agregás el contenido nuevo después con el
   patrón ya armado?
2. ¿Arrancamos ya con el orden del §6 (mismo estilo que `dashboard.py`: un paso a la vez,
   con checklist y tu confirmación en el medio), o preferís que primero te muestre un
   ejemplo más detallado de cómo queda un archivo de detector (por ejemplo
   `kpis_negocio/ris.py`) antes de tocar nada del archivo real?

**Respondidas**: sin detecciones nuevas (el hueco de SSL/Elasticsearch queda para después,
la forma de detección todavía está en definición), y ejecutar todo de una sola vez en vez
de paso a paso.

---

## 8. Qué se hizo y cómo se verificó

`dashboard_app/alerts_engine.py` (1226 líneas, un solo archivo) se convirtió en un
paquete de 13 archivos (1366 líneas en total -- el crecimiento es overhead esperado de
separar en módulos: imports y docstrings repetidos, no lógica duplicada):

| Archivo | Líneas | Contenido |
|---|---|---|
| `__init__.py` | 18 | Re-exporta los 9 símbolos que usan `server.py` y los routers (`cargar_config`, `cargar_exclusiones`, `_match_patron`, `procesar_offline`, `verificar_kpis_programados`, `verificar_estado_software`, `_serie_de`, `_ventana`, `_drena`). `import alerts_engine` sigue funcionando igual en todo el resto del repo. |
| `config.py` | 148 | `cargar_config`, `KPI_DEFAULTS`, `_kpi_habilitado`, `_followers_de`. |
| `estado.py` | 176 | `actualizar_estado_alerta` (el gestor de incidentes) + `_parsear_timestamp`. El "sink" común de todo detector. |
| `exclusiones.py` | 119 | `cargar_exclusiones`, `evaluar_exclusion`, `_match_patron`, `_registrar_hit`. Autocontenido. |
| `infra.py` | 338 | `_evaluar_reglas_v3`, `_verificar_conectividad`, `analizar_reporte`, y `verificar_infra_hospitales` (nuevo -- ver nota abajo). El más grande porque evalúa muchos hallazgos de un mismo reporte. |
| `kpis_negocio/_runner.py` | 90 | El runner compartido (§4) -- acá vive la lógica que antes estaba duplicada entre RIS y MAMO. |
| `kpis_negocio/ris.py` | 28 | Solo declara los 5 parámetros de RIS y llama al runner. |
| `kpis_negocio/mamo.py` | 26 | Ídem para Mamografía. |
| `software/mirth.py` | 84 | `verificar_mirth` (debounce de 2 ticks). |
| `software/dicom_autoenrute.py` | 210 | `verificar_autoenrute_dicom` + `_serie_de`/`_drena`/`_ventana` (la heurística de drenaje). |
| `orquestador.py` | 129 | `procesar_offline`, `verificar_kpis_programados`, `verificar_estado_software` -- acá se ve y se agrega qué corre en cada tick. |

**Un ajuste estructural, no de comportamiento**: el loop que recorría todos los
hospitales y llamaba a `_evaluar_reglas_v3` vivía inline dentro de `procesar_offline`.
Se extrajo a `infra.verificar_infra_hospitales()` para que el detector de infra sea
autocontenido igual que Mirth/DICOM/KPIs (que ya tenían su propio loop). El orquestador
ahora solo llama a esa función y usa el diccionario de contadores que devuelve para el
mismo log de health-check de siempre -- mismo comportamiento, mismos números.

**Dependencia externa preservada**: `dashboard_app/routers/hospital_detalle.py` sigue
llamando a `alerts_engine._serie_de`, `_ventana` y `_drena` sin cambios (re-exportados
desde `software/dicom_autoenrute.py`).

### Checklist

1. **Sintaxis** (`py_compile` en los 13 archivos) — ✅ OK.
2. **Import real del paquete** (`import alerts_engine` con `JWT_SECRET`/`ASANA_ACCESS_TOKEN`
   dummy) y confirmación de que los 9 símbolos del contrato externo existen — ✅ los 9.
3. **Import de `dashboard_app.dashboard` y de `server.py`** (el entrypoint real) — ✅ 74
   rutas, sin cambios.
4. **Tick completo de punta a punta** (`procesar_offline` con datos de prueba reales:
   un hospital con CPU al 95% y un canal Mirth en `STOPPED` dos ticks seguidos) — ✅ se
   crearon las dos alertas esperadas (`HOST_CPU` y `MIRTH_...`), con el log de
   health-check mostrando los contadores correctos.
5. **KPI RIS** (hospital sin producción reciente) — ✅ generó `KPI_INACT_RAD`. **KPI
   MAMO con el switch apagado** — ✅ correctamente no generó nada.
6. **DICOM autoenrute** (cola estancada) — ✅ generó `CRITICAL` una vez corregido un
   defecto de la prueba (no del código): el primer intento usó un dato exactamente en el
   borde de la ventana de 45 min, y el tiempo real transcurrido durante la prueba lo
   empujó justo afuera. Con datos mejor distribuidos en el tiempo, disparó correctamente.
7. **Motor de exclusiones** (`evaluar_exclusion` con match y sin match) — ✅ ambos casos
   correctos.
8. **Los dos consumidores externos, con `TestClient` + login real**: `routers/alertas_config.py`
   (usa `_match_patron` y `cargar_exclusiones` vía los endpoints de exclusiones) y
   `routers/hospital_detalle.py` (usa `cargar_config`/`_serie_de`/`_ventana`/`_drena` vía
   `/api/hospital/{id}/software`) — ✅ ambos en 200 con datos reales.

### Qué falta de tu lado

1. Desplegar toda la carpeta `dashboard_app/alerts_engine/` (nueva) y confirmar que ya
   no queda el archivo viejo `dashboard_app/alerts_engine.py` en el servidor (si el
   deploy es por copia de archivos y no por git, un archivo viejo residual causaría un
   conflicto de nombre módulo/paquete).
2. Reiniciar el proceso.
3. Mirar los logs del primer par de ticks (cada 60s) para confirmar que el log de
   health-check (`🩺 [Vigilancia] Tick | ...`) sigue apareciendo igual que siempre.
4. Cuando quieras retomar el tema de SSL/Elasticsearch (§1.4), avisame -- con esta
   estructura ya armada, ese trabajo futuro es agregar un archivo en `software/` y una
   línea en `orquestador.py`, no tocar un archivo de 1226 líneas.
