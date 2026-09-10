"""
Detalle de un hospital puntual: último reporte de infraestructura,
histórico (con downsampling), histórico de KPIs, estado de software
(Mirth/logs/SSL/colas DICOM -- la función más grande de todo dashboard.py),
detalle del diccionario de logs, y la configuración de KPIs granular por
hospital.

Décimo y último router extraído de dashboard.py -- ver
docs/08-plan-refactor-dashboard.md. Extraído con sed a partir del archivo
original (no retipeado a mano) para no arriesgar un error de transcripción
en la función de 268 líneas de estado de software.
"""
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

import alerts_engine
import auth
import database
from core import get_db
from database import HospitalMetadata

router = APIRouter()

@router.get("/api/hospital/{hospital_id}")
def obtener_detalle_hospital(hospital_id: str,
                             db: Session = Depends(get_db),
                             current_user: dict = Depends(auth.require_hospital_access("infra"))):
    query = text("SELECT * FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1")
    result = db.execute(query, {"hid": hospital_id}).fetchone()
    if not result: return {"error": "Hospital no encontrado"}
    try:
        full_data = json.loads(result.full_json_data) if result.full_json_data else {}
        full_data['db_timestamp'] = str(result.timestamp)[:19].replace("T", " ")
        return full_data
    except Exception: return {"error": "Error procesando datos"}

# --- EN DASHBOARD.PY ---
# 1. Devuelve esta función a su estado original simplificado
@router.get("/api/hospital/{hospital_id}/history")
def obtener_historial(hospital_id: str, horas: int = 24,
                      db: Session = Depends(get_db),
                      current_user: dict = Depends(auth.require_hospital_access("infra"))):
    flimit = datetime.now() - timedelta(hours=horas)
    
    # 🛡️ FIX: Agregamos LIMIT 15000 para evitar desbordamientos de memoria
    query = text("""
        SELECT timestamp, host_cpu_usage, full_json_data 
        FROM reportes_historicos 
        WHERE hospital_id = :hid AND timestamp >= :flimit 
        ORDER BY timestamp ASC
        LIMIT 15000
    """)
    result = db.execute(query, {"hid": hospital_id, "flimit": flimit}).fetchall()
    
    if not result: return []

    # 🛡️ FIX: Downsampling agresivo. Nunca devolvemos más de ~600 puntos al frontend.
    total_registros = len(result)
    step = 1
    if total_registros > 600: 
        step = max(1, int(total_registros / 600))
    muestras = result[::step]
    
    historial = []
    for row in muestras:
        try:
            if isinstance(row.full_json_data, str):
                d = json.loads(row.full_json_data) if row.full_json_data else {}
            else:
                d = row.full_json_data if row.full_json_data else {}
                
            # 1. Datos Físicos
            phy = d.get("physical_layer") or d.get("physical_host") or {}
            tele = phy.get("telemetry") or {}
            
            cpu_val = row.host_cpu_usage
            if cpu_val is None:
                cpu_val = (tele.get("cpu") or {}).get("usage_percent", 0)
                
            sensors = phy.get("sensors") or (d.get("environment") or {}).get("thermal") or {}
            temps_list = sensors.get("temperatures") or sensors.get("cpu_temps") or []
            
            cpu_s = {}
            for x in temps_list:
                val = x.get("value") if x.get("value") is not None else x.get("temp_c")
                name = x.get("name") or x.get("sensor")
                if val is not None and name:
                    cpu_s[name] = val
            
            amb_val = sensors.get("ambient_temp_c")
            if amb_val is None:
                for x in temps_list:
                    if "Ambient" in (x.get("name") or ""): 
                        amb_val = x.get("value")
                        break

            # --------------------------------------------------------
            # --- 1.5 Datos de Red (NUEVO) ---
            # --------------------------------------------------------
            net_health = phy.get("network_health") or {}
            net_lat = net_health.get("cloud_latency_ms")
            net_up = net_health.get("upload_usage_mbps")
            net_dw = net_health.get("download_usage_mbps")

            # 2. Datos Virtuales
            vms_data = {}
            if "virtual_layer" in d and isinstance(d["virtual_layer"], list):
                for vm in d["virtual_layer"]:
                    vid = vm.get("id")
                    if vid: 
                        vms_data[vid] = {
                            "cpu": (vm.get("telemetry") or {}).get("cpu", {}).get("usage_percent", 0), 
                            "ram": (vm.get("telemetry") or {}).get("ram", {}).get("usage_percent", 0)
                        }
            elif "vms" in d and isinstance(d["vms"], dict):
                for k, v in d["vms"].items():
                    m = v.get("metrics") or {}
                    vms_data[k] = {
                        "cpu": m.get("cpu_load_percent", 0),
                        "ram": (m.get("ram") or {}).get("percent", 0)
                    }

            historial.append({
                "timestamp": str(row.timestamp)[:19].replace("T", " "),
                "global": {
                    "cpu_host": cpu_val, 
                    "temp_amb": amb_val, 
                    "cpu_sensors": cpu_s,
                    # --- NUEVO: Inyectamos la red en el scope global ---
                    "network": {
                        "lat": net_lat, 
                        "up": net_up, 
                        "dw": net_dw
                    } 
                },
                "vms": vms_data
            })
        except Exception as e:
            continue
        
    return historial

