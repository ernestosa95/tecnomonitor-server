# Plan — migración a PostgreSQL y rediseño del almacenamiento

**Estado — 2026-10-01: plan v2. Fase 0 hecha** (resultados en §9.2); **Fase 2 en curso**.
Decisiones abiertas que quedan: solo el detalle de inmutabilidad del archivo (decisión 1). La v1
(2026-09-18) decidió *a qué motor* ir; esta versión agrega *cómo guardar los datos* una vez allá, con
foco en performance y en bajar el almacenamiento. Las hipótesis de §2.3 y §5 quedaron medidas sobre
una foto de producción del 2026-10-01: el diseño se confirma y el almacenamiento resulta menor que
lo estimado.

---

## 1. Objetivos

1. **Performance:** que las vistas y el motor de alertas no dependan de recorrer o parsear el
   histórico (hoy el motor hace un `MAX(timestamp) … GROUP BY` sobre 2,6 M filas cada 60 s y los
   gráficos parsean miles de JSON en Python).
2. **Almacenamiento:** pasar de ~35 GB/año a un orden de magnitud menos, sin perder información.
3. **Auditoría:** conservar todo lo que llega, con una política explícita de cuánto tiempo y dónde
   (en la base o archivado), configurable y con piso.
4. **Un solo motor y una sola forma de leer los datos:** sin SQLite + archivos `historico_*.db`
   que nadie lee.

## 2. Situación actual

### 2.1 Medido en producción (2026-09-18)

| Tabla | Filas | Tamaño | Por fila | Ritmo |
|---|---|---|---|---|
| `reportes_historicos` | 2.663.140 (desde 2026-04-01) | **16,33 GB (~93 %)** | ~6,1 KB | ~16 mil filas/día, ~98 MB/día, ~35 GB/año |
| `software_monitoring` | 1.824.891 (desde 2026-04-02) | 0,5 GB | ~290 B | ~11 mil filas/día |
| `reportes_uso` | 43.347 (desde 2026-03-06) | 0,04 GB | | ~1 por hospital y hora |
| `alertas` | 1.513 (desde 2026-01-15) | mínimo | | |

Índices: ~0,3 GB en total. **El problema es `full_json_data`**: el reporte completo del agente
(salvo `software_monitoring` y `application_metrics`, que se separan) guardado tal cual cada 5
minutos por hospital (~57 hospitales × 288 reportes/día).

**Medición del 2026-09-30:** la base pesa **20,5 GB** (+1,8 GB en 12 días: ~150 MB/día, ~4,5
GB/mes). Disco del server: 85,3 GB, **35,1 GB libres** (33 GB el 2026-10-01).

**Corrección con la Fase 0 (2026-10-01):** se estimaba que con el resumen pausado el archivo crecería
a ~7–8 GB/mes. No es así: el resumen procesa un lote por día y en seis meses solo llegó a ~14 mil
filas resumidas (menos del 1 % de cada mes, §9.2 B), así que liberaba muy poco. El ritmo sigue siendo
**~4–4,5 GB/mes** y sube con los hospitales (62 en abril, 77 en septiembre). Con 33 GB libres:
**~4 meses hasta el umbral de 15 GB** (principios de 2027) y ~7 hasta llenar el disco. Volver a
prender el resumen no cambia ese plazo de forma apreciable. Un `VACUUM` para recuperar espacio no es
opción (server detenido y el doble de disco).

### 2.2 Qué cambió desde la v1

- **Fuentes nuevas en `software_monitoring`** (agentes 4.5.2 a 4.5.4): `sql_integrity` (una fila
  por base y por reinicio: despreciable), `sql_backup` (una fila por base y por backup:
  despreciable), `patient_portal` (≈16 estados × 288 lecturas/día por hospital con portal:
  ~4.600 filas/día cada uno). Más hospitales con Mirth (un hospital con ~25 canales son ~7.200
  filas/día). Es la tabla que más va a crecer en filas.
- **Tablas de configuración nuevas** (chicas, sin problema de volumen): `mirth_channel_topology`,
  `mirth_nodos`, `mirth_canales_meta`, `monitoreo_modulos`, `dicom_regla_baseline`.
- **`maintenance.py` corría** todos los días (`server.py:111`) resumiendo con pérdida los bloques
  de 30 min de más de 7 días. Desde 2026-09-30 está pausado por defecto con un interruptor (ver
  decisión 2, §11); lo resumido antes no se recupera.

### 2.3 Por qué pesa tanto

Cada reporte repite el **inventario** del hospital, que casi nunca cambia: modelo y serie del host,
nombres y configuración de cada VM, discos con su tamaño total, lista de sensores y fuentes,
versiones, `collection_meta`. Lo que cambia cada 5 minutos son unos pocos números (CPU, RAM,
temperaturas, uso de cada disco, latencia). **Medido (Fase 0): el 89,5 % de cada JSON es igual al
reporte anterior del mismo hospital** (88,9 % de los bytes); de 132 campos distintos, 97 cambian en
menos del 1 % de los reportes. Guardarlo entero cada 5 minutos es lo que lleva a 6 KB por fila.

### 2.4 Por qué es lento (lecturas calientes, verificadas en el código del 2026-09-30)

| Quién | Qué hace hoy | Costo |
|---|---|---|
| Motor de alertas, cada 60 s (`alerts_engine/infra.py:73-90`) | Último reporte de cada hospital con `MAX(timestamp) … GROUP BY` sobre toda la tabla, y parsea su JSON | Recorre el índice de 2,6 M filas por minuto |
| Gráfico de infraestructura `/history` (hasta 30 días) | Trae hasta 15.000 filas con su JSON y submuestrea en Python | Mueve ~90 MB por consulta de 30 días |
| PDF de infraestructura (`routers/informes.py:188`, `generator_report.py:782`, código duplicado) | Carga en memoria todas las filas del rango con su JSON | Igual, sin tope |
| Resumen de red y por hospital (`routers/resumen_red.py:145`, `resumen_hospital.py:113/159`) | Suma **todo** el histórico de `reportes_uso` de cada hospital en Python | Crece sin techo con el tiempo |
| Pestaña Software (24 h / 7 días) | Series de Mirth, autoenrute y portal desde `software_monitoring` con `extra_data` en JSON | Aceptable hoy; crece con los hospitales |
| Ingesta (`main.py`) | Endpoint `async` con sesión síncrona: bloquea el event loop mientras escribe | Escala mal con más agentes |

