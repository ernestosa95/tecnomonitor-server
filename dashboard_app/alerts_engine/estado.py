"""
El gestor de incidentes: el único punto por el que cualquier detector (viejo
o nuevo) reporta un hallazgo. Decide si hay que crear/cerrar/reabrir una
alerta y su tarea de Asana correspondiente. Ningún detector debería tocar
la tabla `alertas` directamente -- todos pasan por acá.

Ver docs/09-plan-refactor-alertas.md.
"""
from datetime import datetime

import requests

import database

from .exclusiones import evaluar_exclusion, _registrar_hit

try:
    import asana_conector
except ImportError as e:
    print(f"⚠️ No se pudo cargar asana_conector: {e}")
    asana_conector = None

_ORDEN_NIVEL = {"OK": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}


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


# --- GESTOR INTELIGENTE DE INCIDENTES V3 ---
def actualizar_estado_alerta(db, hid, tipo_unico, nivel, mensaje, asana_proj_id=None, asana_followers=None):
    ahora = datetime.now()
    DIAS_CADUCIDAD = 15

    # Obtener la última alerta
    alerta = db.query(database.AlertaModel).filter(
        database.AlertaModel.hospital_id == hid,
        database.AlertaModel.tipo == tipo_unico
    ).order_by(database.AlertaModel.id.desc()).first()

    # --- CASO 0: EXCLUSIÓN ---
    regla = evaluar_exclusion(hid, tipo_unico, nivel)
    if regla:
        _registrar_hit(db, regla["id"])

        # Si venía una alerta abierta de antes, se cierra: dejarla colgada
        # significaría un ticket de Asana vivo para siempre.
        if alerta and alerta.is_active == 1:
            print(f"🔇 EXCLUIDA (cierre): {hid} -> {tipo_unico} [regla #{regla['id']}]")
            if alerta.asana_task_gid and asana_conector:
                asana_conector.cerrar_tarea_asana(alerta.asana_task_gid, hid, tipo_unico, ahora)
            alerta.end_time = ahora
            alerta.is_active = 0
            alerta.mensaje = f"[EXCLUIDA] Regla #{regla['id']}: {mensaje}"
            db.commit()
            try:
                requests.post("http://127.0.0.1:8001/api/internal/trigger-ws", timeout=1)
            except Exception:
                pass
            return

        if regla["accion"] == "total":
            return  # ruido cero: ni DB ni Asana

        # accion == "silencioso": queda el registro local, sin ticket
        if not alerta or alerta.is_active == 0:
            nueva = database.AlertaModel(
                hospital_id=hid,
                tipo=tipo_unico,
                mensaje=f"[SILENCIADA][{nivel}] {mensaje}",
                start_time=ahora,
                is_active=1,
                asana_task_gid=None,
            )
            db.add(nueva)
            db.commit()
        return

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
