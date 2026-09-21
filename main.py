from fastapi import FastAPI, HTTPException, Depends, Request
from sqlalchemy.orm import Session
from datetime import datetime
import hashlib
import json
import logging
import traceback
from starlette.requests import ClientDisconnect
from pydantic import BaseModel
from typing import Dict, Any

# Módulos propios
import schemas      
import transformer  
import database
import auth

app = FastAPI(title="TecnoXaas Monitor V4")

# Configurar Logger básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ingest-v4")

def get_db():
    db = database.SessionLocal()
    try: yield db
    finally: db.close()

# Versiones nativas que NO piden token todavía (no cambian con este gate).
VERSIONES_SIN_TOKEN = ["3.0", "4.0", "4.1", "4.2", "4.3"]
# Versiones nativas que SÍ exigen token de ingesta por hospital.
# Ver docs/11-plan-auth-ingesta-agente.md.
VERSIONES_CON_TOKEN = ["4.5"]


# Tope del body de la ingesta. Un reporte real pesa ~50 KB (peor caso medido: 50-55 KB, ver
# docs/04-seguridad.md#s5); 2 MB deja ~35 veces de margen. Se deja generoso a propósito: un rechazo
# es caro (el hospital se ve offline y el agente reintenta el mismo bloque en cada ciclo).
MAX_BODY_INGESTA_BYTES = 2 * 1024 * 1024


async def _leer_json_limitado(request: Request):
    """
    Equivale a `await request.json()`, pero corta con 413 si el body supera
    MAX_BODY_INGESTA_BYTES. Se mira el header Content-Length (rechazo barato, sin leer nada) y
    además lo acumulado del stream, porque el header puede faltar (chunked) o mentir.
    """
    def _rechazar(detalle: str):
        cliente = request.client.host if request.client else "?"
        logger.warning(f"⚠️ [Ingesta] Payload rechazado por tamaño (tope {MAX_BODY_INGESTA_BYTES} bytes) "
                       f"desde {cliente}: {detalle}")
        raise HTTPException(status_code=413, detail="Payload too large")

    declarado = request.headers.get("content-length")
    if declarado and declarado.isdigit() and int(declarado) > MAX_BODY_INGESTA_BYTES:
        _rechazar(f"Content-Length {declarado}")

    partes, total = [], 0
    async for trozo in request.stream():
        total += len(trozo)
        if total > MAX_BODY_INGESTA_BYTES:
            _rechazar("body acumulado por encima del tope")
        partes.append(trozo)
    return json.loads(b"".join(partes))


