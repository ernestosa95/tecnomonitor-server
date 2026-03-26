from fastapi import FastAPI, HTTPException, Depends, Request
from sqlalchemy.orm import Session
from datetime import datetime
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

app = FastAPI(title="TecnoXaas Monitor V4")

# Configurar Logger básico
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ingest-v4")

def get_db():
    db = database.SessionLocal()
    try: yield db
    finally: db.close()

# Definimos el esquema que espera recibir la API
class DictionaryPayload(BaseModel):
    app_name: str
    dictionary: Dict[str, Dict[str, Any]]

@app.post("/api/admin/diccionario-logs")
def actualizar_diccionario_logs(payload: DictionaryPayload, db: Session = Depends(get_db)):
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
        raw_body = await request.json()
    except ClientDisconnect:
        logger.warning("⚠️ [Ingesta] Cliente desconectado a mitad del envío.")
        return {"status": "error", "message": "Client disconnected during transfer"}
    except json.JSONDecodeError:
        logger.warning("⚠️ [Ingesta] JSON recibido es inválido o corrupto.")
        return {"status": "error", "message": "Invalid JSON"}
    except Exception as e:
        logger.warning(f"⚠️ [Ingesta] Error inesperado leyendo payload: {e}")
        return {"status": "error", "message": "Bad request format"}

    # =========================================================
    # 2. LÓGICA DE PROCESAMIENTO INTACTA
    # =========================================================
    try:
        final_payload = None
        is_legacy = False
        
        # Extraemos la versión para evaluarla
        schema_version = raw_body.get("envelope", {}).get("schema_version")

        # 1. DETECCIÓN DE VERSIÓN (Acepta 3.0 y 4.0)
        if schema_version in ["3.0", "4.0"]:
            # Es V3 o V4 Nativo -> Pasa directo sin transformar
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
        # phy puede ser None o un dict con valores None dentro
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
        # --- NUEVO V4: EXTRAER Y GUARDAR MÉTRICAS DE USO (KPIs) ---
        # =========================================================
        app_metrics = data_dict.get('application_metrics')
        
        if app_metrics:
            # 1. Creamos el registro para la nueva tabla
            nuevo_reporte_uso = database.ReporteUso(
                hospital_id = env.get('hospital_id', 'UNKNOWN'),
                timestamp = ts,
                kpi_json_data = json.dumps(app_metrics)
            )
            db.add(nuevo_reporte_uso)
            
            # 2. Eliminamos las métricas del JSON gigante para ahorrar espacio
            # Así la tabla reportes_historicos se queda solo con la infraestructura pura
            del data_dict['application_metrics']
        # =========================================================

        # =========================================================
        # --- NUEVO: EXTRAER Y GUARDAR MONITOREO DE SOFTWARE ---
        # =========================================================
        soft_monitoring = data_dict.get('software_monitoring')
        
        if soft_monitoring:
            h_id = env.get('hospital_id', 'UNKNOWN')
            
            # 1. Procesar Mirth Connect
            for item in soft_monitoring.get("mirth", []):
                db.add(database.SoftwareMonitoring(
                    hospital_id=h_id,
                    app_name="mirth",
                    component_id=item.get("channel", "unknown"),
                    status_value=item.get("status", ""),
                    metric_value=item.get("queued", 0),
                    extra_data={"last_error": item.get("last_error", "")},
                    timestamp=ts # Fallback a la hora de llegada
                ))

            # 2. Procesar Suitestensa
            suite_data = soft_monitoring.get("suitestensa", {})
            suite_scan_ts = suite_data.get("scan_ts")
            
            for ev in suite_data.get("evs", []):
                raw_ts = ev.get("ts") or suite_scan_ts
                ev_time = ts # Fallback
                if raw_ts:
                    clean_ts = raw_ts.replace('Z', '')
                    try:
                        ev_time = datetime.fromisoformat(clean_ts[:26])
                    except ValueError:
                        pass
                        
                db.add(database.SoftwareMonitoring(
                    hospital_id=h_id,
                    app_name="suitestensa",
                    component_id=ev.get("id", "unknown"),
                    status_value=None,
                    metric_value=ev.get("c", 0),
                    extra_data={"subsystems": ev.get("s", [])},
                    timestamp=ev_time
                ))
                
            # 3. Limpiar del JSON gigante para que la tabla histórica no engorde sin sentido
            del data_dict['software_monitoring']
        # =========================================================

        # 4. CREAR REGISTRO DB (Infraestructura)
        nuevo_registro = database.ReporteModel(
            hospital_id = env.get('hospital_id', 'UNKNOWN'),
            timestamp = ts,
            
            host_status = host_status,
            host_cpu_usage = host_cpu,
            host_ram_usage = host_ram,
            power_watts = p_watts,
            
            # Guardamos el JSON de infraestructura (ya sin los KPIs si venían)
            full_json_data = data_dict
        )
        
        # 5. COMMIT (Guarda ambas tablas al mismo tiempo)
        db.add(nuevo_registro)
        db.commit()
        
        logger.info(f"✅ Reporte guardado: {env.get('hospital_id')} (Versión: {schema_version} | Legacy: {is_legacy})")
        return {"status": "ok", "id": nuevo_registro.id, "v3_conversion": is_legacy, "version": schema_version}

    except Exception as e:
        logger.error(f"❌ Error crítico procesando payload: {e}")
        
        print("\n" + "="*50)
        print("🚨 CONTENIDO RECHAZADO:")
        print(json.dumps(raw_body, indent=2, default=str))
        print("="*50 + "\n")
        
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Error interno de procesamiento: {str(e)}")