# Plan — migración a PostgreSQL con historial completo y compresión configurable

**Estado — 2026-09-18: diagramado, NO ejecutado.** Este documento es solo el análisis y el
plan; no se tocó la base, el código ni la infraestructura. Las decisiones abiertas (sección 7)
tienen que cerrarse antes de arrancar la Fase 1.

## 1. Por qué

`monitor_hospitales.db` (SQLite, un solo archivo) pesa 18,7 GB y crece sin techo. Objetivos
pedidos: **mejor performance**, **maximizar el almacenamiento**, y **conservar todo el
histórico por auditoría**, con la posibilidad de **niveles de compresión configurables desde
la interfaz**.

## 2. Qué se midió (producción, 2026-09-18)

| Tabla | Filas | Tamaño | Por fila | Ritmo |
|---|---|---|---|---|
| `reportes_historicos` | 2.663.140 (desde 2026-04-01) | **16,33 GB (~93%)** | ~6,1 KB | ~98 MB/día, ~35 GB/año |
| `software_monitoring` | 1.824.891 (desde 2026-04-02) | 0,5 GB | ~290 B | ~11 mil filas/día |
| `reportes_uso` | 43.347 (desde 2026-03-06) | 0,04 GB | | |
| `alertas` | 1.513 (desde 2026-01-15) | mínimo | | |

- Los índices suman ~0,3 GB: irrelevantes. **Todo el problema es el `full_json_data` de
  `reportes_historicos`** (JSON promedio 6.207 bytes sobre las últimas 2.000 filas).
- Como 16,33 GB / 2,66 M filas ≈ 6,1 KB, cada fila pesa lo que un JSON crudo: la compresión
  actual reduce la *cantidad* de filas viejas pero no el tamaño por fila.
- **Riesgo hacia adelante**: `software_monitoring` es chica hoy, pero un hospital con ~25
  canales Mirth genera ~7.200 filas/día. Habilitado en más hospitales crece de forma
  significativa.
- Proyección (a validar): conservar todo crudo son ~35 GB/año. Con compresión sin pérdida de
  5 a 10x (**hipótesis sin medir**) serían 3,5 a 7 GB/año, unos 35 a 70 GB en 10 años.

## 3. Hallazgos del código

- **`maintenance.py`** comprime bloques de 30 min solo de `reportes_historicos`, pasados 7
  días, con un lote de 1.000 filas por corrida (`BATCH_SIZE`); `server.py` lo dispara una vez
  por día. `DIAS_RETENCION_TOTAL = 365` está definido y nunca se usa. Es un **resumen con
  pérdida** (conserva el último documento y promedia CPU/RAM del host): choca con el
  requisito de auditoría, y cada día descarta muestras de forma irreversible.
- `software_monitoring`, `reportes_uso` y `alertas` no tienen retención.
- **Archivado manual existente**: `exportar_mes.py` + `borrar_mes.py` + `limpiar_db.py`
  (meses hardcodeados) generan `historico_YYYY_MM.db`. **Ningún código lee esos archivos**:
  lo archivado es inaccesible desde el dashboard.
- **Quién lee la historia**:
  - `reportes_historicos`: gráfico `/history` (hasta 30D, `LIMIT 15000`, submuestreo a ~600
    puntos parseando el JSON de cada fila en Python), y dos PDF de infraestructura con rango
    libre (`routers/informes.py:184` y `generator_report.py:778`, código duplicado, cargan
    todas las filas del rango en memoria).
  - Último reporte por hospital: detalle, `resumen_red`, OFFLINE, motor de alertas
    (`alerts_engine/infra.py:74` hace un `MAX(timestamp) ... GROUP BY hospital_id` sobre la
    tabla completa cada 60 s).
  - `reportes_uso` se lee **completa** (`resumen_red.py:141` y el resumen por hospital suman
    todo el histórico de KPIs): no se puede podar. `alertas` alimenta los PDF.
  - `software_monitoring`: motor de alertas (últimas filas) y `/software` (hasta 7D).
- Lo que efectivamente se usa del JSON histórico: CPU/RAM del host, temperaturas, red,
  CPU/RAM por VM y el último snapshot completo del período.
- **Portabilidad a Postgres**: 29 `json.loads(...)` y 18 `text()` crudos. En Postgres las
  columnas JSON llegan ya parseadas y `json.loads(dict)` falla; algunos sitios ya lo toleran
  (`/history`, los PDF), otros no (`hospital_detalle.py:36`, `alerts_engine/software/mirth.py`,
  `routers/mirth_mapa.py`). Los timestamps en SQLite llegan como texto.
- El endpoint de ingesta (`main.py`) es `async` pero usa sesión síncrona (bloquea el event
  loop). Los esquemas se manejan con `create_all` + scripts manuales en `Accesorios/`.

## 4. Compresión sin pérdida, resumen y borrado: son cosas distintas

| Operación | Reversible | ¿Compatible con auditoría? |
|---|---|---|
| Compresión sin pérdida | Sí | Sí, y ahorra espacio |
| Resumen (lo que hace hoy `maintenance.py`) | No | No |
| Borrado por retención | No | Solo si la política lo permite |

Conservar todo y ahorrar espacio no se oponen: se resuelve con compresión sin pérdida.

## 5. Arquitectura objetivo