## 3. Principios del rediseño

1. **Separar inventario de métricas.** El inventario (host, VMs, discos, sensores, versiones) se
   guarda **solo cuando cambia**, con vigencia "desde / hasta". Las métricas, en filas angostas y
   tipadas cada 5 minutos. Juntas reconstruyen el estado de cualquier momento.
2. **Métricas en columnas, no en JSON.** Un número es un número: se consulta, se indexa y se
   comprime mucho mejor que dentro de un texto.
3. **Particionar por tiempo y comprimir por columnas lo viejo** (TimescaleDB): los datos de más de
   N días pasan a formato columnar comprimido, sin pérdida y transparente para las consultas.
4. **Agregados precalculados para leer** (por hora, por día, por mes): los gráficos de 7 y 30
   días, los PDF y los resúmenes leen agregados, no crudo. **No reemplazan al crudo**: se calculan
   a partir de él y se pueden regenerar.
5. **Estado actual materializado:** una fila por hospital con su último reporte, actualizada en la
   ingesta. Nadie más busca "el último" recorriendo el histórico.
6. **El JSON crudo, solo para auditoría y en niveles:** caliente en la base por un período
   configurable, después archivado comprimido fuera de la base (ver §6).

## 4. Modelo objetivo

### 4.1 Infraestructura (reemplaza a `reportes_historicos`)

| Tabla | Qué guarda | Cuándo escribe | Lo lee |
|---|---|---|---|
| `hospitales` | Catálogo (hoy `hospitales_metadata`) con una clave numérica corta | ABM | Todo |
| `inventario_host` | Modelo, serie, CPU/RAM totales, hipervisor, sensores disponibles | Solo si cambia (hash del bloque) | Detalle, PDF |
| `inventario_vm` | Por VM: nombre, tipo (vm/ws/eq), SO, CPU/RAM asignadas, discos con su tamaño | Solo si cambia | Detalle, PDF |
| `metricas_host` | 1 fila por reporte: CPU, RAM, potencia, temperatura ambiente y CPU, estado, uptime, latencia, uso de red | Cada reporte | Gráficos, PDF, alertas |
| `metricas_vm` | 1 fila por VM y reporte: CPU, RAM, uptime, estado y motivo (`state_reason`) | Cada reporte | Gráficos por VM, alertas |
| `metricas_disco` | 1 fila por disco y reporte: % y GB usados | Cada reporte (o solo si cambió más de un umbral, decisión 9) | Alertas de disco, PDF |
| `sensores` | Ventiladores, fuentes, RAID: estado | Solo si cambia | Alertas, detalle |
| `estado_actual_hospital` | Último reporte normalizado de cada hospital (y su JSON) | En la ingesta (upsert) | Detalle, OFFLINE, motor de alertas, resumen de red |
| `reporte_crudo` | El JSON del agente tal cual llegó | Cada reporte, por el período caliente (§6) | Auditoría, depuración |

Ajustes que surgen de la Fase 0 (§9.2 D):

- **`uptime_seconds`** (host y VM) cambia en todos los reportes: se guarda la **hora de arranque**
  (`boot_time = timestamp − uptime`), que solo cambia al reiniciar; el uptime se calcula al leer.
- **`network_health.last_check`** repite la hora del reporte: no se guarda aparte.
- **`physical_layer.storage_layer.error`** cambia en el 88 % de los reportes que lo traen porque el
  agente manda el texto entero de la excepción de Redfish (H07 y H37 al 2026-10-01). Va a
  `sensores` / inventario como estado + motivo corto; conviene que el agente lo normalice.
- Las métricas tipadas son unas **35 rutas, ~73 valores por reporte**: CPU/RAM de host y VM,
  ventiladores, temperaturas, potencia, latencia y uso de red, y los signos vitales de los servicios
  (handles, threads, RAM, CPU). Dimensión real: 2,1 VMs por reporte (máx. 4), 5,5 discos y 3,4
  servicios por VM.

### 4.2 Software (reemplaza a la tabla genérica `software_monitoring`)

Hoy todo convive en una tabla con `app_name` + `component_id` + `extra_data` JSON. Se propone
tipar lo que tiene volumen y dejar genérico lo que no:

| Tabla | Hoy | Qué cambia |
|---|---|---|
| `mirth_canal_metricas` | `app_name='mirth'` | Columnas propias (recibidos, enviados, errores, encolados, estado) y el canal por su `channel_id` |
| `cola_dicom_metricas` | `app_name='dicom_routing'` | Columnas propias (regla, pendientes) |
| `portal_estado_metricas` | `app_name='patient_portal'` | Columnas propias (origen, código, total, últimas 24 h, sin ISO, más antiguo) |
| `software_eventos` | logs de Suitestensa, SSL, CHECKDB, backups | Se queda genérica: poco volumen, formatos variados |

### 4.3 KPIs de uso (reemplaza a `reportes_uso`)

`kpi_ris`, `kpi_pacs`, `kpi_usuarios` con columnas (equipo, modalidad, totales, admitidos,
ejecutados…), una fila por equipo y hora. Más un agregado diario y mensual por hospital: el resumen
de red y el del hospital pasan a sumar 12 filas por año en lugar de todo el histórico.

### 4.4 Agregados precalculados (TimescaleDB continuous aggregates)

| Agregado | Granularidad | Para |
|---|---|---|
| `infra_1h`, `infra_1d` | Promedio, máximo y mínimo por hospital (host y VM) | Gráficos de 7 y 30 días, PDF |
| `mirth_1h` | Mensajes por canal y hora | Pestaña Software en 7 días, mapa acumulado de 24 h / 7 días |
| `cola_dicom_1h`, `portal_1h` | Máximo y último por hora | Pestaña Software en 7 días |
| `kpi_1d`, `kpi_1m` | Totales por hospital | Resumen de red y por hospital, PDF de KPIs |

Regla de lectura: rangos de hasta 24 h van al crudo de 5 minutos; 7 días al agregado horario; 30
días o más al diario. Los gráficos dejan de submuestrear en Python.

### 4.5 Lo que no cambia de forma

`alertas`, `configuracion`, usuarios y accesos, exclusiones, Mirth (topología, nodos, curación),
`monitoreo_modulos`, `dicom_regla_baseline`, `historial_reportes`, `log_dictionary`: son chicas y su
forma está bien. Se migran tal cual (con tipos de fecha y claves foráneas reales, que SQLite no
hacía cumplir).

