# Contrato de datos — qué debe enviar el agente al servidor

Especificación de lo que el agente instalado en cada hospital tiene que mandarle al
servidor para que el reporte se acepte, se guarde, y genere las alertas/KPIs esperados.
Basado en la lectura exacta de `schemas.py` (validación Pydantic), `main.py` (qué hace con
cada campo al recibirlo) y `dashboard_app/alerts_engine/` (qué campos lee cada detector
para decidir un nivel de alerta) — no es una interpretación, es lo que el código
efectivamente exige y consume hoy, `2026-09-10` (respuestas de error revisadas `2026-09-17`).

## 1. El endpoint

```
POST /v1/hospital-status
Content-Type: application/json
Status esperado: 201
```

Sin autenticación para `schema_version` viejo (`3.0` a `4.3`); a partir de `"4.5"` exige
token por hospital — ver [§2bis](#2bis--autenticación-por-token--a-partir-de-schema_version-45-vigente)
más abajo, ya vigente, no pausado. El body tiene un **tope de 2 MB** (`MAX_BODY_INGESTA_BYTES` en
`main.py`, ver [04-seguridad.md#s5](04-seguridad.md#s5)); un reporte real pesa unos 10–55 KB, así
que el tope solo se alcanza con un payload anómalo.

**Respuestas:**
- `201` + `{"status": "ok", "id": <int>, "v3_conversion": false, "version": "4.3"}` — aceptado.
- `400` + `{"detail": "..."}` — el body no se pudo ni leer como JSON (vacío, corrupto,
  conexión cortada a mitad del envío). No llegó a intentarse la validación del contrato.
- `401` + `{"detail": "No autorizado"}` — solo para `schema_version` que exige token (ver
  §2bis): falta el header, el token no existe, o no corresponde al `hospital_id` declarado.
  No se guarda nada.
- `413` + `{"detail": "Payload too large"}` — el body supera los 2 MB (se mira el `Content-Length`
  y también lo realmente recibido, por si el envío es en trozos). No se intenta leer ni se guarda
  nada; el checkpoint del agente no avanza, igual que con cualquier otro error.
- `500` + `{"detail": "Error interno de procesamiento de formato"}` — el JSON se pudo leer
  pero el payload no pasó la validación del contrato, o algo se rompió procesándolo. El
  servidor **no** te dice qué campo falló (es a propósito, para no filtrar detalles internos
  — ver [04-seguridad.md](04-seguridad.md)), así que conviene loguear del lado del agente el
  payload completo antes de mandarlo, para poder comparar contra este documento si un envío
  rebota.

> Corregido `2026-09-17`: hasta esa fecha, un body ilegible (JSON corrupto o conexión
> cortada a mitad del envío) devolvía `201` en vez de un código de error — el endpoint
> declara `status_code=201` como default de FastAPI para cualquier `return` que no sea una
> excepción, y esos tres casos hacían `return {...}` en vez de levantar `HTTPException`. Como
> el agente solo mira `raise_for_status()` (nunca el body de la respuesta, ver
> [CONTRATO_AGENTE.md §1](../../tecnomonitor-agent/docs/CONTRATO_AGENTE.md) del repo del
> agente), tomaba esos envíos corruptos como éxito y avanzaba el checkpoint de SQL/Elastic de
> un dato que nunca se guardó — pérdida silenciosa y permanente de ese bloque. Ver `main.py`,
> función `recibir_reporte`.

## 2. ⚠️ La trampa más importante: `schema_version`

El servidor decide cómo interpretar el payload mirando **un solo campo**:
`envelope.schema_version`.

```python
if schema_version in ["3.0", "4.0", "4.1", "4.2", "4.3"]:
    final_payload = raw_body                              # se usa tal cual
else:
    final_payload = transformer.transformar_v2_a_v3(raw_body)  # se asume formato VIEJO
```

Si mandás el formato de este documento pero `schema_version` viene vacío, mal escrito, o
directamente no está — el servidor **no rechaza el payload**: asume que es el formato
legacy V2 (completamente distinto: `header`/`physical_host`/`environment`/`vms` en vez de
`envelope`/`physical_layer`/`virtual_layer`) e intenta transformarlo. El resultado es
basura silenciosa, no un error claro. **Para un agente nuevo, usá siempre
`"schema_version": "4.3"`** (la versión más nueva aceptada tal cual).

## 2bis. ✅ Autenticación por token — a partir de `schema_version 4.5` (vigente)

**Implementado y desplegable — 2026-09-11.** Ver
[11-plan-auth-ingesta-agente.md](11-plan-auth-ingesta-agente.md) para el diseño completo.

Las versiones viejas (`"3.0"` a `"4.3"`) **no cambian**: siguen sin pedir nada, como hoy.
A partir de `"schema_version": "4.5"`, el reporte **exige** un header adicional:

```
Authorization: Bearer <token>
```

- El token es único por hospital, generado desde el panel de administración (al crear el
  hospital, o mediante un botón "regenerar" para uno ya existente). Se copia una sola vez
  al configurar el agente -- el servidor no lo vuelve a mostrar después.
- El servidor identifica el hospital **por el token**, no por el `hospital_id` del JSON --
  si no coinciden, rechaza. Mandá siempre los dos (el token en el header, `hospital_id` en
  el envelope) y asegurate de que sean del mismo hospital.
- Si falta el header, el token no existe, o no corresponde al `hospital_id` declarado: el
  servidor rechaza el reporte completo (401) y no guarda nada. El mensaje de rechazo es
  intencionalmente genérico (no dice cuál de los tres motivos fue).

**Antes de mandar un agente en `"4.5"` a producción**, generar el token del hospital desde
el panel (alta nueva, o `POST /api/hospitales-metadata/{hid}/regenerar-token` para uno ya
existente) y confirmar con el equipo que el código del servidor con este gate ya está
desplegado -- hasta entonces, seguí usando `"4.3"`.

## 3. `envelope` — metadata del reporte

No tiene un esquema Pydantic estricto en V4 (es `Dict[str, Any]`, técnicamente acepta
cualquier cosa), pero en la práctica el servidor espera estos 4 campos:

| Campo | Tipo | Obligatorio en la práctica | Qué pasa si falta |
|---|---|---|---|
| `schema_version` | string | **Sí** | Ver §2 — sin esto, el payload se interpreta como legacy V2 y probablemente se corrompe o rebota. |
| `hospital_id` | string | **Sí** | Se guarda como `"UNKNOWN"`. Además, si no coincide con un `hospital_id` ya cargado en `hospitales_metadata` (por un Admin, desde el panel), **el reporte se guarda pero el motor de alertas no evalúa nada para él** (todas las consultas de alertas filtran por hospitales ya existentes en esa tabla). |
| `agent_version` | string | Recomendado | No se usa para nada crítico hoy, pero ayuda a diagnosticar en soporte qué versión de agente reportó algo raro. |
| `timestamp` | string ISO 8601 (`"2026-09-10T14:30:00"`) | **Sí** | Si falta o no parsea, el servidor usa **su propia hora** (`datetime.now()` del servidor, no la del hospital) como timestamp del reporte — la detección de OFFLINE y los gráficos de histórico van a quedar levemente desalineados si esto pasa seguido. |

```json
"envelope": {
  "schema_version": "4.3",
  "agent_version": "2.4.1",
  "hospital_id": "H23",
  "timestamp": "2026-09-10T14:30:00"
}
```

## 4. `physical_layer` — estado del host físico

Clave requerida en el JSON (puede venir `{}`, pero la clave tiene que existir). Todo lo de
adentro es opcional, pero cada pieza faltante apaga una alerta puntual (no revienta nada).

### 4.1 `physical_layer.telemetry` — CPU/RAM del host

```json
"telemetry": {
  "cpu": { "usage_percent": 42.5 },
  "ram": { "total_gb": 64.0, "used_gb": 27.1, "usage_percent": 42.3 },
  "uptime_seconds": 1382400
}
```

| Campo | Consumido por |
|---|---|
| `cpu.usage_percent` | Columna `host_cpu_usage` (dashboard) **y** alerta `HOST_CPU` (escalonado 75/85/90%). |
| `ram.used_gb` | Columna `host_ram_usage` (lo que se muestra en el dashboard). |
| `ram.usage_percent` | Alerta `HOST_RAM` (umbral único, default 95% — configurable). **Ojo: es un campo DISTINTO de `used_gb` y lo usan consumidores distintos — mandá los dos.** |
| `uptime_seconds` | Alerta `HOST_UPTIME` si es menor a 600s (reinicio reciente/abrupto). Si no viene acá, el servidor prueba `host_info.uptime_seconds` como alternativa (ver 4.4). |

### 4.2 `physical_layer.sensors` — estado general, temperatura, fans, alimentación

```json
"sensors": {
  "status": "OK",
  "temperatures": [
    { "name": "CPU1", "value": 58.0, "unit": "C", "status": "OK" },
    { "name": "Ambient", "value": 24.5, "unit": "C", "status": "OK" }
  ],
  "fans": [
    { "name": "FAN1", "value": 4200, "unit": "RPM", "status": "OK" }
  ],
  "power": {
    "watts_current": 320.5,
    "supplies": [
      { "name": "PSU1", "watts": 320.5, "status": "OK" },
      { "name": "PSU2", "watts": 0.0, "status": "Failed" }
    ]
  }
}
```

| Campo | Consumido por |
|---|---|
| `status` | Columna `host_status`, se muestra tal cual en el dashboard. |
| `temperatures[].name`, `.value` | Una alerta `TEMP_<name>` por cada sensor, escalonada contra `temp_cpu_max` (configurable, default 70°C: NOTICE a -10, WARNING a -5, CRITICAL al llegar). `unit`/`status` se guardan pero no se usan para decidir el nivel. |
| `fans[].name`, `.status` | Alerta `FAN_<name>`: **todo o nada** — cualquier `status` distinto de exactamente `"OK"` (string) dispara CRITICAL. Si el switch "Fans" está apagado en el panel, esto no se evalúa. |
| `power.watts_current` | Columna `power_watts`. |
| `power.supplies[].name`, `.status` | Alerta `PSU_<name>`, mismo criterio todo-o-nada que fans (`status != "OK"` -> CRITICAL). |

### 4.3 `physical_layer.storage_layer` — RAID / discos físicos del host

Es un `Dict` libre (no tiene sub-schema estricto), pero el motor de alertas busca estas dos
claves puntuales:

```json
"storage_layer": {
  "logical_volumes": [
    { "name": "RAID1_SO", "status": "Online" }
  ],
  "physical_drives": [
    { "slot": 0, "status": "OK" },
    { "slot": 1, "status": "Predictive Failure" }
  ]
}
```

`status` en `"OK"` o `"Online"` (ambos válidos) = sano. Cualquier otro valor = CRITICAL.
Si el switch "RAID" está apagado en el panel, no se evalúa nada de acá.

### 4.4 `physical_layer.host_info` — identificación del host

```json
"host_info": { "hostname": "PVE-H23-01", "type": "proxmox", "model": "Dell R740", "uptime_seconds": 1382400 }
```

Solo `uptime_seconds` se usa activamente (como alternativa si no vino en `telemetry`, ver
4.1). El resto (`hostname`, `type`, `model`) se guarda en `full_json_data` para referencia
pero no dispara ninguna alerta ni se usa en ninguna query hoy.

### 4.5 `physical_layer.network_health` — latencia/uso de red

```json
"network_health": {
  "status": "OK",
  "upload_usage_mbps": 12.4,
  "download_usage_mbps": 45.2,
  "cloud_latency_ms": 38.0,
  "cloud_status": "OK"
}
```

Solo `cloud_latency_ms` dispara alerta (`NETWORK_LATENCY`): NOTICE a partir de 200ms,
CRITICAL a partir de 500ms. Requiere el switch "Latencia de Red" activo en el panel. El
resto de los campos se guarda pero no se evalúa.

## 5. `virtual_layer` — máquinas virtuales del host

Clave requerida (puede venir `[]`), lista de objetos:

```json
"virtual_layer": [
  {
    "id": "VM-RIS-01",
    "type": "vm",
    "state": "running",
    "telemetry": {
      "cpu": { "usage_percent": 35.0 },
      "ram": { "usage_percent": 60.0 },
      "uptime_seconds": 500000
    },
    "storage": [
      { "mount_point": "/", "total_gb": 100.0, "free_gb": 40.0, "usage_percent": 60.0 }
    ]
  }
]
```

| Campo | Consumido por |
|---|---|
| `id` | Identifica la VM en las alertas (`VM_CPU_<id>`, `VM_RAM_<id>`, `DISK_<id>_<mount>`) y en el panel. |
| `telemetry.cpu.usage_percent`, `telemetry.ram.usage_percent` | Alertas `VM_CPU_<id>` / `VM_RAM_<id>`, escalonadas contra `cpu_vm_max`/`ram_vm_max` (configurables, default 90%). |
| `storage[].mount_point`, `.usage_percent`, `.free_gb` | Alerta `DISK_<id>_<mount_point>`, escalonada contra `disk_threshold` (configurable, default 90%). `total_gb` se guarda pero no se usa para el nivel. |
| `state` | Se guarda y se muestra; no dispara alerta por sí solo. |

`application_layer.services` (estado de procesos/servicios dentro de la VM) también está
soportado por el schema pero **no lo lee ninguna alerta hoy** — se guarda en
`full_json_data` para referencia visual, nada más.

## 6. `application_metrics` — KPIs de negocio (RIS/PACS/usuarios)

**Opcional a nivel de la clave** (podés omitirla enteramente si el agente no mide esto
todavía), pero si la mandás, **cada item de cada lista exige TODOS sus campos** — no hay
valores por defecto acá, es una validación estricta con Pydantic y falta un campo tira
abajo *todo el reporte*, no solo ese item.

```json
"application_metrics": {
  "extraction_interval_hours": 1.0,
  "start_time_extraction": "2026-09-10T13:00:00",
  "end_time_extraction": "2026-09-10T14:00:00",
  "ris": [
    {
      "equipo": "RX Sala 1", "aet": "RX_SALA1", "mod": "CR",
      "totales": 20, "citados": 18, "admitidos": 15, "ejecutados": 15,
      "con_imagen": 15, "borradores": 2, "definitivos": 13, "suspendidos": 3
    }
  ],
  "pacs": [
    { "aet": "PACS_MAIN", "mod": "CR", "almacenados": 15 }
  ],
  "users": [
    { "rol": "Tecnólogo", "usuarios_unicos": 4, "inicios_sesion": 9 }
  ]
}
```

**`ris[]`** — un item por equipo/modalidad, dentro de la ventana de extracción
(`extraction_interval_hours`). Los nombres son bastante descriptivos del flujo clínico
(citado -> admitido -> ejecutado -> con imagen -> reporte borrador -> definitivo, o
suspendido en cualquier punto) — **confirmá la semántica exacta de cada contador con
el equipo clínico antes de mapear los datos del RIS**, esto es lo que infiero de los
nombres de campo, no una definición de negocio documentada en otro lado.

- `mod` y `admitidos` son los **dos únicos campos que hoy lee el motor de alertas**
  (`KPI_INACT_RAD`: suma `admitidos` de los `mod` configurados en el panel — default
  `DX,CR` — y si da cero en la ventana configurada, alerta; `KPI_INACT_MAMO`: mismo
  criterio pero busca `mod` que contenga `MG` o `MAMO`). El resto de los campos
  (`equipo`, `aet`, `totales`, `citados`, `ejecutados`, `con_imagen`, `borradores`,
  `definitivos`, `suspendidos`) se guardan para reporting/PDF pero no disparan alertas hoy.
- Todos los campos son obligatorios igual, aunque no se usen todos activamente.

**`pacs[]`** — almacenamiento por AET/modalidad. `aet` con prefijo `ENT_` se cuenta aparte
como "estudios con IA" en el resumen de red; `aet` en `CLIENT`/`WADO`/`PACS` se excluye del
conteo (son AETs técnicos, no equipos reales).

**`users[]`** — actividad de usuarios por rol. No lo lee ninguna alerta hoy, es para
reporting.

## 7. `software_monitoring` — integraciones de software

Clave opcional. Cuatro sub-claves, todas opcionales entre sí (podés mandar solo la que te
sirva).

### 7.1 `mirth` — canales de Mirth Connect

Dos formatos aceptados. Para un agente nuevo, usá el formato con instancias (dict):

```json
"mirth": {
  "Default": [
    { "channel": "HL7_ADMISSION", "channel_id": "7f3c1a2e-...", "status": "Started", "queued": 0,
      "last_error": "", "received": 1500, "sent": 1500, "errored": 0 }
  ]
}
```

(El formato legacy, una lista simple sin nombre de instancia, también se acepta y se
etiqueta internamente como instancia `"Default"` — no lo uses para un agente nuevo, es
solo por compatibilidad hacia atrás.)

| Campo | Consumido por |
|---|---|
| `channel` | Identifica el canal en la alerta (`MIRTH_<channel>`), junto con la instancia (`component_id = "[instancia] channel"`). |
| `channel_id` | Opcional, agregado `2026-09` (agente >= 4.5.1). GUID interno de Mirth, estable aunque se renombre el canal — se guarda en `extra_data.channel_id`. Join preferido con `mirth_channel_topology` para el mapa de integraciones; `component_id` sigue siendo el identificador legacy para agentes que no lo mandan. |
| `status` | Si es `STOPPED`/`ERROR`/`PAUSED` **dos ticks seguidos** (~2 minutos, filtra micro-cortes), CRITICAL. |
| `queued` | Si supera el umbral `crit` de la criticidad curada del canal (default `media` → 200; ver [13-contrato-topologia-mirth.md](13-contrato-topologia-mirth.md)), CRITICAL, aunque el status esté OK. El umbral único global `mirth_queued_threshold` quedó deprecado. |
| `errored` | Opcional, agregado `2026-09`. Contador de mensajes en error, como número propio (antes solo viajaba mezclado en el texto de `last_error`). Se guarda en `extra_data.errored`, no dispara alerta todavía. |
| `last_error`, `received`, `sent` | Se guardan para mostrar en el panel, no disparan alerta. |

**`mirth_topology`** (opcional, agregado `2026-09`, agente >= 4.5.1): clave hermana de
`mirth`, con la definición técnica de cada canal (conector de origen, conectores de destino,
y el `channel_id` destino cuando un conector es "Channel Writer" — routing interno entre
canales). No participa del detector de alertas; se ingiere en una tabla separada
(`mirth_channel_topology`, upsert por `channel_id`, no serie temporal) para alimentar el mapa
de integraciones. Ver [13-contrato-topologia-mirth.md](13-contrato-topologia-mirth.md) para
el contrato completo y [02-modelo-de-datos.md](02-modelo-de-datos.md) para el modelo. Un
agente que no manda esta clave sigue funcionando exactamente igual que antes — `mirth[]` no
depende de `mirth_topology` para nada.

### 7.2 `suitestensa_logs` — eventos de log (Elasticsearch/Suitestensa)

```json
"suitestensa_logs": {
  "scan_time": "2026-09-10T14:00:00Z",
  "events": [
    { "rule_id": "CRIT-MQ-01", "count": 3 }
  ]
}
```

`rule_id` se busca en la tabla `log_dictionary` (cargada por separado vía
`POST /api/admin/diccionario-logs`) para resolver severidad/título/descripción/acción
recomendada. Si el `rule_id` no está en el diccionario, se guarda igual con severidad
`INFO` y un título genérico de "regla desconocida". **Este es el módulo mencionado como
"en construcción" — la forma final de esta sub-clave puede cambiar.**

### 7.3 `ssl_certificates` — certificados por vencer

```json
"ssl_certificates": [
  { "url": "https://pacs.hospital.local", "status": "OK", "days_remaining": 45, "expiration_date": "2026-10-25", "issuer": "Let's Encrypt" }
]
```

⚠️ **Se ingesta y se guarda, pero al día de este documento ningún detector de
`alerts_engine` lo lee todavía** — es uno de los huecos identificados en
[09-plan-refactor-alertas.md](09-plan-refactor-alertas.md#1-el-problema-concreto). Mandalo
igual si ya lo tenés disponible: cuando se cierre ese hueco, no va a hacer falta tocar el
agente.

### 7.4 `dicom_routing_queues` — colas de auto-enrutado DICOM

```json
"dicom_routing_queues": [
  {
    "id_rule": "RULE_PACS_TO_CLOUD",
    "from_node": { "key": "pacs01", "nickname": "PACS Principal", "hostname": "10.0.0.5" },
    "to_node": { "key": "cloud01", "nickname": "Nube Central", "hostname": "cloud.tecnoimagen.com.ar" },
    "pending_instances": 1200
  }
]
```

`pending_instances` es una serie temporal: el detector mira si **bajó** en algún momento
de una ventana de tiempo (no un umbral fijo — ver
[alerts_engine/software/dicom_autoenrute.py](../dashboard_app/alerts_engine/software/dicom_autoenrute.py)
para el porqué). Para que la heurística funcione bien, **mandá esta cola en cada reporte
periódico**, no solo cuando cambia — el detector necesita varios puntos en el tiempo para
distinguir una cola sana (sube y baja) de una trabada (solo sube).

## 8. Payload mínimo que el servidor acepta

Esto pasa la validación y se guarda, pero no genera ninguna alerta interesante (sirve para
probar conectividad):

```json
{
  "envelope": {
    "schema_version": "4.3",
    "agent_version": "0.0.1-test",
    "hospital_id": "H23",
    "timestamp": "2026-09-10T14:30:00"
  },
  "physical_layer": {},
  "virtual_layer": []
}
```

## 9. Resumen: qué mandar según qué querés que funcione

| Si el agente mide... | Mandá... | Para activar |
|---|---|---|
| CPU/RAM/temp/fans/PSU del host | `physical_layer.telemetry` + `.sensors` | Alertas de infraestructura (activas por switch en el panel, `enable_fans`/`enable_power`) |
| RAID | `physical_layer.storage_layer` | Alertas RAID (switch `enable_raid`) |
| Latencia de red | `physical_layer.network_health.cloud_latency_ms` | Alerta de latencia (switch `enable_network_latency`) |
| VMs | `virtual_layer[]` | Alertas de VM (siempre activas si hay switch general de alertas para el hospital) |
| Producción RIS/PACS | `application_metrics` | KPIs de inactividad RIS/Mamografía (switches `kpi_rad_alert_enabled`/`kpi_mamo_alert_enabled`) |
| Mirth Connect | `software_monitoring.mirth` | Alerta de Mirth (switch `mirth_alert_enabled`) |
| Auto-enrutado DICOM | `software_monitoring.dicom_routing_queues`, **en cada reporte** | Alerta de cola trabada (switch `dicom_alert_enabled`) |
| Certificados SSL | `software_monitoring.ssl_certificates` | Nada todavía (dato guardado, sin alerta — ver §7.3) |
| Logs de Suitestensa | `software_monitoring.suitestensa_logs` | Se guarda enriquecido contra el diccionario; en construcción |

## 10. Formato legacy V2 (no usar para un agente nuevo)

Existe un formato anterior (`header`/`physical_host`/`environment`/`vms`, sin
`schema_version` reconocible) que el servidor sigue aceptando vía
`transformer.transformar_v2_a_v3()` por compatibilidad con agentes viejos ya desplegados.
No lo documento en detalle acá porque no aplica a un agente nuevo — si en algún momento
hace falta, la referencia autoritativa es el propio `transformer.py`.
