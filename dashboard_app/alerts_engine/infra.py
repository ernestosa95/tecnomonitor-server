"""
Detector de infraestructura: CPU/RAM/temperatura/fans/PSU/RAID/latencia de
red/uptime del host físico y de las VMs, más la conectividad (offline).

Es el detector más grande porque evalúa MUCHOS hallazgos a partir de un solo
reporte -- agregar una métrica nueva DENTRO de un reporte que ya se procesa
(ej. un sensor nuevo) es agregar unas pocas líneas acá, no requiere ningún
archivo nuevo. Ver docs/09-plan-refactor-alertas.md §2 (caso A vs B).

Ver docs/09-plan-refactor-alertas.md para el resto de la reorganización.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import text

import database

from .config import _followers_de, cargar_config
from .estado import _parsear_timestamp, actualizar_estado_alerta

# NOTA: constante histórica sin uso actual. Los umbrales de latencia reales
# están hardcodeados en _evaluar_reglas_v3 (200 ms NOTICE / 500 ms CRITICAL).
# Se conserva por compatibilidad con posibles imports externos.
UMBRAL_LATENCIA_MS = 50.0


# =====================================================================
# PUNTOS DE ENTRADA
# =====================================================================

def analizar_reporte(hospital_id, json_data_v3, db):
    """
    Camino de INGESTA: se dispara al recibir un reporte del agente.

    🛠️ FIX BUG 4 (el más grave): esta función llamaba a _evaluar_reglas_v3()
    con 5 argumentos cuando la firma exige 6 (faltaba asana_followers).
    Resultado: TypeError en CADA reporte entrante -> analizar_reporte()
    nunca ejecutó una sola regla. Las alertas de infra que se veían las
    generaba exclusivamente procesar_offline() en su tick periódico.

    Nota: al momento de este refactor, nada en el repo llama a esta función
    (ni main.py ni ningún router) -- queda documentado tal cual, sin tocar
    comportamiento, igual que el resto del archivo.
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


def verificar_infra_hospitales(db, config, global_asana_followers):
    """
    Recorre el último reporte de cada hospital y corre _evaluar_reglas_v3.
    Extraído del loop que antes vivía inline en procesar_offline(), para que
    el detector de infra sea autocontenido igual que los demás (Mirth,
    DICOM, KPIs). Devuelve los contadores del tick para el health-check que
    imprime el orquestador.
    """
    total = evaluados = omitidos = con_error = total_hallazgos = 0

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

    return {
        "total": total, "evaluados": evaluados, "omitidos": omitidos,
        "con_error": con_error, "total_hallazgos": total_hallazgos,
    }


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