def _validar_token_ingesta(request: Request, raw_body: dict, db: Session) -> None:
    """
    Valida el header `Authorization: Bearer <token>` para schema_version que
    requieren token (VERSIONES_CON_TOKEN). Rechazo genérico a propósito (sin
    detallar el motivo) para no ayudar a adivinar tokens ajenos por descarte.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="No autorizado")

    token = auth_header[len("Bearer "):].strip()
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

    hospital = db.query(database.HospitalMetadata).filter_by(
        ingest_token_hash=token_hash
    ).first()
    if not hospital:
        raise HTTPException(status_code=401, detail="No autorizado")

    hospital_id_payload = raw_body.get("envelope", {}).get("hospital_id")
    if hospital_id_payload != hospital.hospital_id:
        raise HTTPException(status_code=401, detail="No autorizado")


def _upsert_topologia_mirth(db: Session, hospital_id: str, topo_payload, ts: datetime) -> None:
    """
    Guarda/actualiza (upsert, NO serie histórica) la definición técnica de
    cada canal de Mirth reportada en `software_monitoring.mirth_topology`
    (agente >= 4.5.1, ver tecnomonitor-agent/docs/CONTRATO_AGENTE.md §7bis).
    Alimenta el mapa de integraciones -- ver docs/13-contrato-topologia-mirth.md.

    Sin borrado: un canal que deja de aparecer envejece vía `last_seen` (se
    ve como "no visto hace X" en el panel de administración), no se borra
    solo -- perdería la curación asociada (criticidad, nodos asignados) ante
    un corte temporal de la API de Mirth. Tolerante a payload ausente o mal
    formado: un agente viejo que no manda esta clave no genera ninguna fila
    ni rompe el resto de la ingesta.
    """
    if not topo_payload or not isinstance(topo_payload, dict):
        return

    for instancia, bloque in topo_payload.items():
        if not isinstance(bloque, dict):
            continue
        canales = (bloque.get("channels") or [])[:500]  # tope defensivo

        for ch in canales:
            if not isinstance(ch, dict):
                continue
            cid = ch.get("channel_id")
            if not cid:
                continue

            nombre = (ch.get("name") or "unknown")[:200]
            component_id = f"[{instancia}] {nombre}" if instancia != "Default" else nombre
            origen = ch.get("source") or {}
            destinos = ch.get("destinations") or []

            hash_bloque = hashlib.sha1(json.dumps(
                {"nombre": nombre, "revision": ch.get("revision"), "origen": origen, "destinos": destinos},
                sort_keys=True, default=str,
            ).encode("utf-8")).hexdigest()

            fila = db.query(database.MirthChannelTopology).filter_by(
                hospital_id=hospital_id, instancia=instancia, channel_id=cid
            ).first()

            if fila is None:
                db.add(database.MirthChannelTopology(
                    hospital_id=hospital_id,
                    instancia=instancia,
                    channel_id=cid,
                    component_id=component_id,
                    nombre=nombre,
                    revision=ch.get("revision"),
                    source_transport=origen.get("transport"),
                    source_endpoint=origen.get("endpoint"),
                    destinos=destinos,
                    topo_hash=hash_bloque,
                    primera_vez=ts,
                    last_seen=ts,
                    updated_at=ts,
                ))
            else:
                fila.last_seen = ts
                if fila.topo_hash != hash_bloque:
                    fila.component_id = component_id
                    fila.nombre = nombre
                    fila.revision = ch.get("revision")
                    fila.source_transport = origen.get("transport")
                    fila.source_endpoint = origen.get("endpoint")
                    fila.destinos = destinos
                    fila.topo_hash = hash_bloque
                    fila.updated_at = ts


# Definimos el esquema que espera recibir la API
class DictionaryPayload(BaseModel):
    app_name: str
    dictionary: Dict[str, Dict[str, Any]]

@app.post("/api/admin/diccionario-logs")
def actualizar_diccionario_logs(
    payload: DictionaryPayload, 
    db: Session = Depends(get_db),
    # AGREGAR LA SIGUIENTE LÍNEA PARA REQUERIR EL ROL ADMIN:
    usuario_actual = Depends(auth.require_roles("Admin")) 
):
    """
    Endpoint para cargar o actualizar masivamente el diccionario de logs.
    """
    app_name = payload.app_name.lower()
    nuevos = 0
    actualizados = 0

    for event_id, data in payload.dictionary.items():
        # Buscamos si el código ya existe para ese software
        registro = db.query(database.LogDictionary).filter(
            database.LogDictionary.app_name == app_name,
            database.LogDictionary.event_id == event_id
        ).first()

        if registro:
            # Si existe, lo actualizamos
            registro.title = data.get("titulo", registro.title)
            registro.description = data.get("descripcion", registro.description)
            registro.action = data.get("accion", registro.action)
            registro.severity = data.get("severidad_default", registro.severity)
            actualizados += 1
        else:
            # Si no existe, lo creamos
            nuevo_registro = database.LogDictionary(
                app_name=app_name,
                event_id=event_id,
                title=data.get("titulo", ""),
                description=data.get("descripcion", ""),
                action=data.get("accion", ""),
                severity=data.get("severidad_default", "INFO")
            )
            db.add(nuevo_registro)
            nuevos += 1

    db.commit()
    return {
        "status": "ok", 
        "msg": f"Diccionario de {app_name} procesado.",
        "nuevos": nuevos,
        "actualizados": actualizados
    }

@app.post("/v1/hospital-status", status_code=201)
async def recibir_reporte(request: Request, db: Session = Depends(get_db)):
    """
    Ingesta Universal: Detecta v2/v3/v4, normaliza y guarda.
    """
    # =========================================================
    # 1. MANEJO DE RED SEGURO (Ataja ClientDisconnect)
    # =========================================================
    try:
        raw_body = await _leer_json_limitado(request)

        # =========================================================
        # 🔍 DEBUG TEMPORAL: IMPRIMIR PAYLOAD DE P10
        # =========================================================
        if isinstance(raw_body, dict):
            h_id = raw_body.get("envelope", {}).get("hospital_id")
            if h_id == "H45":
                logger.info("🔔 [DEBUG H45] Capturado reporte entrante:")
                # print(json.dumps(raw_body, indent=2))
        # =========================================================
    except HTTPException:
        raise  # el 413 del tope de tamaño no debe caer en el "except Exception" (400 genérico) de abajo
    except ClientDisconnect:
        logger.warning("⚠️ [Ingesta] Cliente desconectado a mitad del envío.")
        # HTTPException, no `return {...}`: la ruta declara status_code=201 por
        # defecto para cualquier return que no sea una excepción -- un dict de
        # error acá volvía como 201, y el agente (que solo mira raise_for_status())
        # lo tomaba como éxito y avanzaba el checkpoint de un dato que nunca se guardó.
        raise HTTPException(status_code=400, detail="Client disconnected during transfer")
    except json.JSONDecodeError:
        logger.warning("⚠️ [Ingesta] JSON recibido es inválido o corrupto.")
        raise HTTPException(status_code=400, detail="Invalid JSON")
    except Exception as e:
        logger.warning(f"⚠️ [Ingesta] Error inesperado leyendo payload: {e}")
        raise HTTPException(status_code=400, detail="Bad request format")

    # =========================================================
    # 2. LÓGICA DE PROCESAMIENTO
    # =========================================================
    schema_version = "Desconocida"
    is_legacy = False
    
    try:
        final_payload = None
        
        # Extraemos la versión para evaluarla
        schema_version = raw_body.get("envelope", {}).get("schema_version")

        # 1. DETECCIÓN DE VERSIÓN (Acepta 3.0 y 4.0)
        if schema_version in VERSIONES_SIN_TOKEN:
            # Es V3 o V4 Nativo -> Pasa directo sin transformar
            final_payload = raw_body
        elif schema_version in VERSIONES_CON_TOKEN:
            # 4.5+: exige Authorization: Bearer <token> por hospital.
            _validar_token_ingesta(request, raw_body, db)
            final_payload = raw_body
        else:
            # Es V2 Legacy -> Transformar a V3 (retrocompatible con V4)
            logger.info("Detectado payload V2 o desconocido. Iniciando transformación...")
            final_payload = transformer.transformar_v2_a_v3(raw_body)
            is_legacy = True

        # 2. VALIDACIÓN ESTRICTA (Usamos el nuevo Schema V4)
        reporte = schemas.AgentReportV4(**final_payload)
        
        # 3. PREPARAR DATOS SQL
        data_dict = reporte.model_dump(mode='json')
        
        # Extracción segura de capas principales
        env = data_dict.get('envelope') or {}
        phy = data_dict.get('physical_layer') or {}
        
        # Manejo seguro de timestamp
        ts_str = env.get('timestamp')
        try:
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now()
        except:
            ts = datetime.now()

        # --- EXTRACCIÓN BLINDADA ---
        sensors = phy.get('sensors') or {}  
        tele = phy.get('telemetry') or {}   
        
        cpu_obj = tele.get('cpu') or {}
        ram_obj = tele.get('ram') or {}
        
        host_status = sensors.get('status', 'Unknown')
        host_cpu = cpu_obj.get('usage_percent', 0.0)
        host_ram = ram_obj.get('used_gb', 0.0)

        # Power (con doble chequeo)
        p_watts = 0.0
        p_obj = sensors.get('power') 
        if p_obj:
            p_watts = p_obj.get('watts_current', 0.0)

        # =========================================================
        # --- EXTRAER Y GUARDAR MÉTRICAS DE USO (KPIs) ---
        # =========================================================
        app_metrics = data_dict.get('application_metrics')
        
        if app_metrics:
            nuevo_reporte_uso = database.ReporteUso(
                hospital_id = env.get('hospital_id', 'UNKNOWN'),
                timestamp = ts,
                kpi_json_data = json.dumps(app_metrics)
            )
            db.add(nuevo_reporte_uso)
            del data_dict['application_metrics']
        
        # =========================================================
        # --- EXTRAER Y GUARDAR MONITOREO DE SOFTWARE ---
        # =========================================================
        
        # 1. Definimos la variable (Evita el NameError)
        soft_monitoring = data_dict.get('software_monitoring')
        
        if soft_monitoring:
            h_id = env.get('hospital_id', 'UNKNOWN')
            
            # --- PROCESAR MIRTH CONNECT (Soporta múltiples instancias) ---
            mirth_data = soft_monitoring.get("mirth")
            
            # Detectamos formato: lista (v2) o diccionario (v3/v4 con instancias)
            if isinstance(mirth_data, dict):
                mirth_instances = mirth_data
            elif isinstance(mirth_data, list):
                mirth_instances = {"Default": mirth_data}
            else:
                mirth_instances = {}

            for instance_name, channels in mirth_instances.items():
                for item in channels:
                    nombre_canal = item.get("channel", "unknown")
                    id_comp = f"[{instance_name}] {nombre_canal}" if instance_name != "Default" else nombre_canal

                    db.add(database.SoftwareMonitoring(
                        hospital_id=h_id,
                        app_name="mirth",
                        component_id=id_comp,
                        status_value=item.get("status", ""),
                        metric_value=item.get("queued", 0), # El encolado sigue siendo nuestra métrica de control
                        extra_data={
                            "instancia": instance_name,
                            "last_error": item.get("last_error", ""),
                            # --- NUEVOS DATOS AGREGADOS ---
                            "recibidos": item.get("received", 0),
                            "enviados": item.get("sent", 0),
                            # --- Mapa de integraciones (ver docs/13-contrato-topologia-mirth.md) ---
                            "channel_id": item.get("channel_id"),
                            "errored": item.get("errored", 0),
                        },
                        timestamp=ts
                    ))

            # --- PROCESAR TOPOLOGÍA DE MIRTH (mapa de integraciones) ---
            _upsert_topologia_mirth(db, h_id, soft_monitoring.get("mirth_topology"), ts)

            # --- PROCESAR ELASTICSEARCH / SUITESTENSA LOGS ---
            suite_logs_data = soft_monitoring.get("suitestensa_logs", {})
            if suite_logs_data:
                scan_ts_str = suite_logs_data.get("scan_time", "")
                try:
                    scan_ts = datetime.fromisoformat(scan_ts_str.replace('Z', '')[:26]) if scan_ts_str else ts
                except:
                    scan_ts = ts
                    
                for ev in suite_logs_data.get("events", []):
                    rule_id = ev.get("rule_id", "UNKNOWN_RULE")
                    event_count = ev.get("count", 0)

                    # 1. Buscar la información enriquecida en el Diccionario de Logs
                    # Usamos first() porque el event_id debería ser único
                    dict_info = db.query(database.LogDictionary).filter(
                        database.LogDictionary.event_id == rule_id
                    ).first()

                    # 2. Asignar valores (si no existe en el dic, ponemos defaults)
                    severidad = dict_info.severity if dict_info and dict_info.severity else "INFO"
                    titulo = dict_info.title if dict_info else "Regla desconocida (No en diccionario)"
                    descripcion = dict_info.description if dict_info else "Sin descripción"
                    accion = dict_info.action if dict_info else "Avisar a soporte N2"

                    # 3. Guardar en SoftwareMonitoring
                    db.add(database.SoftwareMonitoring(
                        hospital_id=h_id,
                        app_name="elasticsearch", # Lo mantenemos como elasticsearch según la lógica original
                        component_id=rule_id,
                        status_value=severidad, # <-- Ahora viene del Diccionario
                        metric_value=event_count,
                        extra_data={
                            "titulo": titulo,
                            "descripcion": descripcion,
                            "accion_recomendada": accion,
                            "from_dictionary": True # Flag útil para saber que se enriqueció
                        },
                        timestamp=scan_ts # Usamos el scan_time general
                    ))

            # --- 3. NUEVO: PROCESAR CERTIFICADOS SSL ---
            ssl_data = soft_monitoring.get("ssl_certificates", [])
            for cert in ssl_data:
                db.add(database.SoftwareMonitoring(
                    hospital_id=h_id,
                    app_name="ssl_certificate",
                    component_id=cert.get("url", "unknown_url"),
                    status_value=cert.get("status", "Unknown"),
                    metric_value=cert.get("days_remaining", 0), # Usamos metric_value para los días
                    extra_data={
                        "expiration_date": cert.get("expiration_date", ""),
                        "issuer": cert.get("issuer", "")
                    },
                    timestamp=ts
                ))
            
            # --- 4. NUEVO: COLAS DE AUTO-ENRUTADO DICOM ---
            routing_queues = soft_monitoring.get("dicom_routing_queues") or []
            for rule in routing_queues:
                id_rule = rule.get("id_rule")
                if id_rule is None:
                    continue

                origen  = rule.get("from_node") or {}
                destino = rule.get("to_node") or {}

                # Sin nodo de origen (key nula) = la regla toma todos los equipos: no es "?".
                nick_o = origen.get("nickname")  or origen.get("hostname")  or ("TODOS" if origen.get("key") is None else "?")
                nick_d = destino.get("nickname") or destino.get("hostname") or "?"

                db.add(database.SoftwareMonitoring(
                    hospital_id=h_id,
                    app_name="dicom_routing",
                    component_id=str(id_rule),
                    status_value="OK",                       # severidad se calcula al leer
                    metric_value=int(rule.get("pending_instances") or 0),
                    extra_data={
                        "label": f"{nick_o} → {nick_d}",
                        "from_key": origen.get("key"),
                        "from_nickname": origen.get("nickname"),
                        "from_hostname": origen.get("hostname"),
                        "to_key": destino.get("key"),
                        "to_nickname": destino.get("nickname"),
                        "to_hostname": destino.get("hostname"),
                    },
                    timestamp=ts
                ))
                
            # Limpiamos el JSON antes de guardar la infraestructura
            del data_dict['software_monitoring']


        # 4. CREAR REGISTRO DB (Infraestructura)
        nuevo_registro = database.ReporteModel(
            hospital_id = env.get('hospital_id', 'UNKNOWN'),
            timestamp = ts,
            host_status = host_status,
            host_cpu_usage = host_cpu,
            host_ram_usage = host_ram,
            power_watts = p_watts,
            full_json_data = data_dict
        )
        
        # 5. COMMIT (Guarda ambas tablas al mismo tiempo)
        db.add(nuevo_registro)
        db.commit()
        
        logger.info(f"✅ Reporte guardado: {env.get('hospital_id')} (Versión: {schema_version} | Legacy: {is_legacy})")
        return {"status": "ok", "id": nuevo_registro.id, "v3_conversion": is_legacy, "version": schema_version}

    # 401 de _validar_token_ingesta: no es un error de formato, no pasa por
    # el bloque de rechazo genérico de abajo (que devuelve 500).
    except HTTPException:
        raise

    # =========================================================
    # 🛡️ BLOQUE CORREGIDO: RECHAZO SEGURO SIN DATA LEAKAGE
    # =========================================================
    except Exception as e:
        # Intentamos rescatar el hospital_id para saber quién falló
        h_id = 'UNKNOWN'
        if isinstance(raw_body, dict):
            h_id = raw_body.get('envelope', {}).get('hospital_id', 'UNKNOWN')
            
        tamanio_payload = len(str(raw_body))
        
        # Extraemos solo las llaves principales del JSON, nunca los valores
        estructura_claves = list(raw_body.keys()) if isinstance(raw_body, dict) else "Formato no diccionario"
        
        logger.error(
            "❌ Payload rechazado de %s | Tamaño: %d bytes | Error: %s | Estructura enviada: %s",
            h_id, tamanio_payload, str(e), estructura_claves
        )
        
        # Opcional: registrar el traceback técnico sin exponer datos
        traceback.print_exc()
        
        raise HTTPException(status_code=500, detail="Error interno de procesamiento de formato")