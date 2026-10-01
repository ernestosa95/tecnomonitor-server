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
from dataclasses import replace
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import alerts_engine
from alerts_engine import modulos
from alerts_engine.software import portal_paciente as portal_detector
from alerts_engine.software import sql_backups as sql_backups_detector
import auth
import database
from datos import infra as datos_infra
from datos import uso as datos_uso
from datos import software as datos_sw
from core import get_db
from database import HospitalMetadata
from routers.mirth_mapa import _parsear_ts

router = APIRouter()

@router.get("/api/hospital/{hospital_id}")
def obtener_detalle_hospital(hospital_id: str,
                             db: Session = Depends(get_db),
                             current_user: dict = Depends(auth.require_hospital_access("infra"))):
    reporte = datos_infra.ultimo_reporte(db, hospital_id)
    if not reporte: return {"error": "Hospital no encontrado"}
    full_data = dict(reporte.data)
    full_data['db_timestamp'] = str(reporte.timestamp)[:19].replace("T", " ") if reporte.timestamp else ""
    return full_data

# --- EN DASHBOARD.PY ---
# 1. Devuelve esta función a su estado original simplificado
@router.get("/api/hospital/{hospital_id}/history")
def obtener_historial(hospital_id: str, horas: int = 24,
                      db: Session = Depends(get_db),
                      current_user: dict = Depends(auth.require_hospital_access("infra"))):
    flimit = datetime.now() - timedelta(hours=horas)

    # Tope de 15.000 reportes leídos y ~600 puntos al frontend (submuestreo parejo).
    puntos = datos_infra.serie_infra(db, hospital_id, flimit, max_puntos=600, limite=15000)

    return [{
        "timestamp": str(p.timestamp)[:19],
        "global": {
            # Sin CPU en el reporte se grafica 0, como cuando se leía la columna host_cpu_usage.
            "cpu_host": p.cpu_host if p.cpu_host is not None else 0.0,
            "temp_amb": p.temp_amb,
            "cpu_sensors": p.temperaturas,
            "network": p.red,
        },
        "vms": p.vms,
    } for p in puntos]

# --- NUEVA RUTA PARA KPIS (Añadir a dashboard.py) ---
@router.get("/api/hospital/{hospital_id}/kpi-history")
def obtener_historial_kpi(hospital_id: str, horas: int = 24,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_hospital_access("kpis"))):
    
    fecha_limite_real = datetime.now() - timedelta(hours=horas)

    # Por fecha del evento (no de inserción), con tope de 15.000 reportes leídos.
    reportes = datos_uso.reportes_uso_por_evento(db, hospital_id, fecha_limite_real, limite=15000)
    reportes.sort(key=lambda r: r.fecha_evento)
    return [{"timestamp": r.fecha_evento.strftime("%Y-%m-%d %H:%M:%S"), "application_metrics": r.metrics}
            for r in reportes]


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
# 2) El criterio lo pone alerts_engine.evaluar_cola(), no una copia
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

    filas = datos_sw.lecturas(db, hospital_id, datos_sw.DICOM_ROUTING, desde, por_componente=True)

    por_regla = {}
    for f in filas:
        por_regla.setdefault(f.component_id, []).append(f)

    baselines = alerts_engine.cargar_baselines(db, hospital_id) if cfg.get('dicom_baseline_enabled', True) else {}

    estados = {}
    for id_rule, historia in por_regla.items():
        valores, timestamps = alerts_engine._serie_de(historia)
        
        nivel = None
        det = {"estancada": False, "creciendo": False, "minimo_ventana": 0, "ventana_minutos": win_warn}
        
        if not valores:
            estados[id_rule] = {"status": "SIN_DATOS", "estancada": False, "creciendo": False, "minimo_ventana": 0, "ventana_minutos": win_warn, "sin_datos": True}
            continue
            
        actual = valores[-1]
        bl = baselines.get(str(id_rule))
        res = alerts_engine.evaluar_cola(valores, timestamps, ahora, win_warn, win_crit,
                                         min_inst, drain_pct, bl)
        nivel = res["nivel"]

        if res["motivo"] == "drenaje":
            v_crit, v_warn = res["v_crit"], res["v_warn"]
            det["minimo_ventana"] = min(v_warn) if v_warn else 0
            if nivel == "CRITICAL":
                det["creciendo"] = actual >= v_crit[0] if v_crit else False
                det["estancada"] = not det["creciendo"]
                det["ventana_minutos"] = win_crit
                det["minimo_ventana"] = min(v_crit) if v_crit else 0
            elif nivel == "WARNING":
                det["creciendo"] = actual >= v_warn[0] if v_warn else False
                det["estancada"] = not det["creciendo"]

        estados[id_rule] = {
            "status": nivel or "SIN_DATOS",
            "estancada": bool(det.get("estancada")),
            "creciendo": bool(det.get("creciendo")),
            "minimo_ventana": det.get("minimo_ventana"),
            "ventana_minutos": det.get("ventana_minutos"),
            "sin_datos": nivel is None,
            "piso_habitual": bl["piso"] if bl and bl.get("activa") else None,
        }
        
    return estados

