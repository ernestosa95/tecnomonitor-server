import sys
import os
import json
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from sqlalchemy import desc, text
import requests

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
    """
    Carga la configuración global desde la base de datos, 
    manejando tipos booleanos, enteros y cadenas.
    """
    def g(k, d, is_bool=False):
        r = db.query(database.ConfigModel).filter_by(clave=k).first()
        if r:
            if is_bool:
                return r.valor == '1'
            # Si el valor por defecto es entero, intentamos convertir el valor de la DB
            if isinstance(d, int):
                try:
                    return int(r.valor)
                except (ValueError, TypeError):
                    return d
            # En cualquier otro caso (como strings), devolvemos el valor crudo
            return r.valor
        return d

    return {
        # --- Configuración de Infraestructura (Existente) ---
        "offline_minutes": g("offline_minutes", 15),
        "disk_threshold": g("disk_threshold", 90),
        "temp_cpu_max": g("temp_cpu_max", 70),
        "enable_fans": g("enable_fans", True, is_bool=True),
        "enable_power": g("enable_power", True, is_bool=True),
        "enable_raid": g("enable_raid", True, is_bool=True),
        
        # --- Configuración de KPIs de Negocio (Nuevos) ---
        "kpi_rad_alert_enabled": g("kpi_rad_alert_enabled", False, is_bool=True),
        "kpi_rad_threshold_hours": g("kpi_rad_threshold_hours", 24),
        "kpi_rad_modalities": g("kpi_rad_modalities", "DX,CR,MAMO"),
        "kpi_rad_responsible_email": g("kpi_rad_responsible_email", "")
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
        # =========================================================
        # 🛡️ FIX BATCH: CARGA EN LOTE PARA EVITAR N+1 QUERIES
        # =========================================================
        # 1. Traemos TODA la metadata de una sola vez (1 query)
        toda_la_metadata = db.query(database.HospitalMetadata).all()
        
        # 2. Armamos un Diccionario para búsqueda instantánea en RAM
        # Estructura: {"H01": <Objeto Metadata>, "H02": <Objeto Metadata>}
        meta_dict = {meta.hospital_id: meta for meta in toda_la_metadata}

        # 3. Traemos el último reporte de cada hospital (1 query)
        query = text("""
            SELECT h.hospital_id, h.full_json_data 
            FROM reportes_historicos h
            INNER JOIN (SELECT hospital_id, MAX(timestamp) as max_t FROM reportes_historicos GROUP BY hospital_id) max_h 
            ON h.hospital_id = max_h.hospital_id AND h.timestamp = max_h.max_t
        """)
        reportes = db.execute(query).fetchall()
        
        # 4. El bucle ahora es instantáneo, lee de la RAM (meta_dict) en vez de la DB
        for row in reportes:
            meta = meta_dict.get(row.hospital_id)
            
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

            try:
                requests.post("http://127.0.0.1:8001/api/internal/trigger-ws", timeout=1)
            except:
                pass
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

        # 🛟 FIX SALVAVIDAS B2: Si la alerta está activa pero nunca se creó en Asana (falló en el pasado)
        if not alerta.asana_task_gid and asana_conector:
            print(f"⚠️ ALERTA ACTIVA SIN TAREA PREVIA: Creando nueva tarea en Asana para {hid}...")
            nuevo_gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id)
            alerta.asana_task_gid = nuevo_gid
            db.commit()
            
        # 1. ¿Cambió la gravedad? Solo si es distinto avisamos a Asana
        elif nivel_db != nivel:
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
                # Flujo normal: Reabre la tarea existente
                asana_conector.actualizar_tarea_asana(alerta.asana_task_gid, hid, tipo_unico, nivel, mensaje, reabrir=True)
            elif asana_conector:
                # 🛟 FIX SALVAVIDAS: Si no hay tarea previa válida, creamos una nueva
                print(f"⚠️ REINCIDENCIA SIN TAREA PREVIA: Creando nueva tarea en Asana para {hid}...")
                nuevo_gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id)
                alerta.asana_task_gid = nuevo_gid
                
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

