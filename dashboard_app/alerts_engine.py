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

# NOTA: constante histórica sin uso actual. Los umbrales de latencia reales
# están hardcodeados en _evaluar_reglas_v3 (200 ms NOTICE / 500 ms CRITICAL).
# Se conserva por compatibilidad con posibles imports externos.
UMBRAL_LATENCIA_MS = 50.0


# =====================================================================
# DEFAULTS DE KPIs GRANULARES POR HOSPITAL
# ---------------------------------------------------------------------
# 🛠️ FIX BUG 3: Estos valores DEBEN ser idénticos a los que devuelve
# dashboard.get_kpi_settings() y a los que asume script.js en el modal
# (`prefs.KPI_INACT_RAD ?? true` / `prefs.KPI_INACT_MAMO ?? false`).
#
# Antes el motor usaba defaults invertidos respecto de la UI:
#   - MAMO: motor asumía True, la UI mostraba el switch APAGADO
#     -> el ticket se generaba igual aunque vos lo vieras desactivado.
#   - RAD:  motor asumía False, la UI mostraba el switch ENCENDIDO
#     -> la alerta nunca disparaba aunque vos la vieras activa.
#
# ⚠️ IMPORTANTE AL DESPLEGAR: al alinear RAD en True, los hospitales con
# has_ris=True y kpi_settings vacío ('{}') EMPIEZAN a generar tickets
# KPI_INACT_RAD que hoy no generan. Corré primero un backfill que escriba
# el JSON explícito en todos los hospitales existentes; así el default deja
# de importar y no hay sorpresas.
# =====================================================================
KPI_DEFAULTS = {
    "KPI_INACT_RAD": True,
    "KPI_INACT_MAMO": False,
}


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
        # --- Configuración de Infraestructura ---
        "offline_minutes": g("offline_minutes", 15),
        "disk_threshold": g("disk_threshold", 90),
        "temp_amb_max": g("temp_amb_max", 27),
        "temp_cpu_max": g("temp_cpu_max", 70),
        "cpu_host_max": g("cpu_host_max", 85),
        "ram_host_max": g("ram_host_max", 95),
        "cpu_vm_max": g("cpu_vm_max", 90),
        "ram_vm_max": g("ram_vm_max", 90),
        "enable_fans": g("enable_fans", True, is_bool=True),
        "enable_power": g("enable_power", True, is_bool=True),
        "enable_raid": g("enable_raid", True, is_bool=True),

        # 🛠️ FIX BUG 2: esta clave NO se cargaba. El switch "Latencia de Red"
        # se guardaba bien en la DB y la UI lo leía bien, pero el motor jamás
        # lo veía (config.get() devolvía siempre el default).
        "enable_network_latency": g("enable_network_latency", True, is_bool=True),

        # 🛠️ FIX BUG 1: esta clave NO se cargaba. procesar_offline() hacía
        # config.get('global_alert_responsible_email', '') -> siempre ''
        # -> lista de followers vacía -> Asana recibía followers: []
        # aunque en el panel tuvieras responsables configurados.
        "global_alert_responsible_email": g("global_alert_responsible_email", ""),

        # --- Configuración de KPIs de Negocio ---
        "kpi_execution_time": g("kpi_execution_time", "08:00"),
        "kpi_rad_alert_enabled": g("kpi_rad_alert_enabled", False, is_bool=True),
        "kpi_rad_threshold_hours": g("kpi_rad_threshold_hours", 24),
        "kpi_rad_modalities": g("kpi_rad_modalities", "DX,CR,MAMO"),
        "kpi_rad_responsible_email": g("kpi_rad_responsible_email", ""),
        "kpi_mamo_alert_enabled": g("kpi_mamo_alert_enabled", False, is_bool=True),
        "kpi_mamo_threshold_days": g("kpi_mamo_threshold_days", 7),
        "kpi_mamo_responsible_email": g("kpi_mamo_responsible_email", ""),

        # --- CONFIGURACIONES DE MIRTH ---
        "mirth_alert_enabled": g("mirth_alert_enabled", False, is_bool=True),
        "mirth_queued_threshold": g("mirth_queued_threshold", 100),
        "mirth_responsible_email": g("mirth_responsible_email", "")
    }


