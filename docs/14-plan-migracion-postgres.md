# Plan — migración a PostgreSQL y rediseño del almacenamiento

**Estado — 2026-09-30: plan v2, NO ejecutado. Sin código.** La v1 (2026-09-18) decidió *a qué
motor* ir; esta versión agrega *cómo guardar los datos* una vez allá, con foco en performance y en
bajar el almacenamiento. Las mediciones de producción son las del 2026-09-18 (§2); todo lo que dice
"estimado" es hipótesis a validar en la Fase 0 (§9) antes de comprometer el diseño.

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
GB/mes, con el resumen de `maintenance.py` todavía andando). Disco del server: 85,3 GB, **35,1 GB
libres**. Con el resumen pausado (decisión 2) el archivo crece más rápido, porque SQLite no achica
el archivo al borrar y el resumen liberaba páginas que se reusaban: estimado **~7–8 GB/mes, unos 4
meses de margen**. Umbral propuesto: si el libre baja de ~15 GB, volver a prender el resumen
mientras tanto. Un `VACUUM` para recuperar espacio no es opción (server detenido y el doble de
disco).

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
temperaturas, uso de cada disco, latencia). **Estimado: más del 80 % de cada JSON es información
repetida del reporte anterior.** Guardarlo entero cada 5 minutos es lo que lleva a 6 KB por fila.

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

## 5. Estimación de almacenamiento (a validar en la Fase 0)

Supuestos: 57 hospitales, 1 reporte cada 5 minutos, 5 VMs y 10 discos promedio por hospital.

| Dato | Hoy | Con el rediseño, sin comprimir | Comprimido (columnar, ~10x) |
|---|---|---|---|
| Métricas de infraestructura | 98 MB/día (JSON) | ~20 MB/día | **~2 MB/día ≈ 0,7 GB/año** |
| Inventario | (dentro del JSON) | KB/día (solo cambios) | Despreciable |
| `software_monitoring` | ~3 MB/día, en crecimiento | ~2 MB/día | **~0,2 MB/día** |
| JSON crudo caliente (30 días en la base) | — | 98 MB/día | ~1 GB en total (compresión nativa ~3x) |
| JSON crudo archivado (fuera de la base) | — | — | **~2,5 GB/año** (zstd por hospital y día, ~15x) |

Resultado estimado: **de ~35 GB/año a ~1 GB/año en la base, más ~2,5 GB/año de archivo
comprimido**, con más información consultable que hoy y sin resumen con pérdida. Las tasas de
compresión son típicas para series de este tipo, **no están medidas sobre nuestros datos**: la
Fase 0 lo confirma con una muestra real antes de diseñar el esquema final.

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
| **4. Carga histórica** | Pasar el histórico | Transformación idempotente por hospital y mes: JSON → inventario + métricas + crudo/archivo. Verificación: conteos, y comparación de valores en una muestra contra el JSON original. Incluye los `historico_*.db` | M |
| **5. Doble escritura** | Validar en vivo | La ingesta escribe en los dos motores 1–2 semanas; comparación automática diaria; las lecturas siguen en SQLite | M |
| **6. Corte** | Cambiar de motor | Interruptor por área (gráficos, alertas, PDF…), motor de alertas último; OFFLINE pausado durante la ventana (los agentes no reenvían lo que se pierde). Vuelta atrás: el interruptor a SQLite, que siguió actualizado | S |
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
5. Inventario y estado de los `historico_*.db`.
6. Tiempos actuales de las lecturas de §2.4, para tener contra qué comparar.
7. Espacio y RAM disponibles donde iría Postgres.

Orden: 0 → (1 y 2 en paralelo) → 3 → 4 → 5 → 6 → 7 → 8. La 2 aporta aunque la migración se demore.

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

1. **Auditoría:** cuánto tiempo hay que conservar el crudo, si exige **inmutabilidad** y si incluye
   `software_monitoring` y KPIs. Define el nivel frío.
2. ~~**¿Pausar `maintenance.py` ya?**~~ **Resuelto (2026-09-30): pausado.** El resumen con
   pérdida solo corre si se prende *Configuración → Almacenamiento* (clave
   `mantenimiento_resumen_enabled`, apagada por defecto; `maintenance.resumen_habilitado()`). Al
   desplegarlo deja de correr solo. Costo: la base crece ~3 GB/mes hasta la migración; vigilar el
   disco del server.
3. **Crudo: ¿cuánto en la base y cuánto archivado?** Propuesta: 30 días en la base, el resto en
   archivo frío.
4. **Dónde vive Postgres** (mismo servidor o aparte) y quién lo opera. Con 35 GB libres (§2.1),
   durante la carga histórica y la doble escritura conviven SQLite (20+ GB y creciendo), Postgres y
   el archivo del crudo: en este disco queda justo. **Recomendado: ampliar el disco o poner Postgres
   en otra VM.**
5. **TimescaleDB o Postgres puro** (depende de la 4).
6. **Tolerancia a downtime** en el corte.
7. **Zona horaria:** guardar en UTC con la zona de cada hospital, o seguir en hora local sin zona.
8. **¿Hacer la Fase 2 (capa de acceso) ya, sobre SQLite?** Recomendado: es útil sola y baja el
   riesgo de todo lo demás.
9. **Discos: ¿todas las muestras o solo cambios?** El uso de disco cambia lento; guardar solo
   cuando varía más de X % bajaría filas, pero pierde la serie exacta (sería con pérdida, salvo que
   el crudo lo respalde).

## 12. Qué se conserva de la v1

La elección de motor (todo a Postgres, alternativa C), las alternativas descartadas (solo SQLite;
SQLite caliente + Postgres histórico; ClickHouse como tercera tecnología), la distinción entre
compresión sin pérdida, resumen y borrado, la UI de políticas con piso y doble confirmación, la
doble escritura y el corte por interruptores. Lo nuevo es el modelo de §3–§7.