# --- NUEVA RUTA PARA KPIS (Añadir a dashboard.py) ---
@router.get("/api/hospital/{hospital_id}/kpi-history")
def obtener_historial_kpi(hospital_id: str, horas: int = 24,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_hospital_access("kpis"))):
    
    fecha_limite_real = datetime.now() - timedelta(hours=horas)
    fecha_limite_sql = fecha_limite_real - timedelta(days=3)
    
    # 🛡️ FIX: Agregamos LIMIT 15000 como cap absoluto
    query = text("""
        SELECT timestamp, kpi_json_data 
        FROM reportes_uso 
        WHERE hospital_id = :hid AND timestamp >= :flimit 
        ORDER BY timestamp ASC
        LIMIT 15000
    """)
    result = db.execute(query, {"hid": hospital_id, "flimit": fecha_limite_sql}).fetchall()
    
    if not result: return []

    historial_kpi = []
    
    for row in result:
        try:
            metrics = json.loads(row.kpi_json_data) if row.kpi_json_data else {}
            fecha_extraccion_str = metrics.get("start_time_extraction")
            
            if fecha_extraccion_str:
                try:
                    fecha_evento = datetime.fromisoformat(fecha_extraccion_str)
                except ValueError:
                    fecha_evento = datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp
            else:
                fecha_evento = datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp
                
            if fecha_evento >= fecha_limite_real:
                historial_kpi.append({
                    "timestamp": fecha_evento.strftime("%Y-%m-%d %H:%M:%S"),
                    "application_metrics": metrics
                })
        except Exception as e:
            continue
            
    historial_kpi.sort(key=lambda x: x["timestamp"])
    
    return historial_kpi