# =====================================================================
# HELPERS DE RESOLUCIÓN DE FOLLOWERS Y PREFERENCIAS
# =====================================================================

def _followers_de(db, config, clave='global_alert_responsible_email'):
    """
    Traduce una lista de emails guardada en config (CSV) a IDs de Asana.
    Centralizado para que todos los caminos (ingesta, tick, KPIs, Mirth)
    resuelvan followers de la misma manera.
    """
    emails = [e.strip() for e in (config.get(clave) or '').split(',') if e.strip()]
    if not emails:
        return []
    usuarios = db.query(database.UserModel).filter(
        database.UserModel.email.in_(emails)
    ).all()
    return [u.asana_id for u in usuarios if u.asana_id]


def _kpi_habilitado(hosp, clave):
    """
    🛠️ FIX BUG 3: única fuente de verdad para leer el JSON granular de KPIs
    de un hospital. Tolera kpi_settings en string (SQLite) o dict, y usa
    KPI_DEFAULTS cuando la clave no está seteada.
    """
    prefs = hosp.kpi_settings or {}
    if isinstance(prefs, str):
        try:
            prefs = json.loads(prefs)
        except Exception:
            prefs = {}
    if not isinstance(prefs, dict):
        prefs = {}
    return bool(prefs.get(clave, KPI_DEFAULTS.get(clave, False)))


# =====================================================================
# PUNTOS DE ENTRADA
# =====================================================================

def analizar_reporte(hospital_id, json_data_v3, db: Session):
    """
    Camino de INGESTA: se dispara al recibir un reporte del agente.

    🛠️ FIX BUG 4 (el más grave): esta función llamaba a _evaluar_reglas_v3()
    con 5 argumentos cuando la firma exige 6 (faltaba asana_followers).
    Resultado: TypeError en CADA reporte entrante -> analizar_reporte()
    nunca ejecutó una sola regla. Las alertas de infra que se veían las
    generaba exclusivamente procesar_offline() en su tick periódico.
    """
    meta = db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first()
    if not meta or not meta.alerts_enabled:
        return

    config = cargar_config(db)
    followers = _followers_de(db, config)  # <-- faltaba por completo
    return _evaluar_reglas_v3(
        hospital_id, json_data_v3, db, config,
        meta.asana_project_id, followers
    )


