"""
Configuración del motor de alertas: carga desde la base, defaults de KPIs
granulares por hospital, y resolución de responsables (email -> Asana ID).

Primer módulo del paquete alerts_engine/ -- ver
docs/09-plan-refactor-alertas.md para el detalle completo de la reorganización.
"""
import json

import database


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


def cargar_config(db):
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
        # DEPRECADO desde el mapa de integraciones (ver
        # dashboard_app/alerts_engine/software/mirth.py y
        # docs/13-contrato-topologia-mirth.md): el detector ya no lo lee, lo
        # reemplazan los 6 umbrales por criticidad de abajo. Se deja la
        # clave (con su default histórico) por si algo del panel viejo
        # todavía la muestra, hasta sacarla del formulario.
        "mirth_queued_threshold": g("mirth_queued_threshold", 100),
        # Umbrales de cola por criticidad de canal (alta/media/baja), mismos
        # defaults que el prototipo del mapa de integraciones. `crit` es el
        # que dispara CRITICAL (mismo criterio "todo o nada por umbral" que
        # ya tenía `mirth_queued_threshold`); `warn` solo pinta el mapa a
        # menos que se prenda `mirth_queue_warning_alert_enabled`.
        "mirth_queue_warn_alta": g("mirth_queue_warn_alta", 20),
        "mirth_queue_crit_alta": g("mirth_queue_crit_alta", 80),
        "mirth_queue_warn_media": g("mirth_queue_warn_media", 60),
        "mirth_queue_crit_media": g("mirth_queue_crit_media", 200),
        "mirth_queue_warn_baja": g("mirth_queue_warn_baja", 150),
        "mirth_queue_crit_baja": g("mirth_queue_crit_baja", 400),
        # Criticidad que se le asume a un canal que todavía no fue
        # clasificado en el panel de administración del mapa.
        "mirth_crit_default": g("mirth_crit_default", "media"),
        # Minutos sin una fila nueva de un canal para considerarlo "sin
        # datos frescos" en el mapa (no dispara alerta, es solo visual).
        "mirth_stale_minutes": g("mirth_stale_minutes", 15),
        # Apagado por default: si se prende, cruzar el umbral `warn` (no solo
        # `crit`) también genera un ticket de Asana en WARNING.
        "mirth_queue_warning_alert_enabled": g("mirth_queue_warning_alert_enabled", False, is_bool=True),
        "mirth_responsible_email": g("mirth_responsible_email", ""),

        # --- CONFIGURACIONES DE AUTOENRUTE DICOM ---
        # La regla NO usa umbral absoluto de instancias: detecta que la cola
        # no drena. Ver software/dicom_autoenrute.py para el detalle.
        #
        # dicom_min_instances es un piso de ruido, no un umbral de alerta:
        # colas chicas trabadas (5 instancias) no ameritan despertar a nadie.
        "dicom_alert_enabled": g("dicom_alert_enabled", False, is_bool=True),
        # 45 min y no 30: con ventanas cortas la ventana puede caer entera
        # sobre la pendiente de subida de un serrucho sano y disparar un
        # falso positivo. Ver la nota de calibración en dicom_autoenrute.py.
        "dicom_stall_warning_minutes": g("dicom_stall_warning_minutes", 45),
        "dicom_stall_critical_minutes": g("dicom_stall_critical_minutes", 120),
        "dicom_min_instances": g("dicom_min_instances", 1000),
        # Porcentaje (entero) al que tiene que caer la cola dentro de la ventana
        # para considerarla "drenando". 70 => bajó al menos al 70% del valor
        # con el que arrancó la ventana en algún momento.
        "dicom_drain_percent": g("dicom_drain_percent", 90),
        # Piso habitual por regla (baseline adaptativo). Sin UI a propósito: es un
        # interruptor de emergencia. Apagar: INSERT OR REPLACE INTO configuracion
        # (clave, valor) VALUES ('dicom_baseline_enabled', '0'). Ver docs/12 §3quater.
        "dicom_baseline_enabled": g("dicom_baseline_enabled", True, is_bool=True),
        "dicom_responsible_email": g("dicom_responsible_email", ""),
    }


def _followers_de(db, config, clave='global_alert_responsible_email'):
    """
    Traduce una lista de emails guardada en config (CSV) a IDs de Asana.
    Centralizado para que todos los detectores resuelvan followers de la
    misma manera.
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