# ============================================================
# ESTADO DE SALUD DE LAS COLAS DE AUTOENRUTE DICOM
# ------------------------------------------------------------
# Por qué esto vive en su propia función y con su propia query:
#
# 1) El estado NO puede depender del filtro de tiempo del panel. Antes se
#    calculaba sobre `history`, que es el resultado de la query filtrada por
#    `minutos`; con `minutos == 0` esa query devuelve UN registro por regla
#    (rn = 1), así que el estancamiento no se evaluaba nunca, y al pasar de
#    24H a 7D el mismo hospital podía mostrar estados distintos en el mismo
#    instante. La salud de una ruta no cambia según el zoom.
#
# 2) El criterio lo pone alerts_engine.evaluar_cola_dicom(), no una copia
#    local. Si el panel recalculara con su propia lógica, tarde o temprano
#    mostraría verde con un ticket abierto en Asana, o al revés.
# ============================================================
def _estado_colas_dicom(db: Session, hospital_id: str):
    """
    Devuelve {component_id: {"status", "estancada", "creciendo", "sin_datos"}}
    con ventana fija, independiente de lo que esté mirando el usuario.
    """
    try:
        cfg = alerts_engine.cargar_config(db)
    except Exception:
        cfg = {}

    win_crit = int(cfg.get('dicom_stall_critical_minutes', 120) or 120)
    win_warn = int(cfg.get('dicom_stall_warning_minutes', 45) or 45)
    min_inst = int(cfg.get('dicom_min_instances', 50) or 0)
    drain_pct = int(cfg.get('dicom_drain_percent', 70) or 70)

    ahora = datetime.now()
    desde = ahora - timedelta(minutes=win_crit * 1.5)

    filas = db.execute(text("""
        SELECT component_id, metric_value, timestamp
        FROM software_monitoring
        WHERE hospital_id = :hid
          AND app_name = 'dicom_routing'
          AND timestamp >= :desde
        ORDER BY component_id, timestamp ASC
    """), {"hid": hospital_id, "desde": desde}).fetchall()

    por_regla = {}
    for f in filas:
        por_regla.setdefault(f.component_id, []).append(f)

    estados = {}
    for id_rule, historia in por_regla.items():
        valores, timestamps = alerts_engine._serie_de(historia)
        
        nivel = None
        det = {"estancada": False, "creciendo": False, "minimo_ventana": 0, "ventana_minutos": win_warn}
        
        if not valores:
            estados[id_rule] = {"status": "SIN_DATOS", "estancada": False, "creciendo": False, "minimo_ventana": 0, "ventana_minutos": win_warn, "sin_datos": True}
            continue
            
        actual = valores[-1]
        
        if actual <= 0 or actual < min_inst:
            nivel = "OK"
        else:
            v_crit, cubre_crit = alerts_engine._ventana(valores, timestamps, win_crit, ahora)
            v_warn, cubre_warn = alerts_engine._ventana(valores, timestamps, win_warn, ahora)
            
            if not cubre_warn:
                nivel = None  # SIN_DATOS
            else:
                drena_warn = alerts_engine._drena(v_warn, drain_pct)
                drena_crit = alerts_engine._drena(v_crit, drain_pct) if cubre_crit else True
                
                det["minimo_ventana"] = min(v_warn) if v_warn else 0
                
                if not drena_crit:
                    nivel = "CRITICAL"
                    det["creciendo"] = actual >= v_crit[0] if v_crit else False
                    det["estancada"] = not det["creciendo"]
                    det["ventana_minutos"] = win_crit
                    det["minimo_ventana"] = min(v_crit) if v_crit else 0
                elif not drena_warn:
                    nivel = "WARNING"
                    det["creciendo"] = actual >= v_warn[0] if v_warn else False
                    det["estancada"] = not det["creciendo"]
                else:
                    nivel = "OK"

        estados[id_rule] = {
            "status": nivel or "SIN_DATOS",
            "estancada": bool(det.get("estancada")),
            "creciendo": bool(det.get("creciendo")),
            "minimo_ventana": det.get("minimo_ventana"),
            "ventana_minutos": det.get("ventana_minutos"),
            "sin_datos": nivel is None,
        }
        
    return estados