def procesar_offline(db: Session):
    config = cargar_config(db)

    # --- IDs de Asana Globales (Infraestructura) ---
    global_asana_followers = _followers_de(db, config, 'global_alert_responsible_email')
    if not global_asana_followers:
        print("ℹ️ [Vigilancia] Sin followers globales resueltos "
              "(revisá 'Responsables Asana (Infraestructura)' y que esos usuarios tengan asana_id).")

    # OFFLINE aislado: si falla, no arrastra al resto del tick
    try:
        _verificar_conectividad(db, config, global_asana_followers)
    except Exception as e:
        print(f"⚠️ [Vigilancia] Falló _verificar_conectividad: {repr(e)}")

    # --- Contadores del tick ---
    total = evaluados = omitidos = con_error = total_hallazgos = 0

    try:
        # Carga en lote (evita N+1)
        toda_la_metadata = db.query(database.HospitalMetadata).all()
        meta_dict = {meta.hospital_id: meta for meta in toda_la_metadata}

        query = text("""
            SELECT h.hospital_id, h.full_json_data
            FROM reportes_historicos h
            INNER JOIN (SELECT hospital_id, MAX(timestamp) as max_t FROM reportes_historicos GROUP BY hospital_id) max_h
            ON h.hospital_id = max_h.hospital_id AND h.timestamp = max_h.max_t
        """)
        reportes = db.execute(query).fetchall()
        total = len(reportes)

        for row in reportes:
            meta = meta_dict.get(row.hospital_id)

            if not (meta and meta.alerts_enabled and row.full_json_data):
                omitidos += 1
                continue

            # 🛡️ AISLAMIENTO POR HOSPITAL: un payload roto ya no tumba a los demás
            try:
                data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
                resultado = _evaluar_reglas_v3(
                    row.hospital_id, data, db, config,
                    meta.asana_project_id, global_asana_followers
                )
                if isinstance(resultado, int):
                    total_hallazgos += resultado
                evaluados += 1
            except Exception as e:
                con_error += 1
                # 🔑 Limpia la transacción sucia para que el próximo hospital no herede el error
                try:
                    db.rollback()
                except Exception:
                    pass
                print(f"❌ [Vigilancia] Hospital '{row.hospital_id}' falló y se salteó: {repr(e)}")

    except Exception as e:
        # Esto ahora SOLO salta por fallos de carga (query/lote), no por un hospital puntual
        print(f"❌ [Vigilancia] Error de carga en procesar_offline: {repr(e)}")

    # --- HEALTH-CHECK DEL TICK ---
    try:
        activas_ahora = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1).count()
    except Exception:
        activas_ahora = -1

    print(
        f"🩺 [Vigilancia] Tick | hospitales={total} evaluados={evaluados} "
        f"omitidos={omitidos} con_error={con_error} "
        f"hallazgos_no_ok={total_hallazgos} alertas_activas={activas_ahora}"
    )


# =====================================================================
# HELPERS DE UMBRALES
# =====================================================================

def _nivel_cpu_ram(valor):
    """
    Escalonado fijo 90/85/75 para CPU de host físico.
    ⚠️ PENDIENTE (no tocado en esta pasada): ignora config['cpu_host_max'].
    """
    if valor >= 90: return "CRITICAL"
    if valor >= 85: return "WARNING"
    if valor >= 75: return "NOTICE"
    return "OK"


def _nivel_temp(valor, crit_max):
    if valor >= crit_max: return "CRITICAL"
    if valor >= crit_max - 5: return "WARNING"
    if valor >= crit_max - 10: return "NOTICE"
    return "OK"


def _nivel_ram_host(valor, umbral_critical=95):
    """
    Umbral ÚNICO (no escalonado) para RAM de host físico.
    Motivo: la mayoría de los hospitales opera con RAM alta de forma normal
    (cache de SO), así que el escalonado 75/85/90 generaba falsos positivos
    constantes. Solo alertamos CRITICAL al superar el umbral configurado
    (default 95%). No hay WARNING/NOTICE intermedios a propósito.
    """
    if valor >= umbral_critical:
        return "CRITICAL"
    return "OK"


def _nivel_cpu_ram_configurable(valor, umbral_max):
    """Igual que _nivel_cpu_ram pero usando el umbral configurado en vez de 90 fijo."""
    if valor >= umbral_max:
        return "CRITICAL"
    if valor >= umbral_max - 5:
        return "WARNING"
    if valor >= umbral_max - 10:
        return "NOTICE"
    return "OK"


def _nivel_disco(valor, umbral_critical=90):
    """
    🛠️ FIX: acepta el umbral configurado (config['disk_threshold']) en vez de
    tener 90/85/80 hardcodeado sin relación con el panel (input 'conf-disk').

    🧹 LIMPIEZA: existía una segunda definición _nivel_disco(valor) más arriba
    del archivo, sin parámetro de umbral. Python la pisaba con ésta (la última
    definición gana), así que funcionaba de casualidad. Se eliminó la muerta.
    """
    if valor >= umbral_critical:
        return "CRITICAL"
    if valor >= umbral_critical - 5:
        return "WARNING"
    if valor >= umbral_critical - 10:
        return "NOTICE"
    return "OK"