```
Agentes ──> Ingesta ──> PostgreSQL (+ TimescaleDB, a confirmar)
                          ├─ reporte_raw            JSONB comprimido sin pérdida (auditoría)
                          ├─ metricas_infra         columnas tipadas (gráficos y PDF rápidos)
                          ├─ metricas_vm            formato largo por VM
                          ├─ hospital_estado_actual 1 fila por hospital (detalle, OFFLINE, alertas)
                          ├─ software_monitoring / reportes_uso / alertas
                          └─ politicas_retencion    (editable desde la UI)
Lectura: dashboard, PDF, motor de alertas ──> una única capa de acceso
```

- Todo en un solo motor: **desaparece la lectura dual** SQLite + histórico.
- Se separa la forma del dato: tablas angostas tipadas para consultar, payload crudo
  comprimido para auditar.
- **Niveles configurables (UI Admin)**: por defecto solo lo reversible (a los N días,
  comprimir sin pérdida). Todo lo irreversible (resumen o borrado) va aparte, con vista
  previa (mismo patrón que Exclusiones), doble confirmación, **piso de retención** que la UI
  no puede bajar y registro de quién cambió cada política.

## 6. Alternativas evaluadas

- **A. Solo SQLite con retención y resumen**: descartada por los nuevos requisitos (el
  resumen es con pérdida y SQLite no comprime sin pérdida ni escala en escritores).
- **B. SQLite caliente + Postgres histórico**: viable, pero es la que más código nuevo pide
  (enrutar por rango, mezclar y deduplicar en el borde, ETL idempotente, dos motores para
  siempre).
- **C. Todo a Postgres (elegida)**: migración única y ventana de corte, a cambio de un solo
  motor y sin lectura dual.
- ClickHouse: mejor compresión para analítica, pero sería una tercera tecnología para un
  volumen que Postgres maneja de sobra.

## 7. Plan por fases

| Fase | Objetivo | Entregable / criterio de salida | Tamaño |
|---|---|---|---|
| **0. Medir y decidir** | Cerrar incógnitas sin tocar nada | Prueba de compresión (`zstd`) sobre una muestra; cuántas filas ya están resumidas con pérdida (`_is_compressed`); inventario de los `historico_*.db`; espacio en disco y RAM; licencia/compatibilidad de TimescaleDB; decisiones de abajo | S |
| **1. Infra Postgres** | Staging y producción | Instalación, endurecimiento (TLS, roles app/lectura/migración), backups con recuperación a un punto en el tiempo, monitoreo, prueba de restauración | M |
| **2. Modelo y acceso a datos** | Código preparado | Esquema con migraciones (Alembic), particiones/hypertables, políticas de compresión, capa única de lectura de series, porte de dialecto, ingesta por lotes con pool | L |
| **3. Carga histórica** | Pasar el histórico | Chunks idempotentes por hospital/mes, transformando a tipadas + crudo; verificación de conteos y comparación de JSON en muestra; incluir los `historico_*.db` | M |
| **4. Doble escritura** | Validar en vivo | Ingesta escribe en ambos motores 1 a 2 semanas con comparación automática; lecturas siguen en SQLite | M |
| **5. Corte** | Cambiar de motor | Interruptores por endpoint, alertas OFFLINE pausadas en la ventana (los agentes no tienen cola), motor de alertas al final. Rollback: volver el interruptor a SQLite, que sigue actualizado | S |
| **6. Políticas y UI** | Niveles configurables | Pantalla Admin con vista previa, piso de retención, doble confirmación, registro de cambios | M |
| **7. Retiro de SQLite** | Limpiar | Backup final de solo lectura, apagar `maintenance.py` y los scripts de export/borrado/vacuum, actualizar docs | S |

Orden: 0 → 1 → 2 → 3 → 4 → 5 → 6 → 7. Las fases 1 y 2 pueden ir en paralelo; la 6 puede ir
después del corte.

## 8. Decisiones abiertas

1. **Período de auditoría** y si exige **inmutabilidad**. Hoy no hay registro de acciones de
   usuarios ni de cambios de configuración; si la auditoría los incluye, es funcionalidad
   nueva.
2. **¿Pausar `maintenance.py` ya?** Mientras corre destruye muestras de más de 7 días. Es
   independiente del plan y solo cuesta espacio (~3 GB/mes).
3. **Dónde vive Postgres** (mismo servidor o aparte), si ya hay uno y quién lo opera.
4. **Tolerancia a downtime** en el corte.
5. **Alcance de la auditoría**: solo infraestructura y KPIs, o también `software_monitoring`.

## 9. Riesgos

- **TimescaleDB**: la compresión va bajo su licencia comunitaria y algunos Postgres
  gestionados no la permiten. Alternativa sin extensión: pg_partman + compresión JSONB, que
  comprime menos.
- **Operar Postgres** es infraestructura nueva (backups, seguridad, monitoreo), y el server
  no tiene suite de tests que respalde una migración de motor.
- **OFFLINE masivo**: cualquier corte o VACUUM que tire la ingesta más de `offline_minutes`
  (15) abre alertas OFFLINE para toda la flota.
- El `VACUUM` de la base actual necesita el server detenido y ~2x de disco libre.
- Los datos ya resumidos con pérdida por `maintenance.py` no se pueden recuperar; los
  archivos `historico_*.db` conviene revisarlos antes de darlos por buenos.