## 5. Estimación de almacenamiento (medida en la Fase 0, 2026-10-01)

Base: 77 hospitales, ~18.500 reportes/día (~108 MB/día de JSON), 2,1 VMs y ~12 discos por reporte.
Compresión del crudo **medida** sobre un día real; la columnar de TimescaleDB sigue siendo un
supuesto (10x) hasta probarla en la Fase 3.

| Dato | Hoy | Con el rediseño, sin comprimir | Comprimido |
|---|---|---|---|
| Métricas de infraestructura | ~108 MB/día (JSON) | ~23 MB/día (1,28 KB por reporte) | **~0,8 GB/año** (columnar 10x, supuesto) |
| Inventario | (dentro del JSON) | KB/día (solo cambios) | Despreciable |
| `software_monitoring` | ~4,7 MB/día de `extra_data` (~28 mil filas/día) | ~2 MB/día | **~0,2 MB/día** |
| JSON crudo caliente (30 días en la base) | — | ~3,2 GB | **~0,3 GB** (zstd por fila con diccionario, 11,6x medido) |
| JSON crudo archivado (fuera de la base) | — | — | **~0,5 GB/año** (zstd-19 por hospital y día, 79,6x medido) |

Resultado: **de ~45 GB/año (ritmo actual) a ~1 GB/año en la base, más ~0,5 GB/año de archivo**,
sin resumen con pérdida. Todo escala lineal con la cantidad de hospitales. El histórico completo
(16,5 GB de JSON desde 2026-04) archivado ocuparía ~0,2 GB; la foto entera de la base comprimida con
zstd -10 pesa 580 MB (2,84 %).

### 5.1 Medido en Postgres 16 + TimescaleDB (2026-10-01, Fase 3)

Una semana real de la foto (22 al 29 de septiembre: 132.007 reportes de 77 hospitales) cargada
con `herramientas/migracion_pg/cargar_infra.py` sobre `postgres/esquema.sql`, en la PC (Docker,
`timescale/timescaledb:2.17.2-pg16`). Carga: 3,7 min por semana (~1,5 h los 6 meses).

| Tabla | Filas/semana | Sin comprimir | Comprimido | Tasa | Por año |
|---|---|---|---|---|---|
| `metricas_host` | 132 mil | 22 MB | 2,3 MB | 10x | 0,12 GB |
| `metricas_sensor` | 920 mil | 85 MB | 5,7 MB | 15x | 0,30 GB |
| `metricas_vm` | 299 mil | 56 MB | 4,1 MB | 14x | 0,21 GB |
| `metricas_disco` | 1,5 M | 234 MB | 9,5 MB | 24x | 0,50 GB |
| `metricas_servicio` | 926 mil | 168 MB | 7,5 MB | 22x | 0,39 GB |
| `recoleccion` | 99 mil | 51 MB | 0,8 MB | 70x | 0,04 GB |
| `inventario` (versiones) | 3.826 | 8,9 MB | (TOAST) | — | 0,46 GB |
| `reporte_crudo` (30 días) | 132 mil | 401 MB | 48 MB | 8,4x | **~0,2 GB fijos** |

**Resultado: ~2 GB/año en la base** (métricas 1,55 + inventario 0,46) más ~0,2 GB fijos de crudo
caliente, contra ~45 GB/año de hoy: unas 20 veces menos. Es el doble del estimado de §5 porque
hay más filas de discos, sensores y servicios de lo supuesto. Margen de mejora, si hace falta:
- **Inventario (0,46 GB/año):** el 3 % de los reportes cambia el inventario, casi todo en 3
  hospitales donde la recolección oscila (H37 iDRAC, H07 una VM): el reporte llega sin discos o
  sin controladoras y el inventario alterna entre completo y vacío. Conservar la última versión
  conocida de esas partes cuando la recolección falla lo bajaría a casi nada.
- **Servicios (0,39 GB/año):** `handles` e hilos son lo que más cambia y lo que menos se mira.

**Software y KPIs (bloque 2 del esquema)**, cargados del 22/09 al 01/10 (9,4 días):

| Tabla | Filas | Sin comprimir | Comprimido | Tasa | Por año |
|---|---|---|---|---|---|
| `mirth_canal_metricas` | 116 mil | 32 MB | 1,2 MB | 30x | ~45 MB |
| `cola_dicom_metricas` (+ `dicom_reglas`) | 108 mil | 13 MB | 0,9 MB | 16x | ~35 MB |
| `software_eventos` (SSL, Elastic) | 90 mil | 39 MB | 1,0 MB | 37x | ~40 MB |
| `portal_estado_metricas` (1 hospital, 1,5 días) | 2.316 | 0,5 MB | 0,2 MB | 3x (muestra chica) | ~50 MB por hospital con portal |
| `sql_eventos`, `kpi_*` (tablas comunes) | — | 1,4 MB | — | — | ~50 MB |

Software + KPIs: **~0,2 GB/año** (hoy `software_monitoring` crece ~1,2 GB/año). Total con
infraestructura: **~2,2 GB/año en la base**.

**Agregados continuos (bloque 3 del esquema):** 9 agregados (host, VM y sensores por hora y por
día; cola DICOM, Mirth y portal por hora), comprimidos: 6,5 MB en 9,4 días, **~0,25 GB/año**.
Validados contra la foto: 105.385 promedios horarios y diarios de host y VM en 76 hospitales,
idénticos a calcularlos desde SQLite. Lecturas (en la PC): gráfico de 9 días de un hospital 9–54
ms (SQLite hoy 80–106 ms; 30 días ~1 s); último reporte de todos los hospitales para el motor de
alertas 17 ms (SQLite 330 ms). **Total en la base con agregados: ~2,5 GB/año.**

**Decidido (2026-10-01): VM sin dato = hueco, marcado con el motivo.** La serie lleva CPU/RAM en
`None` (no 0) más el estado y el motivo que informa el agente, en los dos motores. El gráfico
colorea el tramo: rojo "DESCONECTADA" (`Offline` por `port_closed`, `wmi_timeout` o sin motivo:
la VM no responde), ámbar "SIN LECTURA (WMI)" (`wmi_error`: responde pero falló la lectura) y
gris "SIN DATOS" (no vino en el reporte), con el total de cada uno debajo del título.

## 6. Auditoría y retención por niveles