# =====================================================================
# MOTOR DE REGLAS V3
# =====================================================================

def _evaluar_reglas_v3(hid, data, db, config, asana_proj_id, asana_followers=None):
    # 🛠️ Default defensivo: si algún camino olvida pasar followers, degradamos
    # a lista vacía en vez de reventar con TypeError (ver FIX BUG 4).
    if asana_followers is None:
        asana_followers = []

    hallazgos = {}
    phy = data.get('physical_layer') or {}
    tele_host = phy.get('telemetry') or {}
    sensors = phy.get('sensors') or {}
    storage_layer = phy.get('storage_layer') or {}
    host_info = phy.get('host_info') or {}
    net_health = phy.get('network_health') or {}
    v_layer = data.get('virtual_layer') or []

    # =========================================================
    # 🟢 1. ANÁLOGOS Y MÉTRICAS DE HOST (CPU/RAM/Temp/Uptime)
    # =========================================================
    cpu_usage = (tele_host.get('cpu') or {}).get('usage_percent', 0) or 0
    hallazgos["HOST_CPU"] = (_nivel_cpu_ram(cpu_usage), f"Uso CPU: {cpu_usage}%")

    ram_host = (tele_host.get('ram') or {}).get('usage_percent', 0) or 0
    hallazgos["HOST_RAM"] = (_nivel_ram_host(ram_host, config.get('ram_host_max', 95)), f"Uso RAM: {ram_host}%")

    temp_list = sensors.get('temperatures') or []
    for t in temp_list:
        val = t.get('value', 0)
        name = t.get('name', 'Unknown')
        nivel_t = _nivel_temp(val, config['temp_cpu_max'])
        hallazgos[f"TEMP_{name}"] = (nivel_t, f"Temperatura {name}: {val}°C")

    uptime_host = host_info.get('uptime_seconds') or tele_host.get('uptime_seconds', -1)
    if uptime_host is not None and uptime_host >= 0:
        dias_uptime = uptime_host / 86400.0
        if uptime_host < 600:
            hallazgos["HOST_UPTIME"] = ("WARNING", f"Reinicio reciente/abrupto detectado. Uptime: {int(uptime_host/60)} min")
        else:
            hallazgos["HOST_UPTIME"] = ("OK", f"Uptime estable: {int(dias_uptime)} días")

    # 🛠️ FIX BUG 2: este bloque NO consultaba el switch. Los otros sensores sí
    # (enable_fans / enable_power / enable_raid), pero latencia alertaba siempre.
    #
    # NOTA sobre el apagado: cuando el flag está en False simplemente NO se
    # genera hallazgo, así que las NETWORK_LATENCY que ya estén abiertas NO se
    # auto-cierran (quedan colgadas hasta cierre manual en Asana). Si preferís
    # que apagar el switch cierre las abiertas, hay que emitir "OK" explícito
    # en el else. Decisión pendiente -> por ahora, comportamiento conservador.
    if config.get('enable_network_latency', True):
        latencia_ms = net_health.get('cloud_latency_ms', -1)
        if latencia_ms is not None and latencia_ms >= 0:
            if latencia_ms >= 500:
                hallazgos["NETWORK_LATENCY"] = ("CRITICAL", f"Latencia de red severa: {latencia_ms} ms")
            elif latencia_ms >= 200:
                hallazgos["NETWORK_LATENCY"] = ("NOTICE", f"Saturación/Latencia de red elevada: {latencia_ms} ms")
            else:
                hallazgos["NETWORK_LATENCY"] = ("OK", f"Latencia de red normal: {latencia_ms} ms")

    # =========================================================
    # 🟢 2. BOOLEANOS (Todo o nada -> CRITICAL o OK)
    # =========================================================
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
            hallazgos[f"RAID_VOL_{ld.get('name')}"] = (nivel, f"Volumen RAID '{ld.get('name')}': {st}")

        for pd in storage_layer.get('physical_drives', []):
            st = pd.get('status', 'OK')
            nivel = "OK" if st in ['OK', 'Online'] else "CRITICAL"
            hallazgos[f"RAID_DISK_{pd.get('slot')}"] = (nivel, f"Disco físico (Slot {pd.get('slot')}): {st}")

    # =========================================================
    # 🟢 3. CAPA VIRTUAL (VMs)
    # =========================================================
    cpu_vm_max = config.get('cpu_vm_max', 90)
    ram_vm_max = config.get('ram_vm_max', 90)
    disk_threshold = config.get('disk_threshold', 90)

    for vm in v_layer:
        vm_id = vm.get('id', 'unknown')
        vm_tele = vm.get('telemetry') or {}

        cpu_vm = (vm_tele.get('cpu') or {}).get('usage_percent', 0) or 0
        hallazgos[f"VM_CPU_{vm_id}"] = (
            _nivel_cpu_ram_configurable(cpu_vm, cpu_vm_max),
            f"[{vm_id}] Uso CPU VM: {cpu_vm}%"
        )

        ram_vm = (vm_tele.get('ram') or {}).get('usage_percent', 0) or 0
        hallazgos[f"VM_RAM_{vm_id}"] = (
            _nivel_cpu_ram_configurable(ram_vm, ram_vm_max),
            f"[{vm_id}] Uso RAM VM: {ram_vm}%"
        )

        for disco in (vm.get('storage') or []):
            mount = disco.get('mount_point', 'unknown')
            pct = disco.get('usage_percent', 0) or 0
            hallazgos[f"DISK_{vm_id}_{mount}"] = (
                _nivel_disco(pct, disk_threshold),
                f"[{vm_id}] Disco '{mount}' al {pct}% de uso"
            )

    # =========================================================
    # 🟢 4. PERSISTENCIA
    # =========================================================
    contador = 0
    for tipo_unico, (nivel, mensaje) in hallazgos.items():
        actualizar_estado_alerta(db, hid, tipo_unico, nivel, mensaje, asana_proj_id, asana_followers)
        if nivel != "OK":
            contador += 1

    return contador


