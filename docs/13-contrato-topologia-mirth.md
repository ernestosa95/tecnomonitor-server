# Contrato de datos — topología de integraciones Mirth (mapa de red)

**Estado — 2026-09-21: implementado en código (agente, servidor y frontend); ver "Pendientes".**
Este documento reemplaza a la versión anterior
(`2026-09-11`), que había dejado el problema bloqueado por dos motivos: sin confirmar si el
agente tenía acceso a la config de canal de Mirth, y con criticidad por canal / etiquetas
humanas explícitamente fuera de alcance. Ambos puntos se resolvieron: el acceso a
`GET /api/channels` es viable (misma sesión autenticada que ya usa `mirth_collector.py` para
`/statistics`/`/statuses`), y se decidió meter criticidad + curación humana en la misma
entrega. Ver el plan de implementación completo para el detalle fase por fase — acá va el
contrato de datos resultante.

Construye un panel como el prototipo `prototipo_red_integraciones.html` (mapa origen → canal
Mirth → destino, con criticidad, cola/tráfico en línea de tiempo, y routing interno entre
canales vía "Channel Writer").

## Qué está implementado hoy (agente >= 4.5.1)

- **Agente**: `mirth_collector.py` manda `channel_id`/`errored` por canal en `mirth[]`, y una
  clave nueva `mirth_topology` con la definición técnica de cada canal (origen, destinos,
  routing interno). Cacheado 1h por instancia, sin ningún toggle nuevo en la GUI — se activa
  junto con `enabled_mirth`. Ver
  [CONTRATO_AGENTE.md §7bis](../../tecnomonitor-agent/docs/CONTRATO_AGENTE.md#7bis-mirth-y-mirth_topology--extendidos-para-el-mapa-de-integraciones-mirth_collectorpy)
  para el contrato completo del lado agente.
- **Servidor — ingesta**: `main.py` guarda `channel_id`/`errored` en
  `SoftwareMonitoring.extra_data` (igual que siempre, serie temporal) y hace upsert de
  `mirth_topology` en la tabla nueva `mirth_channel_topology` (snapshot técnico, no serie —
  ver [02-modelo-de-datos.md](02-modelo-de-datos.md)). Tolerante a un agente viejo que no
  manda nada de esto: no crea filas de topología, no rompe la ingesta de `mirth[]`.
- **Umbrales de cola por criticidad** (alta/media/baja): `dashboard_app/alerts_engine/software/mirth.py`
  resuelve la criticidad de cada canal (`extra_data.channel_id` → `mirth_canales_meta.crit`,
  con fallback vía `mirth_channel_topology.component_id` para agentes viejos, y
  `mirth_crit_default` si el canal todavía no fue clasificado) y compara la cola contra el
  umbral `crit` de esa criticidad en vez del viejo `mirth_queued_threshold` único (deprecado,
  ya no se lee). `tipo_unico` de la alerta no cambió (sigue por `component_id`, no
  `channel_id`) para no dejar huérfanas las alertas `MIRTH_*` que ya estuvieran abiertas;
  `titulo_visible` sí usa el nombre humano curado cuando existe. Configurable desde el panel
  (`/api/config`, tarjeta "Integración Mirth Connect").

- **Router de administración** (`routers/mirth_topologia.py`): CRUD de nodos curados
  (sistemas origen/destino) y de metadata de canal (criticidad, nombre humano, asignación a
  nodos), más inventario unificado (`/mirth/canales`) que cruza topología técnica + curación
  + último estado, y "Adoptar" (`/mirth/nodos/adoptar`) para convertir un endpoint
  auto-detectado en nodo curado y asignarlo a varios canales de una. Ver
  [03-api-referencia.md](03-api-referencia.md#mapa-de-integraciones-mirth-routersmirth_topologiapy-ver-docs13-contrato-topologia-mirthmd)
  para el detalle de endpoints.

- **Endpoint de lectura del mapa** (`GET /api/hospital/{id}/mirth/mapa`,
  `routers/mirth_mapa.py`): arma `origenes`/`destinos`/`canales`/`tl` (línea de tiempo
  bucketizada por `paso` minutos) a partir de las tablas de curación + el snapshot técnico +
  el histórico de `software_monitoring`. `padre` se calcula al vuelo (no se persiste, ver
  decisión de diseño más arriba). Modo degradado (sin topología, `padre`/endpoints en
  `null`) para hospitales con agente viejo que todavía no manda `mirth_topology`. La
  freshness (`fresco`) es doble: por bucket histórico (¿hubo fila en esa ventana?, usada por
  el scrubber) y del último punto "vivo" (más laxa, `mirth_stale_minutes`, para no marcar
  todo como stale si el hospital reporta con un intervalo mayor al `paso` pedido).

- **Frontend**: pestaña "Integraciones" (`static/mapa_integraciones.js` y `.css`) en
  `index_beta.html` — y también todavía en `index.html`, la UI vieja en desuso — que consume
  `GET /api/hospital/{id}/mirth/mapa`. Modos Operación/Estado, formato Mapa/Lista, barra temporal
  ("en vivo" o un momento pasado), botón secundario "Últimos 30 min" que reemplaza el tráfico del
  tramo por el acumulado de la ventana (solo `index_beta.html`; detalle en REQ-04 de
  [16-plan-actualizacion-y-despliegue.md](16-plan-actualizacion-y-despliegue.md)) y panel de
  detalle por canal. Los canales sin clasificar se clasifican desde este panel.

## Pendientes

- **Criterio de antigüedad en la pestaña Software y en el detector de alertas de Mirth.** El mapa
  ya descarta datos viejos (`mirth_stale_minutes`), pero la pestaña Software y el detector
  (`alerts_engine/software/mirth.py`) no, y las dos vistas del mismo Mirth se contradicen: ver
  REQ-03 en el mismo documento.
- **Clasificar los canales pendientes** de cada hospital (badge `/api/mirth/pendientes`); hasta
  entonces usan `mirth_crit_default`. Es una tarea operativa, no de código.

## 1. Extensión a `software_monitoring.mirth[]` (implementado)

```json
"mirth": {
  "Default": [
    { "channel": "ORU_R01_Resultados", "channel_id": "a1b2c3d4-...",
      "status": "Started", "queued": 12, "received": 1500, "sent": 1488,
      "errored": 3, "last_error": "" }
  ]
}
```

| Campo nuevo | Obligatorio | Qué es |
|---|---|---|
| `channel_id` | Opcional (recomendado) | GUID interno de Mirth, estable aunque se renombre el canal. Un agente viejo que no lo manda sigue guardándose igual, solo que sin join posible contra `mirth_channel_topology`. |
| `errored` | Opcional | Contador de mensajes en error como número propio (antes solo texto en `last_error`). |

## 2. `mirth_topology` (implementado) — definición técnica de cada canal

Clave hermana de `mirth`, presente solo si el agente pudo leer `GET /api/channels` en ese
ciclo (o tiene una copia cacheada de un ciclo anterior — ver el detalle de cache en
`CONTRATO_AGENTE.md §7bis`).

```json
"mirth_topology": {
  "Default": {
    "collected_at": "2026-09-17T10:05:03",
    "full": true,
    "channels": [
      {
        "channel_id": "a1b2c3d4-...",
        "name": "ORU_R01_Resultados",
        "revision": 12,
        "source": {
          "transport": "Database Reader",
          "endpoint": "jdbc:sqlserver://10.0.2.10:1433;databaseName=RIS",
          "host": null, "port": null, "target_channel_id": null
        },
        "destinations": [
          {
            "metadata_id": 1, "name": "HIS - respuesta", "transport": "TCP Sender",
            "endpoint": "10.0.1.20:6662", "host": "10.0.1.20", "port": 6662,
            "target_channel_id": null, "enabled": true
          }
        ]
      }
    ]
  }
}
```

| Campo | Qué es |
|---|---|
| `full` | `true` = la lista de `channels[]` es el inventario completo de esa instancia en ese momento. Permite que el servidor "envejezca" (vía `last_seen`) los canales que dejaron de aparecer, sin necesidad de que el agente mande un borrado explícito. |
| `channels[].source` / cada entrada de `destinations[]` | `{transport, endpoint, host, port, target_channel_id}`. `endpoint` viene saneado (sin credenciales — ver `CONTRATO_AGENTE.md §7bis`). `target_channel_id` solo se puebla cuando el conector es un "Channel Writer" (routing interno entre canales); en ese caso `endpoint`/`host`/`port` van en `null`. |
| `destinations[]` como lista | Resuelve de entrada la pregunta que había quedado pendiente en la versión anterior de este documento: un canal con múltiples destination connectors los manda todos, no hay formato alternativo de "un solo destino". |

**Multi-instancia**: `mirth_topology` tiene una entrada por cada instancia configurada en
`mirth_servers[]`, igual que `mirth[]` — no hay ninguna clave nueva a nivel de envelope para
"agrupar" instancias de un mismo hospital.

## 3. Ingesta del lado del servidor (implementado)

**`mirth[].channel_id`/`errored`**: se agregan a `extra_data` en la fila de
`SoftwareMonitoring` que ya se creaba (append-only, una fila por canal por ciclo, igual que
siempre). Ningún cambio de forma en lo que ya existía.

**`mirth_topology`**: se guarda en una tabla propia, `mirth_channel_topology` — **no** en
`extra_data` de `software_monitoring`. Motivo: la definición de un canal cambia rarísima vez;
guardarla repetida en cada fila de la serie temporal (una por canal por ciclo) desperdicia
espacio y obliga a parsear JSON de la última fila para reconstruir el grafo cada vez que se
necesita. `main.py::_upsert_topologia_mirth` hace upsert por `(hospital_id, instancia,
channel_id)`: si el contenido no cambió (comparado por hash), solo actualiza `last_seen`; si
cambió, pisa los campos y `updated_at`. **Sin borrado**: un canal que deja de reportarse
envejece vía `last_seen`, no se borra solo — perdería la curación asociada (criticidad, nodo
asignado) ante un corte temporal de la API de Mirth.

Ver [02-modelo-de-datos.md](02-modelo-de-datos.md) para las columnas exactas de
`mirth_channel_topology`, `mirth_nodos` y `mirth_canales_meta` (estas dos últimas, de
curación, se administran desde `routers/mirth_topologia.py`, ver arriba).

## 4. Identificador estable de un canal — fallback en tres niveles

Como no todos los agentes van a estar actualizados al mismo tiempo, todo lo que consuma esta
información (detector de alertas, endpoint del mapa) resuelve la identidad de un canal en
este orden:

1. `extra_data.channel_id` (agente >= 4.5.1) — el ideal, estable ante renames.
2. `mirth_channel_topology.component_id → channel_id` (si el agente manda `mirth_topology`
   pero por algún motivo esa fila puntual de `mirth[]` no trajo `channel_id`).
3. Un hash de `component_id` (`"[instancia] nombre"`) como último recurso — funciona en modo
   degradado (sin routing interno derivable, sin endpoints técnicos), pero no rompe nada.