# ============================================================
# INTEGRIDAD DE BASES SQL (DBCC CHECKDB post-reinicio)
# ------------------------------------------------------------
# Mismo motivo que _estado_colas_dicom: el último chequeo de integridad es
# un evento raro (una vez por reinicio real de SQL Server), no algo que
# tenga sentido filtrar por el selector de tiempo del panel -- si el
# usuario mira "30 min" no debería dejar de ver el resultado de un
# reinicio de hace 3 días. Siempre trae la última fila por base,
# independiente de `minutos`.
# ============================================================
def _ultimo_checkdb(db: Session, hospital_id: str):
    filas = datos_sw.ultimas_lecturas(db, hospital_id, datos_sw.SQL_INTEGRITY)

    if not filas:
        return None

    bases = []
    for f in filas:
        extra = f.extra_data
        ts_str = ""
        if f.timestamp:
            ts_str = f.timestamp[:19] if isinstance(f.timestamp, str) else f.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        bases.append({
            "db": f.component_id,
            "status": f.status_value,
            "error_count": f.metric_value,
            "detail": extra.get("detail", ""),
            "checked_at": ts_str,
        })
    bases.sort(key=lambda b: b["db"])

    return {
        "last_checked_at": max((b["checked_at"] for b in bases if b["checked_at"]), default=""),
        "total": len(bases),
        "con_error": sum(1 for b in bases if (b["status"] or "").upper() == "ERROR"),
        "databases": bases,
    }


# ============================================================
# ÚLTIMO BACKUP COMPLETO DE LAS BASES SQL (REQ-06, agente 4.5.3)
# ------------------------------------------------------------
# Igual que _ultimo_checkdb: es un estado actual, independiente del selector
# de tiempo del panel. El criterio (umbral, "sin lecturas") es el mismo que
# usa el detector de alertas: alerts_engine/software/sql_backups.py.
# ============================================================
def _ultimo_backup(db: Session, hospital_id: str):
    max_horas = alerts_engine.cargar_config(db).get("sql_backup_max_hours", 24)
    bases = sql_backups_detector.estado_backups(db, hospital_id, max_horas)
    if not bases:
        return None
    con_fecha = [b["last_full"] for b in bases if b["last_full"]]
    return {
        "max_hours": max_horas,
        "total": len(bases),
        "vencidas": sum(1 for b in bases if b["status"] == sql_backups_detector.ESTADO_VENCIDO),
        "nunca": sum(1 for b in bases if b["status"] == sql_backups_detector.ESTADO_NUNCA),
        "sin_lectura": sum(1 for b in bases if b["status"] == sql_backups_detector.ESTADO_SIN_LECTURA),
        "mas_antiguo": min(con_fecha) if con_fecha else None,
        "last_seen": max((b["last_seen"] for b in bases if b["last_seen"]), default=None),
        "databases": bases,
    }


# ============================================================
# PORTAL PACIENTE: COLA DE PUBLICACIÓN RIS + MPS (REQ-07, agente 4.5.4)
# ------------------------------------------------------------
# El resumen (estado, pendientes, más antiguo) es el de la última lectura,
# con el mismo criterio que la alerta (alerts_engine/software/portal_paciente.py).
# La línea de tiempo sí respeta el selector de tiempo, como el autoenrute DICOM.
# ============================================================
def _portal_paciente(db: Session, hospital_id: str, minutos: int):
    max_horas = alerts_engine.cargar_config(db).get("portal_max_hours", 6)
    resumen = portal_detector.estado_portal(db, hospital_id, max_horas)
    if resumen is None:
        return None
    resumen["history"] = portal_detector.serie_portal(db, hospital_id, minutos)
    return resumen


APPS_PANEL = (datos_sw.MIRTH, datos_sw.SSL, datos_sw.ELASTIC, datos_sw.DICOM_ROUTING)


