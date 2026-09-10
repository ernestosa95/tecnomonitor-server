"""
El orquestador: arma qué detectores corren en cada tick, en qué orden, y con
qué config/followers. Es el único archivo que server.py invoca directamente
(vía __init__.py). Para agregar un detector nuevo, se lo suma acá -- el
resto del motor (config, exclusiones, gestor de incidentes) ya lo atraviesa
automáticamente sin cambios.

Ver docs/09-plan-refactor-alertas.md.
"""
from datetime import datetime

import database

from . import exclusiones as _exclusiones_mod
from . import infra
from .config import _followers_de, cargar_config
from .exclusiones import cargar_exclusiones
from .kpis_negocio import mamo as kpi_mamo
from .kpis_negocio import ris as kpi_ris
from .software import dicom_autoenrute, mirth

# Variable global para registrar la última ejecución de KPIs (el chequeo
# diario no debe repetirse dos veces el mismo día).
ultima_ejecucion_kpis = None


def procesar_offline(db):
    config = cargar_config(db)
    cargar_exclusiones(db)

    # --- IDs de Asana Globales (Infraestructura) ---
    global_asana_followers = _followers_de(db, config, 'global_alert_responsible_email')
    if not global_asana_followers:
        print("ℹ️ [Vigilancia] Sin followers globales resueltos "
              "(revisá 'Responsables Asana (Infraestructura)' y que esos usuarios tengan asana_id).")

    # OFFLINE aislado: si falla, no arrastra al resto del tick
    try:
        infra._verificar_conectividad(db, config, global_asana_followers)
    except Exception as e:
        print(f"⚠️ [Vigilancia] Falló _verificar_conectividad: {repr(e)}")

    # --- Detector de infraestructura (CPU/RAM/temp/fans/PSU/RAID/latencia) ---
    contadores = {"total": 0, "evaluados": 0, "omitidos": 0, "con_error": 0, "total_hallazgos": 0}
    try:
        contadores = infra.verificar_infra_hospitales(db, config, global_asana_followers)
    except Exception as e:
        # Esto ahora SOLO salta por fallos de carga (query/lote), no por un hospital puntual
        print(f"❌ [Vigilancia] Error de carga en procesar_offline: {repr(e)}")

    # --- HEALTH-CHECK DEL TICK ---
    try:
        activas_ahora = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1).count()
    except Exception:
        activas_ahora = -1

    print(
        f"🩺 [Vigilancia] Tick | hospitales={contadores['total']} evaluados={contadores['evaluados']} "
        f"omitidos={contadores['omitidos']} con_error={contadores['con_error']} "
        f"hallazgos_no_ok={contadores['total_hallazgos']} alertas_activas={activas_ahora} "
        f"reglas_exclusion={len(_exclusiones_mod._EXCLUSIONES_CACHE)}"
    )

    # 👇 EJECUCIÓN DEL MÓDULO DE SOFTWARE (Mirth + DICOM) 👇
    print("🔍 [Software] Iniciando evaluación de Mirth y DICOM...")
    try:
        verificar_estado_software(db)
    except Exception as e:
        print(f"❌ [Software] Error crítico en el módulo: {e}")


def verificar_kpis_programados(db):
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

            # --- Detectores de KPIs de negocio. Para agregar uno nuevo: un
            # archivo corto en kpis_negocio/ (ver ris.py/mamo.py) + una línea acá. ---
            kpi_ris.verificar(db)
            kpi_mamo.verificar(db)


def verificar_estado_software(db):
    """
    Punto de entrada del tick de software. Firma sin cambios respecto de la
    versión anterior, para no tocar el scheduler que la invoca.

    Cada módulo tiene su propio switch de config: apagar Mirth no apaga el
    autoenrute, y viceversa.
    """
    config = cargar_config(db)

    hospitales_activos = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True,
        database.HospitalMetadata.alerts_enabled == True
    ).all()

    if config.get('mirth_alert_enabled'):
        try:
            mirth.verificar_mirth(db, config, hospitales_activos)
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"❌ [Software] Falló la verificación de Mirth: {repr(e)}")

    if config.get('dicom_alert_enabled'):
        try:
            dicom_autoenrute.verificar_autoenrute_dicom(db, config, hospitales_activos)
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            print(f"❌ [Software] Falló la verificación de autoenrute DICOM: {repr(e)}")
