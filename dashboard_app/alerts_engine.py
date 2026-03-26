import sys
import os
import json
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import desc, text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import database

try:
    import asana_conector
    print("✅ Conector Asana importado correctamente en Engine V3.")
except ImportError as e:
    print(f"⚠️ No se pudo cargar asana_conector: {e}")
    asana_conector = None

UMBRAL_LATENCIA_MS = 50.0  

def cargar_config(db: Session):
    def g(k, d, is_bool=False):
        r = db.query(database.ConfigModel).filter_by(clave=k).first()
        if r: return (r.valor == '1') if is_bool else int(r.valor)
        return d
    return {
        "offline_minutes": g("offline_minutes", 15),
        "disk_threshold": g("disk_threshold", 90),
        "temp_cpu_max": g("temp_cpu_max", 70),
        "enable_fans": g("enable_fans", True, is_bool=True),
        "enable_power": g("enable_power", True, is_bool=True),
        "enable_raid": g("enable_raid", True, is_bool=True)
    }

def analizar_reporte(hospital_id, json_data_v3, db: Session):
    meta = db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first()
    if not meta or not meta.alerts_enabled: return 
    config = cargar_config(db)
    _evaluar_reglas_v3(hospital_id, json_data_v3, db, config, meta.asana_project_id)

def procesar_offline(db: Session):
    config = cargar_config(db)
    _verificar_conectividad(db, config)
    try:
        query = text("""
            SELECT h.hospital_id, h.full_json_data 
            FROM reportes_historicos h
            INNER JOIN (SELECT hospital_id, MAX(timestamp) as max_t FROM reportes_historicos GROUP BY hospital_id) max_h 
            ON h.hospital_id = max_h.hospital_id AND h.timestamp = max_h.max_t
        """)
        reportes = db.execute(query).fetchall()
        for row in reportes:
            meta = db.query(database.HospitalMetadata).filter_by(hospital_id=row.hospital_id).first()
            if meta and meta.alerts_enabled and row.full_json_data:
                data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
                _evaluar_reglas_v3(row.hospital_id, data, db, config, meta.asana_project_id)
    except Exception as e:
        print(f"❌ Error en ciclo de vigilancia: {e}")

# --- HELPERS DE UMBRALES TRIPLES ---
def _nivel_cpu_ram(valor):
    if valor >= 90: return "CRITICAL"
    if valor >= 85: return "WARNING"
    if valor >= 75: return "NOTICE"
    return "OK"

def _nivel_temp(valor, crit_max):
    if valor >= crit_max: return "CRITICAL"
    if valor >= crit_max - 5: return "WARNING"
    if valor >= crit_max - 10: return "NOTICE"
    return "OK"

def _nivel_disco(valor):
    if valor >= 90: return "CRITICAL"
    if valor >= 85: return "WARNING"
    if valor >= 80: return "NOTICE"
    return "OK"