| Nivel | Qué | Dónde | Por defecto |
|---|---|---|---|
| Caliente | Crudo + métricas sin comprimir | Postgres | 7 días |
| Tibio | Métricas comprimidas por columnas; crudo con compresión nativa | Postgres | Hasta el fin del período caliente del crudo (30 días) |
| Frío | Crudo archivado: un archivo comprimido por hospital y día, con su hash | Disco o almacenamiento de objetos, fuera de la base | Según la política de auditoría (decisión 1) |
| Métricas y agregados | Siempre en la base, comprimidos | Postgres | Sin vencimiento (son livianos) |

- Todo es **sin pérdida**: lo que sale de la base no se resume, se archiva tal cual. Un archivo
  frío se puede volver a cargar si una auditoría lo pide.
- Los períodos se editan desde la UI (Admin), con **piso** que la UI no permite bajar, vista
  previa del efecto, doble confirmación y registro de quién cambió qué (como estaba en la v1).
- Si la auditoría exige **inmutabilidad**, el nivel frío se guarda con el hash de cada archivo en
  la base y en un almacenamiento que no permite reescribir (decisión 1).
- **`maintenance.py` se retira** con la migración, y conviene **pausarlo antes** (decisión 2).

## 7. Mejoras de performance esperadas

| Lectura | Hoy | Después |
|---|---|---|
| Motor de alertas (último reporte por hospital) | `MAX … GROUP BY` sobre 2,6 M filas + JSON, cada 60 s | Lee `estado_actual_hospital`: 57 filas |
| Gráfico de 30 días | 15.000 JSON parseados en Python | ~720 filas del agregado diario/horario, ya tipadas |
| PDF de infraestructura | Todas las filas del rango en memoria | Agregados del rango + inventario |
| Resumen de red / por hospital | Suma todo `reportes_uso` en Python | Suma el agregado mensual |
| Pestaña Software en 7 días | Filas de 5 minutos con JSON | Agregado horario tipado |
| Ingesta | Sesión síncrona dentro de un endpoint `async` | Escritura en lote con pool de conexiones; el inventario solo se escribe si cambió |

Además Postgres permite varios escritores a la vez (SQLite bloquea la base entera al escribir), y
el `VACUUM` deja de requerir el server detenido.

## 8. Infraestructura

- **PostgreSQL 16 + TimescaleDB** (edición comunitaria, autohospedada: la compresión y los
  agregados continuos están incluidos sin costo). Alternativa sin extensión: particiones nativas
  por mes + agregados con vistas materializadas refrescadas por tarea, y archivado de particiones
  viejas; comprime bastante menos y es más trabajo de mantener.
- Dimensionamiento inicial estimado (a confirmar con la Fase 0): 4 vCPU, 8–16 GB de RAM, 100 GB
  de disco SSD para los primeros años (el histórico actual migrado + margen).
- **Backups** con recuperación a un punto en el tiempo (pgBackRest o equivalente) y una prueba de
  restauración antes del corte. Roles separados: aplicación, solo lectura (informes), migración.
- Migraciones de esquema con Alembic (hoy es `create_all` + scripts manuales en `Accesorios/`).
- Monitoreo del propio Postgres (tamaño, compresión, consultas lentas) desde el día uno.

## 9. Plan por fases

| Fase | Objetivo | Entregable / criterio de salida | Tamaño |
|---|---|---|---|
| **0. Medir y decidir** | Validar las hipótesis con datos reales | Ver detalle abajo | S |
| **1. Infra Postgres** | Staging y producción | Instalado, endurecido, backups probados con restauración, monitoreo | M |
| **2. Capa de acceso a datos** | Que el código no dependa del motor ni del JSON | Todas las lecturas de series, último estado e inventario pasan por funciones únicas (al 2026-09-30 hay 36 `json.loads` y 27 consultas SQL crudas con `text()` dispersos, y los PDF de infraestructura duplicados; en la v1 eran 29 y 18: crecen con cada módulo nuevo). Se puede hacer **antes** de migrar, sobre SQLite, y achica el riesgo del corte | L |
| **3. Esquema nuevo** | Tablas de §4, compresión, agregados, políticas | Esquema con Alembic en staging; ingesta que escribe inventario + métricas + crudo; pruebas con reportes reales grabados | M |
| **4. Carga histórica fuera del server** | Pasar el histórico sin cargar ni duplicar disco en producción | Ver §9.1. Desde una foto (`.backup`) en una PC: transformación completa y verificación local; se sube un `pg_dump` comprimido y el archivo frío. Incluye los `historico_*.db` | M |
| **5. Ensayo del diferencial** | Saber cuánto dura el corte | En el server, el diferencial desde la foto contra un esquema de prueba: tiempo y verificación. Sin sincronización continua ni doble escritura (decisión 6) | S |
| **6. Corte con la ingesta parada** | Cambiar de motor | Ingesta y motor de alertas detenidos → diferencial una vez → verificación de conteos → ingesta y lecturas a Postgres → arranque. Lo que manden los agentes en esa ventana se pierde (no reenvían; salvo CHECKDB). Vuelta atrás: arrancar sobre SQLite, que quedó tal cual | S |
| **7. Políticas y UI** | Retención por niveles configurable | Pantalla Admin con piso, vista previa, doble confirmación y registro | M |
| **8. Retiro de SQLite** | Limpiar | Backup final de solo lectura; se apagan `maintenance.py` y los scripts de export/borrado | S |

**Fase 0 en detalle** (solo lectura sobre una copia de producción):

1. Composición del JSON: cuánto pesa cada bloque (`physical_layer`, `virtual_layer`,
   `collection_meta`…) y **qué porcentaje es idéntico al reporte anterior** del mismo hospital.
   Valida el principio 1 y la tabla de §5.
2. Cantidad real de VMs, discos y sensores por hospital (dimensiona `metricas_vm` / `_disco`).
3. Compresión real: zstd sobre un día de JSON de 5 hospitales (nivel frío) y TimescaleDB sobre una
   muestra de métricas tipadas (nivel tibio).
4. Cuántas filas ya están resumidas con pérdida por `maintenance.py` (marca `_is_compressed`) y
   desde cuándo: define qué parte del histórico es recuperable.
5. Inventario y estado de los `historico_*.db`. **(2026-10-01: no están en la carpeta del server; falta saber dónde quedaron los meses exportados antes de 2026-04.)**
6. Tiempos actuales de las lecturas de §2.4, para tener contra qué comparar.
7. Espacio y RAM disponibles donde iría Postgres.