# --- GESTOR INTELIGENTE DE INCIDENTES V3 ---
def actualizar_estado_alerta(db, hid, tipo_unico, nivel, mensaje, asana_proj_id=None, asana_followers=None):
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
        gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id, extra_followers=asana_followers) if asana_conector else None
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
            nuevo_gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id, extra_followers=asana_followers)
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
                nuevo_gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id, extra_followers=asana_followers)
                alerta.asana_task_gid = nuevo_gid

            alerta.is_active = 1
            alerta.end_time = None
            alerta.start_time = ahora
            alerta.mensaje = f"[{nivel}] {mensaje}"
            db.commit()
        else:
            print(f"⚠️ NUEVA ALERTA (Caducidad superada): {hid} -> {tipo_unico}")
            gid = asana_conector.crear_tarea_alerta(hid, tipo_unico, nivel, mensaje, asana_proj_id, extra_followers=asana_followers) if asana_conector else None
            nueva = database.AlertaModel(hospital_id=hid, tipo=tipo_unico, mensaje=f"[{nivel}] {mensaje}", start_time=ahora, is_active=1, asana_task_gid=gid)
            db.add(nueva)
            db.commit()


def _parsear_timestamp(ts_val):
    """Convierte un timestamp de la DB a datetime. Devuelve None SOLO si es irrecuperable."""
    if isinstance(ts_val, datetime):
        return ts_val
    if not ts_val:
        return None
    s = str(ts_val).strip()
    if s.endswith("Z"):          # sufijo UTC que fromisoformat no traga en 3.10
        s = s[:-1]
    try:
        return datetime.fromisoformat(s)          # cubre 'T' y espacio, con/sin microsegundos
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:                          # último recurso: recortar zona/microsegundos sobrantes
        return datetime.strptime(s.replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _verificar_conectividad(db, config, asana_followers):
    limit_min = config['offline_minutes']
    limit_delta = timedelta(minutes=limit_min)
    ahora = datetime.now()

    hospitales_meta = db.query(database.HospitalMetadata).filter_by(alerts_enabled=True).all()

    for meta in hospitales_meta:
        last_report = db.execute(
            text("SELECT timestamp FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1"),
            {"hid": meta.hospital_id}
        ).fetchone()

        # CASO 1: nunca reportó -> nodo nuevo legítimo, lo ignoramos (anti falso-positivo)
        if not last_report:
            continue

        last_seen = _parsear_timestamp(last_report.timestamp)

        # CASO 2: HAY reporte pero el timestamp no parsea.
        # Antes esto caía en el mismo bucket que "nunca conectó" y se salteaba EN SILENCIO.
        # Ese era exactamente el agujero: un hospital que SÍ reportaba quedaba sin alerta OFFLINE.
        if last_seen is None:
            print(f"⚠️ [OFFLINE] '{meta.hospital_id}' tiene reporte pero timestamp ilegible: {last_report.timestamp!r}. Se omite este ciclo.")
            continue

        minutos = int((ahora - last_seen).total_seconds() / 60)
        nivel = "OK" if (ahora - last_seen) <= limit_delta else "CRITICAL"
        msg = (f"Sin conexión hace {minutos} min." if nivel == "CRITICAL"
               else f"Conectado (último reporte hace {minutos} min).")

        actualizar_estado_alerta(db, meta.hospital_id, "OFFLINE", nivel, msg, meta.asana_project_id, asana_followers)


# =====================================================================
# KPIs DE NEGOCIO
# =====================================================================

def verificar_actividad_ris(db: Session):
    config = cargar_config(db)

    if not config.get('kpi_rad_alert_enabled'):
        return

    print("📊 Iniciando verificación de KPIs de Negocio (Inactividad RIS)...")

    umbral_horas = config.get('kpi_rad_threshold_hours', 24)
    modalidades_target = [m.strip().upper() for m in config.get('kpi_rad_modalities', 'DX,CR').split(',')]
    asana_followers = _followers_de(db, config, 'kpi_rad_responsible_email')

    fecha_limite = datetime.now() - timedelta(hours=umbral_horas)

    hospitales_ris = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True,
        database.HospitalMetadata.alerts_enabled == True,
        database.HospitalMetadata.has_ris == True
    ).all()

    for hosp in hospitales_ris:
        # 🛠️ FIX BUG 3 (espejo): antes era .get('KPI_INACT_RAD', False), o sea
        # que un hospital sin kpi_settings guardado NUNCA disparaba, aunque la
        # UI mostrara el switch encendido. Ahora ambos usan KPI_DEFAULTS.
        if not _kpi_habilitado(hosp, 'KPI_INACT_RAD'):
            continue

        reportes = db.query(database.ReporteUso).filter(
            database.ReporteUso.hospital_id == hosp.hospital_id,
            database.ReporteUso.timestamp >= fecha_limite
        ).all()

        total_admitidos = 0

        for rep in reportes:
            if not rep.kpi_json_data:
                continue
            try:
                metrics = json.loads(rep.kpi_json_data)
                for item in metrics.get('ris', []):
                    mod_reportada = str(item.get('mod', '')).upper()
                    if any(mod_target in mod_reportada for mod_target in modalidades_target):
                        total_admitidos += item.get('admitidos', 0)
            except Exception:
                continue

        # --- Lógica de Auto-cierre si volvió a tener producción ---
        if total_admitidos == 0:
            mensaje = f"Cero (0) admisiones registradas en las modalidades {', '.join(modalidades_target)} durante las últimas {umbral_horas} horas."
            print(f"⚠️ ALERTA KPI: Inactividad en {hosp.hospital_id}. Generando ticket...")
            _crear_alerta_kpi_generica(db, hosp, "KPI_INACT_RAD", mensaje, asana_followers)
        else:
            # Si hay admisiones, llamamos a actualizar_estado_alerta para que la CIERRE si estaba abierta
            actualizar_estado_alerta(db, hosp.hospital_id, "KPI_INACT_RAD", "OK", "Producción reanudada", hosp.asana_project_id, asana_followers)