# --- MOTOR DE REGLAS V3 ---
def _evaluar_reglas_v3(hid, data, db, config, asana_proj_id):
    hallazgos = {} 
    phy = data.get('physical_layer') or {}
    tele_host = phy.get('telemetry') or {}
    sensors = phy.get('sensors') or {}
    storage_layer = phy.get('storage_layer') or {} 
    v_layer = data.get('virtual_layer') or []

    # 1. ANÁLOGOS (CPU/RAM/Temp)
    cpu_usage = (tele_host.get('cpu') or {}).get('usage_percent', 0) or 0
    hallazgos["HOST_CPU"] = (_nivel_cpu_ram(cpu_usage), f"Uso CPU: {cpu_usage}%")

    # ram_pct = (tele_host.get('ram') or {}).get('usage_percent', 0) or 0
    # hallazgos["HOST_RAM"] = (_nivel_cpu_ram(ram_pct), f"Uso RAM: {int(ram_pct)}%")

    temp_list = sensors.get('temperatures') or []
    for t in temp_list:
        val = t.get('value', 0)
        name = t.get('name', 'Unknown')
        # Evaluamos según el máximo crítico configurado en DB
        nivel_t = _nivel_temp(val, config['temp_cpu_max'])
        hallazgos[f"TEMP_{name}"] = (nivel_t, f"Temperatura {name}: {val}°C")

    # 2. BOOLEANOS (Todo o nada -> CRITICAL o OK)
    if config['enable_fans']:
        for f in sensors.get('fans', []):
            st = f.get('status', 'OK')
            nivel = "OK" if st == 'OK' else "CRITICAL"
            hallazgos[f"FAN_{f.get('name')}"] = (nivel, f"Fallo Ventilador ({st})")

    if config['enable_power']:
        for p in (sensors.get('power') or {}).get('supplies', []):
            st = p.get('status', 'OK')
            nivel = "OK" if st == 'OK' else "CRITICAL"
            hallazgos[f"PSU_{p.get('name')}"] = (nivel, f"Fallo Fuente ({st})")

    if config.get('enable_raid', True):
        for ld in storage_layer.get('logical_volumes', []):
            st = ld.get('status', 'OK')
            nivel = "OK" if st in ['OK', 'Online'] else "CRITICAL"
            hallazgos[f"RAID_VOL_{ld.get('name')}"] = (nivel, f"Fallo Volumen: {st}")
            
        for pd in storage_layer.get('physical_drives', []):
            st = pd.get('status', 'OK')
            nivel = "OK" if st in ['OK', 'Online'] else "CRITICAL"
            hallazgos[f"RAID_DISK_{pd.get('slot')}"] = (nivel, f"Fallo Disco {pd.get('slot')}: {st}")

    # 3. VMs
    for resource in v_layer:
        if resource.get('state') not in ['Online', 'Running']: continue
        rid = resource.get('id', 'Unknown')
        r_tele = resource.get('telemetry') or {}
        
        vm_cpu = (r_tele.get('cpu') or {}).get('usage_percent', 0)
        hallazgos[f"VM_CPU_{rid}"] = (_nivel_cpu_ram(vm_cpu), f"CPU en {rid}: {vm_cpu}%")

        vm_ram = (r_tele.get('ram') or {}).get('usage_percent', 0)
        hallazgos[f"VM_RAM_{rid}"] = (_nivel_cpu_ram(vm_ram), f"RAM en {rid}: {vm_ram}%")

        for disk in resource.get('storage', []):
            mount = disk.get('mount_point', 'Unknown')
            usage = disk.get('usage_percent', 0)
            hallazgos[f"DISK_{rid}_{mount}"] = (_nivel_disco(usage), f"Disco Lleno en {rid} ({mount}): {usage}%")

    # 4. ENVIAR AL GESTOR INTELIGENTE
    for tipo_unico, (nivel, msg) in hallazgos.items():
        actualizar_estado_alerta(db, hid, tipo_unico, nivel, msg, asana_proj_id)