@router.get("/api/hospital/{hospital_id}/software")
def obtener_estado_software(hospital_id: str, minutos: int = 0,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(auth.require_hospital_access("software"))):

    # 1. QUERY (histórico total vs. ventana de tiempo)
    if minutos == 0:
        query = text("""
            WITH RankedData AS (
                SELECT app_name, component_id, status_value, metric_value, extra_data,
                       ROW_NUMBER() OVER(PARTITION BY app_name, component_id ORDER BY timestamp DESC) as rn
                FROM software_monitoring
                WHERE hospital_id = :hid
                  AND app_name IN ('mirth', 'ssl_certificate', 'elasticsearch', 'dicom_routing')
            )
            SELECT app_name, component_id, status_value, metric_value, extra_data, NULL as timestamp
            FROM RankedData WHERE rn = 1
        """)
        resultados = db.execute(query, {"hid": hospital_id}).fetchall()
        is_historical = False
    else:
        time_limit = datetime.now() - timedelta(minutes=minutos)
        query = text("""
            SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp
            FROM software_monitoring
            WHERE hospital_id = :hid
              AND app_name IN ('mirth', 'ssl_certificate', 'elasticsearch', 'dicom_routing')
              AND timestamp >= :time_limit
            ORDER BY timestamp ASC
        """)
        resultados = db.execute(query, {"hid": hospital_id, "time_limit": time_limit}).fetchall()

        # Fallback: hospital sin historial reciente -> traemos el último snapshot
        if not resultados:
            query_last = text("""
                WITH RankedData AS (
                    SELECT app_name, component_id, status_value, metric_value, extra_data,
                           ROW_NUMBER() OVER(PARTITION BY app_name, component_id ORDER BY timestamp DESC) as rn
                    FROM software_monitoring
                    WHERE hospital_id = :hid
                      AND app_name IN ('mirth', 'ssl_certificate', 'elasticsearch', 'dicom_routing')
                )
                SELECT app_name, component_id, status_value, metric_value, extra_data, NULL as timestamp
                FROM RankedData WHERE rn = 1
            """)
            resultados = db.execute(query_last, {"hid": hospital_id}).fetchall()
            is_historical = False
        else:
            is_historical = True

    # 2. AGRUPAMOS por aplicación y luego por canal/id
    canales_mirth = {}
    certificados_ssl = {}
    elastic_logs = {}
    colas_dicom = {}   # 🆕 DICOM

    for row in resultados:
        if row.app_name == 'mirth':
            canales_mirth.setdefault(row.component_id, []).append(row)
        elif row.app_name == 'ssl_certificate':
            certificados_ssl.setdefault(row.component_id, []).append(row)
        elif row.app_name == 'elasticsearch':
            elastic_logs.setdefault(row.component_id, []).append(row)
        elif row.app_name == 'dicom_routing':          # 🆕 DICOM
            colas_dicom.setdefault(row.component_id, []).append(row)

    software_data = {
        "metadata": {"minutos": minutos, "is_historical": is_historical},
        "mirth": {},
        "ssl_certificates": [],
        "elasticsearch": [],
        "dicom_routing": []        # 🆕 DICOM
    }

    # 3. PROCESAMOS MIRTH (deltas para el gráfico de tráfico)
    for cid, history in canales_mirth.items():
        if not history: continue
        actual = history[-1]
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}
        instancia = extra_actual.get("instancia", "Default")

        if instancia not in software_data["mirth"]:
            software_data["mirth"][instancia] = []

        canal_nombre = cid.replace(f"[{instancia}] ", "") if cid.startswith(f"[{instancia}] ") else cid

        historial_canal = []

        if minutos == 0:
            total_recibidos = extra_actual.get("recibidos", 0)
            total_enviados = extra_actual.get("enviados", 0)
        else:
            total_recibidos, total_enviados = 0, 0
            if is_historical and len(history) > 1:
                prev_r, prev_s = None, None
                for row in history:
                    extra = json.loads(row.extra_data) if row.extra_data else {}
                    r = extra.get("recibidos", 0)
                    s = extra.get("enviados", 0)

                    delta_r = (r - prev_r) if prev_r is not None and r >= prev_r else 0
                    delta_s = (s - prev_s) if prev_s is not None and s >= prev_s else 0

                    if prev_r is not None:
                        total_recibidos += delta_r
                        total_enviados += delta_s

                        if row.timestamp:
                            if isinstance(row.timestamp, str):
                                ts_str = row.timestamp[:19]
                            else:
                                ts_str = row.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            ts_str = ""

                        historial_canal.append({
                            "ts": ts_str,
                            "q": row.metric_value,
                            "traffic": delta_r + delta_s
                        })

                    prev_r, prev_s = r, s

        software_data["mirth"][instancia].append({
            "channel": canal_nombre,
            "status": actual.status_value,
            "queued": actual.metric_value,
            "received": total_recibidos,
            "sent": total_enviados,
            "last_error": extra_actual.get("last_error", ""),
            "history": historial_canal
        })

    # 4. PROCESAMOS CERTIFICADOS SSL
    for url, history in certificados_ssl.items():
        if not history: continue
        actual = history[-1]
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}

        software_data["ssl_certificates"].append({
            "url": url,
            "status": actual.status_value,
            "days_remaining": actual.metric_value,
            "expiration_date": extra_actual.get("expiration_date", ""),
            "issuer": extra_actual.get("issuer", "")
        })

    # 5. PROCESAMOS ELASTICSEARCH
    for rule_id, history in elastic_logs.items():
        if not history: continue
        
        # FIX: Se convierte explícitamente a string para evitar el TypeError
        history.sort(key=lambda x: str(x.timestamp) if x.timestamp else "")

        actual = history[-1]
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}

        last_seen_str = ""
        if actual.timestamp:
            if isinstance(actual.timestamp, str):
                last_seen_str = actual.timestamp[:19]
            else:
                last_seen_str = actual.timestamp.strftime("%Y-%m-%d %H:%M:%S")

        historial_regla = []
        for row in history:
            ts_str = ""
            if row.timestamp:
                if isinstance(row.timestamp, str):
                    ts_str = row.timestamp[:19]
                else:
                    ts_str = row.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            try:
                conteo = int(row.metric_value or 0)
            except (ValueError, TypeError):
                conteo = 0
            historial_regla.append({"ts": ts_str, "count": conteo})

        software_data["elasticsearch"].append({
            "rule_id": rule_id,
            "severity": actual.status_value,
            "count": actual.metric_value,
            "services": extra_actual.get("services", []),
            "evidence": extra_actual.get("evidence", ""),
            "last_seen": last_seen_str,
            "history": historial_regla
        })

    # ==========================================================
    # 6. 🆕 DICOM — COLAS DE AUTO-ENRUTADO
    # ----------------------------------------------------------
    # pending_instances es un GAUGE (nivel actual), no un contador acumulado:
    # NO se calculan deltas como en Mirth. La serie es el valor absoluto.
    #
    # El ESTADO sale de _estado_colas_dicom() (ventana fija + criterio del
    # motor de alertas). La serie del gráfico sí respeta el filtro de tiempo,
    # porque ahí el usuario efectivamente elige qué período mirar.
    # ==========================================================
    estados_dicom = _estado_colas_dicom(db, hospital_id) if colas_dicom else {}

    # Etiqueta del rango visible, para que "Pico" no se lea como valor actual.
    _LABEL_RANGO = {0: "histórico", 30: "30 min", 60: "1 h", 1440: "24 h", 10080: "7 días"}
    pico_label = _LABEL_RANGO.get(minutos, f"{minutos} min")

    for id_rule, history in colas_dicom.items():
        if not history: continue
        
        # FIX: Se convierte explícitamente a string para evitar el TypeError
        history.sort(key=lambda x: str(x.timestamp) if x.timestamp else "")

        actual = history[-1]
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}

        try:
            pendientes = int(actual.metric_value or 0)
        except (ValueError, TypeError):
            pendientes = 0

        # Serie histórica (valor absoluto). Los huecos NO se rellenan con 0:
        # una caída a cero en el gráfico se lee como "la cola se destrabó",
        # que es exactamente lo contrario de lo que pasó.
        historial_regla = []
        for row in history:
            ts_str = ""
            if row.timestamp:
                if isinstance(row.timestamp, str):
                    ts_str = row.timestamp[:19]
                else:
                    ts_str = row.timestamp.strftime("%Y-%m-%d %H:%M:%S")
            try:
                v = int(row.metric_value or 0)
            except (ValueError, TypeError):
                v = None
            historial_regla.append({"ts": ts_str, "pending": v})

        try:
            pico = max(int(h.metric_value or 0) for h in history)
        except (ValueError, TypeError):
            pico = pendientes

        est = estados_dicom.get(id_rule, {})
        estado = est.get("status", "SIN_DATOS")

        software_data["dicom_routing"].append({
            "id_rule": id_rule,
            "label": extra_actual.get("label", f"Regla {id_rule}"),
            "from_node": {
                "nickname": extra_actual.get("from_nickname"),
                "hostname": extra_actual.get("from_hostname")
            },
            "to_node": {
                "nickname": extra_actual.get("to_nickname"),
                "hostname": extra_actual.get("to_hostname")
            },
            "pending_instances": pendientes,
            "status": estado,
            "estancada": est.get("estancada", False),
            "creciendo": est.get("creciendo", False),
            "sin_datos": est.get("sin_datos", True),
            "minimo_ventana": est.get("minimo_ventana"),
            "ventana_minutos": est.get("ventana_minutos"),
            "pico": pico,
            "pico_label": pico_label,
            "history": historial_regla
        })

    return software_data