# Variable global para registrar la última ejecución
ultima_ejecucion_kpis = None


def verificar_kpis_programados(db: Session):
    global ultima_ejecucion_kpis

    config = cargar_config(db)
    hora_configurada = config.get('kpi_execution_time', '08:00')

    ahora = datetime.now()
    hora_actual_str = ahora.strftime("%H:%M")

    # ¿Es la hora de correr los KPIs?
    if hora_actual_str == hora_configurada:
        fecha_hoy = ahora.strftime("%Y-%m-%d")

        # Verificamos que no se haya ejecutado ya en el día de hoy
        if ultima_ejecucion_kpis != fecha_hoy:
            ultima_ejecucion_kpis = fecha_hoy
            print(f"⏰ Hora programada ({hora_configurada}) alcanzada. Lanzando batería de KPIs...")

            # --- Aquí listamos todas las funciones KPI ---
            verificar_actividad_ris(db)   # Alerta 1
            verificar_actividad_mamo(db)  # Alerta 2
            # verificar_otra_alerta_kpi(db)  <-- Cuando agregues más, irán aquí


def verificar_actividad_mamo(db: Session):
    config = cargar_config(db)
    if not config.get('kpi_mamo_alert_enabled'):
        return

    print("📊 Verificando KPI 2: Inactividad en Mamografía...")
    dias_umbral = config.get('kpi_mamo_threshold_days', 7)

    followers = _followers_de(db, config, 'kpi_mamo_responsible_email')

    fecha_limite = datetime.now() - timedelta(days=dias_umbral)

    hospitales_ris = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True,
        database.HospitalMetadata.alerts_enabled == True,
        database.HospitalMetadata.has_ris == True
    ).all()

    for hosp in hospitales_ris:
        # 🛠️ FIX BUG 3: antes era .get('KPI_INACT_MAMO', True) -> un hospital
        # sin kpi_settings guardado se veía APAGADO en la UI pero el motor lo
        # trataba como ENCENDIDO y levantaba el ticket igual. Éste es el caso
        # exacto que apareció en producción.
        if not _kpi_habilitado(hosp, 'KPI_INACT_MAMO'):
            continue

        reportes = db.query(database.ReporteUso).filter(
            database.ReporteUso.hospital_id == hosp.hospital_id,
            database.ReporteUso.timestamp >= fecha_limite
        ).all()

        total_mamo = 0
        for rep in reportes:
            if not rep.kpi_json_data:
                continue
            try:
                metrics = json.loads(rep.kpi_json_data)
                for item in metrics.get('ris', []):
                    mod_reportada = str(item.get('mod', '')).upper()
                    if any(m in mod_reportada for m in ['MG', 'MAMO']):
                        total_mamo += item.get('admitidos', 0)
            except Exception:
                continue

        if total_mamo == 0:
            mensaje = f"Sin admisiones de Mamografía (MG) en los últimos {dias_umbral} días."
            print(f"⚠️ ALERTA KPI MAMO: Inactividad en {hosp.hospital_id}.")
            _crear_alerta_kpi_generica(db, hosp, "KPI_INACT_MAMO", mensaje, followers)
        else:
            # Auto-cierre
            actualizar_estado_alerta(db, hosp.hospital_id, "KPI_INACT_MAMO", "OK", "Producción reanudada", hosp.asana_project_id, followers)