# --- GESTOR INTELIGENTE DE INCIDENTES V3 ---
def actualizar_estado_alerta(db, hid, tipo_unico, nivel, mensaje, asana_proj_id=None):
    ahora = datetime.now()
    DIAS_CADUCIDAD = 15

    # Obtener la última alerta
    alerta = db.query(database.AlertaModel).filter(
        database.AlertaModel.hospital_id == hid,
        database.AlertaModel.tipo == tipo_unico
    ).order_by(database.AlertaModel.id.desc()).first()

    # CASO A: PARAMETRO NORMALIZADO (OK)
    if nivel == "OK":
        if alerta and alerta.is_active == 1:
            print(f"✅ NORMALIZADO: {hid} -> {tipo_unico}")
            if alerta.asana_task_gid and asana_conector:
                asana_conector.cerrar_tarea_asana(alerta.asana_task_gid, hid, tipo_unico, ahora)
            alerta.end_time = ahora
            alerta.is_active = 0
            alerta.mensaje = f"[OK] Normalizado: {mensaje}"
            db.commit()
        return

    # CASO B: FALLO DETECTADO (NOTICE, WARNING, CRITICAL)
    if not alerta:
        # B1: Nunca existió
        print(f"⚠️ NUEVA ALERTA: {hid} -> {tipo_unico} ({nivel})")
        gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id) if asana_conector else None
        nueva = database.AlertaModel(hospital_id=hid, tipo=tipo_unico, mensaje=f"[{nivel}] {mensaje}", start_time=ahora, is_active=1, asana_task_gid=gid)
        db.add(nueva)
        db.commit()

    elif alerta.is_active == 1:
        # B2: Ya estaba abierta. Extraemos nivel guardado tolerando alertas viejas sin corchetes
        nivel_db = "DESCONOCIDO"
        if alerta.mensaje and str(alerta.mensaje).startswith("["):
            nivel_db = str(alerta.mensaje).split("]")[0].replace("[", "")

        nuevo_mensaje = f"[{nivel}] {mensaje}"

        # 1. ¿Cambió la gravedad? Solo si es distinto avisamos a Asana
        if nivel_db != nivel:
            print(f"🛡️ CAMBIO DE GRAVEDAD CONFIRMADO: {hid} -> {tipo_unico} (De {nivel_db} a {nivel})")
            if alerta.asana_task_gid and asana_conector:
                asana_conector.actualizar_tarea_asana(alerta.asana_task_gid, hid, tipo_unico, nivel, mensaje, reabrir=False)
        
        # 2. Guardado en DB silencioso (actualiza decimales y minutos sin tocar Asana)
        if str(alerta.mensaje) != nuevo_mensaje:
            alerta.mensaje = nuevo_mensaje
            db.commit()

    elif alerta.is_active == 0:
        # B3: Estaba cerrada. Amnesia de 15 días
        if alerta.end_time and (ahora - alerta.end_time).days <= DIAS_CADUCIDAD:
            print(f"♻️ REINCIDENCIA (Reabriendo): {hid} -> {tipo_unico} ({nivel})")
            if alerta.asana_task_gid and asana_conector:
                asana_conector.actualizar_tarea_asana(alerta.asana_task_gid, hid, tipo_unico, nivel, mensaje, reabrir=True)
            alerta.is_active = 1
            alerta.end_time = None
            alerta.start_time = ahora
            alerta.mensaje = f"[{nivel}] {mensaje}"
            db.commit()
        else:
            print(f"⚠️ NUEVA ALERTA (Caducidad superada): {hid} -> {tipo_unico}")
            gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id) if asana_conector else None
            nueva = database.AlertaModel(hospital_id=hid, tipo=tipo_unico, mensaje=f"[{nivel}] {mensaje}", start_time=ahora, is_active=1, asana_task_gid=gid)
            db.add(nueva)
            db.commit()

            
# --- CHECK OFFLINE ---
def _verificar_conectividad(db, config):
    limit_min = config['offline_minutes']
    limit_delta = timedelta(minutes=limit_min)
    ahora = datetime.now()
    
    hospitales_meta = db.query(database.HospitalMetadata).filter_by(alerts_enabled=True).all()
    
    for meta in hospitales_meta:
        last_report = db.execute(
            text("SELECT timestamp FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1"),
            {"hid": meta.hospital_id}
        ).fetchone()

        nivel = "CRITICAL" # Offline no tiene Warning
        last_seen = None

        if last_report:
            ts_val = last_report.timestamp
            if isinstance(ts_val, str):
                try: ts_val = datetime.fromisoformat(ts_val)
                except: pass
            
            if isinstance(ts_val, datetime):
                last_seen = ts_val
                if (ahora - last_seen) <= limit_delta:
                    nivel = "OK"
        
        # --- BLOQUEO DE FALSOS POSITIVOS ---
        # Si el hospital nunca se conectó (no hay last_seen), ignoramos la alerta.
        # Esto evita el spam masivo de "Sin conexión registrada." en nodos nuevos.
        if not last_seen:
            continue
            
        msg = f"Sin conexión hace {int((ahora - last_seen).total_seconds()/60)} min."
        actualizar_estado_alerta(db, meta.hospital_id, "OFFLINE", nivel, msg, meta.asana_project_id)