def _como_foto(lecturas):
    """
    Modo "estado actual": una lectura por componente, sin serie. La hora va en
    `ultimo_ts` y `timestamp` queda en None, que es lo que el armado de
    abajo usa para no dibujar historia.
    """
    fotos = []
    for lectura in lecturas:
        foto = replace(lectura, timestamp=None)
        foto.ultimo_ts = lectura.timestamp
        fotos.append(foto)
    return fotos


@router.get("/api/hospital/{hospital_id}/software")
def obtener_estado_software(hospital_id: str, minutos: int = 0,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(auth.require_hospital_access("software"))):

    # 1. LECTURAS (última por componente vs. ventana de tiempo)
    if minutos == 0:
        resultados = _como_foto(datos_sw.ultimas_lecturas(db, hospital_id, APPS_PANEL))
        is_historical = False
    else:
        time_limit = datetime.now() - timedelta(minutes=minutos)
        resultados = datos_sw.lecturas(db, hospital_id, APPS_PANEL, time_limit)

        # Fallback: hospital sin historial reciente -> traemos el último snapshot
        if not resultados:
            resultados = _como_foto(datos_sw.ultimas_lecturas(db, hospital_id, APPS_PANEL))
            is_historical = False
        else:
            is_historical = True

    # Módulos dados de baja (REQ-03): sus componentes no se muestran.
    bajas = modulos.bajas_efectivas_hospital(db, hospital_id)
    apps_ocultas = {app for m in bajas for app in modulos.MODULOS[m]["apps"]}
    resultados = [r for r in resultados if r.app_name not in apps_ocultas]

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
        "modulos_baja": sorted(bajas),
        "mirth": {},
        "ssl_certificates": [],
        "elasticsearch": [],
        "dicom_routing": []        # 🆕 DICOM
    }

    # 3. PROCESAMOS MIRTH (deltas para el gráfico de tráfico)
    # Un canal cuya última lectura quedó más de `mirth_stale_minutes` detrás de la más reciente de
    # Mirth *del hospital* se marca `stale` (mismo criterio que el mapa de integraciones): así un
    # canal borrado o un módulo apagado no se muestra como vigente con su último estado.
    stale_min = alerts_engine.cargar_config(db).get("mirth_stale_minutes", 15)
    ts_canal = {cid: _parsear_ts(getattr(h[-1], "ultimo_ts", None) or h[-1].timestamp)
                for cid, h in canales_mirth.items() if h}
    ultimo_ts_mirth = max((t for t in ts_canal.values() if t), default=None)

    for cid, history in canales_mirth.items():
        if not history: continue
        actual = history[-1]
        extra_actual = actual.extra_data
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
                    extra = row.extra_data
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

        ts_actual = ts_canal.get(cid)
        sin_datos_min = (int((ultimo_ts_mirth - ts_actual).total_seconds() // 60)
                         if ts_actual and ultimo_ts_mirth else None)

        software_data["mirth"][instancia].append({
            "channel": canal_nombre,
            "status": actual.status_value,
            "queued": actual.metric_value,
            "received": total_recibidos,
            "sent": total_enviados,
            "last_error": extra_actual.get("last_error", ""),
            "history": historial_canal,
            "stale": sin_datos_min is not None and sin_datos_min > stale_min,
            "sin_datos_min": sin_datos_min,
        })

    # 4. PROCESAMOS CERTIFICADOS SSL
    for url, history in certificados_ssl.items():
        if not history: continue
        actual = history[-1]
        extra_actual = actual.extra_data

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
        extra_actual = actual.extra_data

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
        extra_actual = actual.extra_data

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

    # 7. 🆕 INTEGRIDAD DE BASES SQL (DBCC CHECKDB) — ver _ultimo_checkdb().
    software_data["sql_integrity"] = (_ultimo_checkdb(db, hospital_id) if "sql_integrity" not in bajas
                                      else None)

    # 8. 🆕 ÚLTIMO BACKUP COMPLETO DE LAS BASES SQL — ver _ultimo_backup().
    software_data["sql_backups"] = (_ultimo_backup(db, hospital_id) if "sql_backups" not in bajas
                                    else None)

    # 9. 🆕 PORTAL PACIENTE (cola de publicación RIS + MPS) — ver _portal_paciente().
    software_data["patient_portal"] = (_portal_paciente(db, hospital_id, minutos) if "patient_portal" not in bajas
                                       else None)

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

    # Apagar una alerta de KPI para el hospital cierra la que esté abierta: el detector deja de
    # evaluarlo y, si no, quedaba abierta para siempre.
    cerradas = modulos.cerrar_kpis_apagados(db, hosp)

    return {"status": "success", "message": "Configuración de alertas actualizada correctamente",
            "alertas_cerradas": cerradas}