@router.get("/api/logs-dictionary/{event_id}")
def obtener_detalle_diccionario_log(event_id: str, 
                                     db: Session = Depends(get_db), 
                                     current_user: dict = Depends(auth.get_current_user)):
    """
    Busca la información explicativa de una regla en el diccionario de la base de datos.
    """
    log_dic = db.query(database.LogDictionary).filter(
        database.LogDictionary.app_name == "suitestensa",
        database.LogDictionary.event_id == event_id
    ).first()
    
    if not log_dic:
        # Fallback por si la regla no está documentada en la DB aún
        return {
            "event_id": event_id,
            "title": "Regla No Documentada",
            "description": "No se encuentra una descripción cargada para este ID de regla en el diccionario local de la base de datos.",
            "action": "Proceder con el análisis directo en la consola de ElasticSearch / Kibana.",
            "severity": "UNKNOWN"
        }
        
    return {
        "event_id": log_dic.event_id,
        "title": log_dic.title,
        "description": log_dic.description,
        "action": log_dic.action,
        "severity": log_dic.severity
    }

# --- 1. ENDPOINT PARA LEER LAS PREFERENCIAS (GET) ---
@router.get("/api/hospital/{hospital_id}/kpi-settings")
def get_kpi_settings(hospital_id: str, db: Session = Depends(get_db), current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial"))):
    """Devuelve la configuración granular de KPIs para un hospital específico."""
    hosp = db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first()
    
    if not hosp:
        raise HTTPException(status_code=404, detail="Hospital no encontrado")
    
    prefs = {}
    if hosp.kpi_settings:
        # Dependiendo del dialecto de DB, kpi_settings podría llegar como string o dict
        if isinstance(hosp.kpi_settings, str):
            try:
                prefs = json.loads(hosp.kpi_settings)
            except:
                prefs = {}
        else:
            prefs = hosp.kpi_settings
            
    # Valores por defecto para la UI si está vacío
    if not prefs:
        prefs = {
            "KPI_INACT_RAD": True,
            "KPI_INACT_MAMO": False, # Por defecto apagamos mamo hasta que el usuario lo prenda
        }
        
    return {"hospital_id": hospital_id, "kpi_settings": prefs}


# --- 2. ENDPOINT PARA GUARDAR LAS PREFERENCIAS (POST) ---
@router.post("/api/hospital/{hospital_id}/kpi-settings")
def update_kpi_settings(hospital_id: str, payload: dict, db: Session = Depends(get_db), current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    """Guarda la configuración granular de KPIs desde la UI."""
    hosp = db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first()
    
    if not hosp:
        raise HTTPException(status_code=404, detail="Hospital no encontrado")
    
    # Validamos que el payload sea un diccionario
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Formato de datos inválido. Se esperaba un JSON (diccionario).")

    # Guardamos en la base de datos (SQLAlchemy con tipo JSON lo maneja directamente)
    hosp.kpi_settings = payload
    db.commit()
    
    return {"status": "success", "message": "Configuración de alertas actualizada correctamente"}