Orden: 0 → (1 y 2 en paralelo) → 3 → 4 → 5 → 6 → 7 → 8. La 2 aporta aunque la migración se demore.

### 9.2 Resultados de la Fase 0 (2026-10-01)

Script `herramientas/migracion_pg/fase0_medicion.py` (solo lectura) sobre una foto de producción
del 2026-10-01 10:31 hecha con `VACUUM INTO` (`quick_check` ok). Corrió en 7 min en la PC de
desarrollo. El informe completo queda fuera del repo, junto a la foto (contiene IDs de hospitales).
Ítems 1–4 y 6 de la lista de arriba: hechos. Pendientes: 5 (`historico_*.db`) y 7 (dónde va
Postgres).

**A. Tablas.** `reportes_historicos` 17,75 GB (**93,4 %**; 2.895.302 filas desde 2026-04-01);
`software_monitoring` 628 MB (2,2 M filas); índices ~0,7 GB; `reportes_uso` 45 MB; el resto, KB.
Sin páginas libres dentro del archivo.

**B. Por mes** (`reportes_historicos`): de 2,21 GB de JSON en abril (62 hospitales) a **3,17 GB en
septiembre (77)**; promedio 5,96 KB por reporte. Filas resumidas con pérdida por `maintenance.py`:
0,3–0,7 % por mes (cada una reemplazó un bloque de 30 min): **casi todo el histórico está completo**.

**C. Composición** (un día, 84 MB): `virtual_layer` 56 %, `physical_layer` 36 % (almacenamiento
físico 13,5 %, sensores 9 %, discos 7,4 %), `collection_meta` 4 %, `envelope` 2 %.

**D. Repetición:** 89,5 % de los valores y 88,9 % de los bytes iguales al reporte anterior; lo que
cambia ronda 1,8 KB por reporte. Ver ajustes al modelo en §4.1.

**E. Compresión del crudo** (un día, 92 MB):

| Método | Tasa |
|---|---|
| zlib-6 / zstd-3, cada reporte por separado | 4,5–4,7x |
| zstd-3 con diccionario, cada reporte | **11,6x** |
| zlib-9 / lzma, archivo por hospital y día | 47x / 72x |
| zstd-19, archivo por hospital y día | **79,6x** (26 s por día de datos) |

**F. Modelo nuevo:** ver §5.

**G. `software_monitoring`** (últimos 30 días, filas/día): Mirth 10.140, autoenrute DICOM 9.012,
Elasticsearch 7.619, SSL 1.232, portal 77, backups SQL 15, CHECKDB 2. ~4,7 MB/día de `extra_data`.

**H. Lecturas calientes** (en la PC, disco local; en el server serán más lentas): último reporte por
hospital para el motor de alertas 264 ms; **gráfico de 30 días de un hospital 978 ms**; resumen de
`reportes_uso` 15 ms. Son la línea de base para comparar después de la Fase 3.

**Lecciones de la foto:** el `.backup` del cliente `sqlite3` **no termina** con la ingesta andando
(se reinicia con cada escritura; estuvo 10 h sin avanzar). Para fotos en caliente usar
`VACUUM INTO` (5,5 min, no bloquea la ingesta) y verificar con `PRAGMA quick_check` antes de
comprimir. Pico de disco durante la foto: ~20 GB.

### 9.0 Fase 2 en curso: capa de acceso `dashboard_app/datos/`

Paquete único para leer el histórico (reglas en `datos/__init__.py`). **Fase 2 terminada (2026-10-01):** ninguna lectura de `reportes_historicos`, `reportes_uso` ni `software_monitoring` queda fuera de `datos/`; solo la ingesta (`main.py`) las toca para escribir y evitar duplicados, y eso se rehace en la Fase 3. Pruebas en `tests/`
(`python3 -m pytest tests`, base SQLite en memoria). Se migra por bloques, cada uno verificado
contra la foto de producción:

| Bloque | Qué | Estado |
|---|---|---|
| 1. Último reporte | `ultimo_reporte`, `ultimos_reportes`, `ultimo_timestamp`, `valores_recientes`; parser único de fechas (`datos.tiempo`). 8 lugares: motor de alertas, OFFLINE, Mirth, autoenrute, detalle del hospital, resumen de red y mapa, resumen del hospital, mapa de Mirth | **Hecho (2026-10-01)** |
| 2. Series de infraestructura | `serie_infra` (métricas ya extraídas: CPU/RAM de host y VM, temperaturas, red) y `ultimo_reporte(hasta=)`. Gráfico `/history` y PDF de infraestructura con su gráfico de temperaturas; se borró la copia muerta del PDF en `routers/informes.py`. Salida idéntica a la anterior sobre la foto (15 series y 3 PDF) | **Hecho (2026-10-01)** |
| 3. KPIs de uso (`reportes_uso`) | `datos.uso`: `reportes_uso` (por inserción) y `reportes_uso_por_evento` (por `start_time_extraction`, con el margen de 3 días en un solo lugar); `fecha_evento` única (había 4 copias). Resumen de red y del hospital, `kpi-history`, PDF clínico y KPIs programados: salida idéntica sobre la foto, salvo el PDF clínico (ver abajo) | **Hecho (2026-10-01)** |
| 4. Software (`software_monitoring`) | `datos.software`: `ultimas_lecturas` (n por componente, por hora o por orden de inserción), `lecturas` (desde una hora, con tope) y `ultima_foto` (portal). `extra_data` llega siempre como dict y la hora como datetime. Detectores (Mirth, autoenrute y su piso habitual, CHECKDB, backups, portal), pestaña Software, mapa, acumulado y topología de Mirth. Salida idéntica sobre la foto: 224 decisiones de detectores (mismo orden), 41 pisos, 28 respuestas de la pestaña Software y los endpoints de Mirth | **Hecho (2026-10-01)** |

**Corrección de paso en el PDF clínico (bloque 3):** el nombre de equipo de cada AET del PACS se
armaba mientras se recorrían los reportes, así que el resultado dependía del orden en que SQLite
devolvía las filas (no estaba fijado). Un mismo equipo salía partido en dos, con su AET y con su
nombre (H03, septiembre: `CTRIVA01` 270 + `TOMO CANON AQUILION LIGHTNING` 195 → ahora 465), y con
páginas de "evolución combinada" de más. Ahora el diccionario se arma completo antes de sumar; los
totales no cambian (H03: 2.180 estudios antes y después) y el orden de las páginas por equipo
pasa a ser fijo (cronológico).