def _crear_alerta_kpi_generica(db, hosp, tipo, mensaje, followers):
    # Verificamos si ya existe para no duplicar
    existe = db.query(database.AlertaModel).filter_by(hospital_id=hosp.hospital_id, tipo=tipo, is_active=1).first()
    if not existe:
        gid = asana_conector.crear_tarea_alerta(hosp.hospital_id, tipo, "WARNING", mensaje, hosp.asana_project_id, extra_followers=followers) if asana_conector else None
        nueva = database.AlertaModel(hospital_id=hosp.hospital_id, tipo=tipo, mensaje=f"[KPI] {mensaje}", start_time=datetime.now(), is_active=1, asana_task_gid=gid)
        db.add(nueva)
        db.commit()


# =====================================================================
# SOFTWARE / MIRTH
# =====================================================================

def verificar_estado_software(db: Session):
    config = cargar_config(db)

    # 1. Chequeo de encendido
    if not config.get('mirth_alert_enabled'):
        return

    umbral_encolados = config.get('mirth_queued_threshold', 100)
    asana_followers = _followers_de(db, config, 'mirth_responsible_email')

    hospitales_activos = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True,
        database.HospitalMetadata.alerts_enabled == True
    ).all()

    for hosp in hospitales_activos:
        # CORRECCIÓN 1 y 2: LIKE insensible a mayúsculas y ORDER BY explícito
        query = text("""
            WITH RankedData AS (
                SELECT component_id, status_value, metric_value, extra_data,
                       ROW_NUMBER() OVER(PARTITION BY component_id ORDER BY timestamp DESC) as rn
                FROM software_monitoring
                WHERE hospital_id = :hid AND LOWER(app_name) LIKE '%mirth%'
            )
            SELECT component_id, status_value, metric_value, extra_data, rn 
            FROM RankedData 
            WHERE rn <= 2
            ORDER BY component_id, rn ASC
        """)
        registros = db.execute(query, {"hid": hosp.hospital_id}).fetchall()

        historial_canales = {}
        for reg in registros:
            cid = reg.component_id
            if cid not in historial_canales:
                historial_canales[cid] = []
            historial_canales[cid].append(reg)

        for cid, historia in historial_canales.items():
            # CORRECCIÓN 3: Re-aseguramos en Python que [0] es siempre el último reporte (rn=1)
            historia.sort(key=lambda x: x.rn)

            actual = historia[0]
            estado_canal = (actual.status_value or '').upper()

            # CORRECCIÓN 4: Parseo seguro a número entero para evitar el TypeError
            try:
                encolados = int(actual.metric_value or 0)
            except (ValueError, TypeError):
                encolados = 0

            estado_anterior = (historia[1].status_value or '').upper() if len(historia) > 1 else estado_canal

            nivel = "OK"
            mensaje = ""

            if estado_canal in ['STOPPED', 'ERROR', 'PAUSED']:
                if estado_anterior in ['STOPPED', 'ERROR', 'PAUSED']:
                    nivel = "CRITICAL"
                    # Nota: Quitamos el "[CRITICAL]" redundante porque la función actualizar_estado_alerta se lo agrega sola
                    mensaje = f"Canal inoperativo de forma sostenida ({estado_canal})."
                else:
                    # Micro-corte detectado: Esperamos al próximo ciclo
                    continue

            elif encolados >= umbral_encolados:
                nivel = "CRITICAL"
                mensaje = f"Acumulación en canal: {encolados} mensajes encolados (Umbral: {umbral_encolados})."

            else:
                mensaje = f"Operando normal. Encolados: {encolados}"

            tipo_alerta = f"MIRTH_{cid[:35]}"

            actualizar_estado_alerta(
                db=db,
                hid=hosp.hospital_id,
                tipo_unico=tipo_alerta,
                nivel=nivel,
                mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers
            )