def verificar_actividad_ris(db: Session):
    config = cargar_config(db)
    
    # 1. ¿Está habilitada la alerta?
    if not config.get('kpi_rad_alert_enabled'):
        return

    print("📊 Iniciando verificación de KPIs de Negocio (Inactividad RIS)...")
    
    umbral_horas = config.get('kpi_rad_threshold_hours', 24)
    modalidades_target = [m.strip().upper() for m in config.get('kpi_rad_modalities', 'DX,CR').split(',')]
    emails_responsables = [e.strip() for e in config.get('kpi_rad_responsible_email', '').split(',') if e.strip()]
    
    # 2. Buscar los IDs de Asana de los responsables seleccionados
    asana_followers = []
    if emails_responsables:
        usuarios = db.query(database.UserModel).filter(database.UserModel.email.in_(emails_responsables)).all()
        asana_followers = [u.asana_id for u in usuarios if u.asana_id]

    fecha_limite = datetime.now() - timedelta(hours=umbral_horas)
    
    # 3. Obtener hospitales que tienen RIS activado
    hospitales_ris = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True,
        database.HospitalMetadata.alerts_enabled == True,
        database.HospitalMetadata.has_ris == True
    ).all()

    for hosp in hospitales_ris:
        # Buscar los reportes de uso en el periodo definido
        reportes = db.query(database.ReporteUso).filter(
            database.ReporteUso.hospital_id == hosp.hospital_id,
            database.ReporteUso.timestamp >= fecha_limite
        ).all()

        total_admitidos = 0
        
        # Parsear los JSONs para contar los admitidos en las modalidades objetivo
        for rep in reportes:
            if not rep.kpi_json_data:
                continue
            try:
                metrics = json.loads(rep.kpi_json_data)
                for item in metrics.get('ris', []):
                    if item.get('mod') in modalidades_target:
                        total_admitidos += item.get('admitidos', 0)
            except Exception as e:
                continue

        # 4. Disparar alerta si no hay actividad
        if total_admitidos == 0:
            mensaje = f"Cero (0) admisiones registradas en las modalidades {', '.join(modalidades_target)} durante las últimas {umbral_horas} horas."
            
            # Para las alertas de KPI, usamos actualizar_estado_alerta pero le inyectamos los followers
            # Temporalmente seteamos los followers en la variable global o se los pasamos a la función
            # Nota: Necesitaremos un pequeño ajuste en asana_conector para recibir followers específicos, 
            # o directamente crear la tarea aquí si queremos aislar la lógica.
            
            print(f"⚠️ ALERTA KPI: Inactividad en {hosp.hospital_id}. Generando ticket...")
            
            # Verificamos si ya hay una alerta activa de este tipo para no duplicar
            alerta_existente = db.query(database.AlertaModel).filter(
                database.AlertaModel.hospital_id == hosp.hospital_id,
                database.AlertaModel.tipo == "KPI_INACT_RAD",
                database.AlertaModel.is_active == 1
            ).first()
            
            if not alerta_existente:
                # Disparamos a Asana (asumiendo que asana_conector tiene una función para esto)
                gid = None
                if asana_conector:
                    gid = asana_conector.crear_tarea_alerta(
                        hospital_id=hosp.hospital_id, 
                        tipo="KPI_INACT_RAD", 
                        nivel="WARNING", 
                        mensaje_detalle=mensaje, 
                        hospital_project_gid=hosp.asana_project_id,
                        extra_followers=asana_followers # <-- Pasamos los IDs recuperados
                    )
                
                # Guardamos en la BD local
                nueva_alerta = database.AlertaModel(
                    hospital_id=hosp.hospital_id, 
                    tipo="KPI_INACT_RAD", 
                    mensaje=f"[WARNING] {mensaje}", 
                    start_time=datetime.now(), 
                    is_active=1, 
                    asana_task_gid=gid
                )
                db.add(nueva_alerta)
                db.commit()