### 9.3 Camino al corte: lo que falta (análisis del 2026-10-01)

**Hecho:** Fase 0 (mediciones), Fase 2 (capa `datos/`, en el repo, sin desplegar), y de la Fase 3
el esquema (infraestructura, software, KPIs, agregados), la transformación y los cargadores,
medidos y validados en la PC contra la foto. Base estimada: ~2,5 GB/año.

#### A. Código, en la PC (no toca producción) — **hecho el 2026-10-01**

Estado: A1 (`85ce911`), A2 (`e163bf0`), A3 y la parte de configuración de A4 (`85ce911`), A5
(`93d6511`). **Alembic (resto de A4) queda para después del corte**: para instalar alcanza
`instalar_esquema.py`; Alembic sirve para los cambios de esquema que vengan. Verificación:
- A1: las 21 funciones de `datos/` dan idéntico leyendo la foto en SQLite y cargada en Postgres
  (82 hospitales), y de punta a punta los detectores (224 decisiones), la pestaña Software, Mirth,
  KPIs, PDF clínico y de infraestructura y el gráfico de 24 h / 7 días.
- A2: 3 h de ingesta real reproducidas contra el endpoint (`reproducir_ingesta.py`): en SQLite
  queda igual que en producción; SQLite y Postgres reproducidos dan idéntico en `datos/`. Pruebas
  de reloj corrido, reporte atrasado, inventario y deduplicación en `tests/test_escritura_pg.py`.
- A5: corte simulado (foto de las 07:00 contra la de las 10:31): diferencial en 16 s, verificación
  OK; volver a correrlo no duplica nada.


| # | Qué | Por qué hace falta | Tamaño |
|---|---|---|---|
| A1 | **`datos/` leyendo de Postgres**: las mismas funciones de `infra`, `uso` y `software` con un segundo motor, elegido por configuración. Verificación igual que en la Fase 2: misma salida desde SQLite y desde Postgres sobre la foto | Es lo que conecta el esquema con el panel, los PDF y el motor de alertas | L |
| A2 | **Ingesta escribiendo en Postgres** (`main.py`): `transformar` → métricas, inventario (solo si cambia), estado actual, crudo y software; las deduplicaciones de CHECKDB/backups/portal; `collection_meta` a módulos dados de baja. **Guardar la hora de recepción del server** y usarla si la del agente difiere más de ~10 min (reloj de H03) | Sin esto, después del corte no entra nada | M |
| A3 | **Tablas chicas** (alertas, configuración, usuarios, hospitales, Mirth, módulos, exclusiones, baselines…): se crean con los modelos de hoy (`create_all` funciona igual); copia completa en el corte. Siguen con hora local sin zona, como hoy: el código las compara con `datetime.now()` y cambiarlas no aporta. Revisado: fuera de `datos/` el código usa solo el ORM, sin SQL propio de SQLite | Son la configuración y el estado de las alertas | S |
| A4 | **Esquema con Alembic** (`postgres/esquema.sql` + tablas chicas) y motor por configuración (`DATABASE_URL`) | Instalación repetible en el server y vuelta atrás por configuración | S |
| A5 | **Cargador del corte**: diferencial por `id` desde la foto (ya reanudable) + tablas chicas completas + refresco de agregados + **verificación** (reportes por hospital y día iguales en los dos motores) | Es el guion del día del corte | S |
| A6 | ~~Decidir el criterio de VMs sin dato~~ **Resuelto (2026-10-01): hueco marcado con el motivo** (§5.1) | Cambia lo que se ve en el gráfico | Hecho |

#### B. En producción, antes del corte (sin detener el monitoreo)

| # | Qué | Quién | Tiempo |
|---|---|---|---|
| B1 | **Desplegar la Fase 2** (sigue en SQLite) y dejarla andar **al menos una semana**: valida `datos/` en producción antes de cambiar de motor. **Programado: 2026-10-02 a la tarde; plazo acortado (ver «Orden y plazo»): un par de horas de observación en vez de una semana.** Va todo el código hasta acá; sin `DATABASE_URL` sigue en SQLite | Usuario (`git pull` + reinicio) | 15 min + 1 semana |
| B2 | **Fase 1 en el server**: swap de 4 GB; PostgreSQL 16 + **TimescaleDB 2.17.2** (la misma versión que la PC, si no el dump no restaura); `timescaledb-tune` con poca memoria (~1,5 GB); roles (app, solo lectura, migración); backup diario (`pg_dump` comprimido) con una restauración de prueba | Usuario con comandos preparados | 1–2 h |
| B3 | **Carga histórica en la PC** desde una foto nueva (`VACUUM INTO`, como el 01/10): 6 meses de infraestructura ~1,5 h + software y KPIs minutos + archivo frío del crudo; verificación; `pg_dump` | PC | ~3 h |
| B4 | **Subir y restaurar** el dump en el server (procedimiento de TimescaleDB: `timescaledb_pre_restore` / `post_restore`) y **ensayo del diferencial** (Fase 5) contra esa copia: mide cuánto dura el corte de verdad | Usuario + guion | 1 h |
| B5 | ~~`historico_*.db`~~ **Resuelto (2026-10-01): se descartan.** El histórico en Postgres arranca el 2026-04-01, igual que hoy en SQLite | — | — |

#### C. El día del corte (monitoreo detenido)

Para que el diferencial sea chico, **B3 y B4 se hacen con una foto de 1 o 2 días antes del corte**.

Comandos (en `tecnomonitor-server/`, con la foto ya cargada y marcada con `corte.py marcar`):

```bash
# app detenida
python3 herramientas/migracion_pg/corte.py diferencial --sqlite monitor_hospitales.db --dsn "$DATABASE_URL_PG"
# termina con "VERIFICACIÓN OK" (código 0); si se corta, se vuelve a correr: no duplica
# .env: DATABASE_URL=<dsn de Postgres>  ->  arrancar
```

1. Detener la app (ingesta, motor de alertas y panel). Desde acá lo que manden los agentes se pierde
   (no reenvían; decisión 6).
2. Diferencial: reportes, software y KPIs con `id` mayor al de la foto; tablas chicas completas.
   Medido en la PC: 3,5 h de diferencia (2.603 reportes, 5.824 lecturas) en 16 s con la
   verificación. Con 1 día (~19 mil reportes) son ~1–2 min en la PC; **en el server, calcular
   ~5 min** (el número firme sale del ensayo B4).
3. Refresco de agregados (solo desde lo que entró) y verificación por hospital y día: incluidos
   en el paso anterior.
4. Cambiar `DATABASE_URL` a Postgres y arrancar **primero la ingesta**; el motor de alertas recién
   cuando todos los hospitales hayan reportado (~10 min), para no disparar OFFLINE en falso.
5. Controles: panel, detalle de 3–4 hospitales, un PDF, tick del motor sin errores, Asana.

**Ventana estimada: 30–45 min** de monitoreo detenido (con margen: diferencial y verificación
~5 min, arranque escalonado ~10 min, controles ~10 min); el número firme sale del ensayo B4. **Vuelta atrás**: volver `DATABASE_URL` a SQLite y arrancar (SQLite queda intacto); se
pierde lo que haya entrado a Postgres después del corte. Conviene elegir el horario de menos
actividad en los hospitales y avisar a quien mire el panel.

#### D. Después del corte

- Fase 7: retención por niveles desde la UI (crudo 30 días → archivo frío) y quitar el resumen de
  `maintenance.py`. No apura: el crudo comprimido son ~0,2 GB al mes.
- Acceso directo a la base: cliente en la PC (DBeaver o pgAdmin) por túnel SSH con el rol de solo
  lectura; Postgres sigue escuchando solo en `127.0.0.1`, sin puertos nuevos ni interfaz web pública.
- Fase 8: backup final de solo lectura del `.db` de SQLite y borrarlo (libera ~21 GB).

#### Orden y plazo

A1–A5 (código, ~3–5 sesiones de trabajo) en paralelo con B1 (despliegue de la Fase 2 y una
semana andando) y B2 (instalar Postgres) → B3 + B4 con una foto fresca → corte (C). **El corte se
puede hacer, como pronto, en 2 a 3 semanas.** Límite de disco (§2.1): el libre llega a ~15 GB en
~4 meses, así que hay margen para no apurarlo.

**Cambio de plazo (2026-10-02):** se comprime todo a un día. B1 a la tarde y ~2 h de observación;
en paralelo B2 en el server; foto nueva apenas B1 esté arriba (resumen apagado) → B3 en la PC
(~3 h) → B4 (subir, restaurar, ensayo) → corte esa noche o al día siguiente sobre la misma foto
(el diferencial de un día es ~5 min en el server). Lo que se resigna es la semana de `datos/` en
producción antes de cambiar de motor; queda cubierto por la paridad verificada (A1, A2) y por la
vuelta atrás a SQLite.

Pendientes operativos fuera de la migración: reloj del agente de H03 (+4 h), encabezados
`X-Forwarded-For` en `location /ws/` de Nginx, revisar si el 8100 (`/hl7/`) escucha en `0.0.0.0`.

### 9.1 Carga histórica: local + diferencial (camino elegido, 2026-09-30)

1. **Foto:** `.backup` de la base de producción (seguro con el server andando). Se anota el último
   `id` de cada tabla grande.
2. **Migración en una PC** (la de desarrollo tiene 172 GB libres, 8 núcleos, 31 GB de RAM):
   Postgres + TimescaleDB local, transformación completa (inventario, métricas, agregados, archivo
   frío) y verificación contra la foto. Se puede repetir las veces que haga falta sin tocar
   producción.
3. **Subida:** `pg_dump` comprimido (pocos GB) + archivo frío, restaurado en el Postgres del server.
   **Misma versión de Postgres y de TimescaleDB en los dos lados**; TimescaleDB pide su procedimiento
   de restauración (pre/post restore).
4. **Diferencial en el server**, con el mismo código de transformación:
   - Tablas que solo agregan filas (`reportes_historicos`, `reportes_uso`, casi todo
     `software_monitoring`): las filas con `id` mayor al de la foto.
   - Filas que se modifican en el lugar (`alertas`, las filas `sql_backup` que renuevan
     `last_seen`, topología y curación de Mirth, configuración, usuarios, `monitoreo_modulos`,
     `dicom_regla_baseline`): tablas chicas, se copian enteras en cada pasada.
5. **Ensayo** (Fase 5): el diferencial en el server contra un esquema de prueba, para medir cuánto
   tarda.
6. **Corte** (Fase 6), con la ingesta parada (decisión 6): diferencial una sola vez, verificación,
   ingesta y lecturas a Postgres. Vuelta atrás: arrancar sobre SQLite, que quedó tal cual al parar.
   El diferencial crece ~16 mil reportes por día desde la foto: si el corte se demora semanas, sacar
   una foto nueva y repetir la migración local (ya probada).

Condición: **la pausa de `maintenance.py` (decisión 2) desplegada antes o al momento de la foto.**
Si el resumen corre después, reescribe en el server filas viejas que en la foto están completas: no
se pierde nada (la foto es la versión buena y el diferencial solo mira `id` nuevos), pero la
comparación del corte daría diferencias en esos meses.

En el server nunca se duplica el histórico: SQLite queda como está (~20,5 GB) y Postgres suma pocos
GB hasta el corte, cuando se borra el `.db`.

#### Alternativa: mes a mes en el server

Solo si no se pudiera migrar fuera del server. Solo para `reportes_historicos` (el 93 % de la base). `reportes_uso` (40 MB; el resumen de red suma
todo su histórico) y `software_monitoring` (0,5 GB; se lee hasta 7 días) pasan enteras en el corte.

Ciclo por mes, empezando por los `historico_*.db` ya exportados y después por el mes más viejo de
la base (2026-04):

1. **Archivo frío:** JSON crudo del mes, un archivo zstd por hospital y día, con su hash. Es el nivel
   frío de §6: queda como segunda copia antes de borrar nada.
2. **Carga en Postgres:** inventario + métricas + agregados, idempotente (se puede repetir).
3. **Verificación:** reportes por hospital y día iguales en SQLite, Postgres y archivo; hash de cada
   JSON contra el archivo; valores de una muestra (CPU, RAM, discos) contra el JSON original.
4. **Borrado del mes en SQLite**, solo si 3 dio bien: por lotes de un día, en horario de poco uso
   (un borrado grande bloquea la base y la ingesta espera hasta 15 s).

**Qué gana y qué no:** SQLite no achica el archivo al borrar (solo `VACUUM`, que no es opción). El
borrado **frena el crecimiento** (cada mes liberado, ~3 GB, absorbe unas tres semanas de reportes
nuevos) y el pico de disco queda en SQLite (~20,5 GB fijo) + Postgres (pocos GB) + archivo (1–2 GB),
que entra en los 35 GB libres. El espacio se recupera de una vez al borrar el `.db` en la Fase 8.

**Restricción mientras dura:** solo se migran meses de más de 31 días (el gráfico de
infraestructura lee hasta 30). El **PDF de infraestructura con rango libre** saldría vacío para un
mes ya migrado: limitarlo a los meses que siguen en SQLite o hacer que ese PDF lea de Postgres para
los meses viejos (decisión 10).

## 10. Riesgos

- **Reconstruir el JSON** a partir de inventario + métricas puede no ser exacto si el agente manda
  campos que el esquema no contempla. Por eso el crudo se conserva (caliente y archivado) y no se
  descarta en favor de lo normalizado.
- **Campos nuevos del agente:** cada versión que agregue una métrica requiere una columna. El crudo
  la conserva igual hasta que se agrega; el contrato de ingesta (docs/10) pasa a ser también
  contrato de esquema.
- **Horas sin zona:** los agentes mandan hora local sin zona y hoy se guarda así. En Postgres hay
  que decidir (decisión 7); mezclar criterios rompe gráficos y alertas.
- **TimescaleDB** es infraestructura nueva y los Postgres gestionados de algunos proveedores no la
  permiten (§8).
- **OFFLINE masivo** si la ingesta se corta más de `offline_minutes` durante el corte.
- **Sin suite de tests en el server:** la Fase 2 debería traer al menos pruebas de las funciones de
  lectura contra los dos motores.
- Lo ya resumido por `maintenance.py` no se recupera.

## 11. Decisiones abiertas

1. ~~**Auditoría:** cuánto tiempo hay que conservar el crudo~~ **Resuelto (2026-10-01): el crudo
   archivado se conserva por tiempo indefinido** (~0,5 GB/año, §5); el período caliente en la base
   sigue en 30 días. Queda por definir solo si hace falta **inmutabilidad** (almacenamiento que no
   permite reescribir) y si el archivo incluye `software_monitoring` y KPIs; no bloquea las Fases
   2 y 3.
2. ~~**¿Pausar `maintenance.py` ya?**~~ **Resuelto (2026-09-30): pausado.** El resumen con
   pérdida solo corre si se prende *Configuración → Almacenamiento* (clave
   `mantenimiento_resumen_enabled`, apagada por defecto; `maintenance.resumen_habilitado()`). Al
   desplegarlo deja de correr solo. Costo: la base crece ~3 GB/mes hasta la migración; vigilar el
   disco del server.
3. ~~**Crudo: ¿cuánto en la base y cuánto archivado?**~~ **Resuelto (2026-10-01, con la Fase 0):
   30 días en la base**, comprimido por fila con diccionario (~0,3 GB en total), **y el resto en
   archivo frío** (zstd-19 por hospital y día, ~0,5 GB/año). Los dos costos son tan bajos que el
   período caliente se puede alargar si la auditoría (decisión 1) lo pide.
4. ~~**Dónde vive Postgres**~~ **Resuelto (2026-10-01): en el mismo server.** Con todo el
   histórico migrado ocupa pocos GB y el archivo frío ~0,2 GB: entra en el disco actual junto a
   SQLite hasta el corte, siempre que el corte llegue antes de que el libre baje de ~15 GB (~4
   meses, §2.1). **Server medido (2026-10-01): 4 vCPU, 7,4 GiB de RAM con ~2,6 GiB disponibles y
   sin swap.** Alcanza para Postgres con poca memoria (`shared_buffers` ~1 GB, `work_mem` chico:
   las lecturas van a agregados), pero **hay que agregar swap (4 GB) antes de instalarlo**: sin
   swap, un pico de memoria mata procesos (OOM) en lugar de ponerlos lentos.
5. ~~**TimescaleDB o Postgres puro**~~ **Resuelto (2026-10-01): PostgreSQL 16 + TimescaleDB**
   (edición comunitaria, §8).
6. ~~**Tolerancia a downtime** en el corte~~. **Resuelto (2026-09-30): se puede detener la
   ingesta** hasta tener el diferencial migrado. Por eso no hay sincronización continua ni doble
   escritura: el diferencial corre una vez con la ingesta parada. Se acepta el hueco de esa ventana.
7. ~~**Zona horaria**~~ **Resuelto (2026-10-01): `timestamptz` en UTC + zona por hospital** (hoy
   todos `America/Argentina/Buenos_Aires`). El histórico, que está en hora local sin zona, se
   convierte asumiendo −03:00 (Argentina no tiene horario de verano desde 2009). La ingesta
   interpreta la hora del agente con la zona de su hospital hasta que el agente mande la zona.
   **Ojo (medido 2026-10-01): la hora del agente no es confiable en todos los hospitales.** El
   reloj del equipo de H03 se viene corriendo: +1 h de abril a julio, +2 h en agosto y
   septiembre, +4 h el 01/10 (sus reportes llegan "del futuro"). El resto está dentro de ±8 min
   contra la hora del server (`software_monitoring.created_at`). La ingesta nueva tiene que
   guardar también la hora de recepción del server y no confiar ciegamente en la del agente.
8. ~~**¿Hacer la Fase 2 (capa de acceso) ya, sobre SQLite?**~~ **Resuelto (2026-10-01): sí, se
   arranca ya.**
9. ~~**Discos: ¿todas las muestras o solo cambios?**~~ **Resuelto (2026-10-01): todas las
   muestras** de las métricas que cambian, sin umbral. Ya están dentro del estimado de §5 (~0,8
   GB/año) y la compresión columnar absorbe los valores repetidos sin perder la serie exacta. El
   inventario de cada disco (punto de montaje, tamaño total) sí va solo cuando cambia.
10. *(Solo si se usa la alternativa mes a mes de §9.1.)* **PDF de infraestructura de meses ya migrados**: limitarlo a
    lo que sigue en SQLite, o que lea de Postgres para esos meses. Depende de cada cuánto se piden
    PDF de meses viejos.

## 12. Qué se conserva de la v1

La elección de motor (todo a Postgres, alternativa C), las alternativas descartadas (solo SQLite;
SQLite caliente + Postgres histórico; ClickHouse como tercera tecnología), la distinción entre
compresión sin pérdida, resumen y borrado, la UI de políticas con piso y doble confirmación, la
doble escritura y el corte por interruptores. Lo nuevo es el modelo de §3–§7.
