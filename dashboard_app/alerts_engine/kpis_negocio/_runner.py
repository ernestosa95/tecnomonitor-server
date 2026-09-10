"""
Runner compartido para KPIs de negocio de "inactividad" (cero producción de
cierta modalidad en una ventana de tiempo). RIS y Mamografía son el mismo
algoritmo con distintos parámetros -- ver ris.py y mamo.py, que son archivos
cortos que solo declaran esos parámetros y llaman a `verificar_kpi_inactividad`.

Un tercer KPI de este mismo tipo (ej. "cero estudios de TC en 12 horas") es
un archivo nuevo igual de corto, no una función copiada. Ver
docs/09-plan-refactor-alertas.md §4.
"""
import json
from datetime import datetime, timedelta

import database

from ..config import _followers_de, _kpi_habilitado
from ..estado import actualizar_estado_alerta
from ..estado import asana_conector  # misma instancia que ya resolvió estado.py


def verificar_kpi_inactividad(db, *, tipo_unico, kpi_settings_key,
                               enabled, umbral, unidad,
                               modalidades_target, responsable_config_key,
                               config, construir_mensaje):
    """
    enabled: bool ya resuelto por el caller (config.get(enabled_config_key)).
    umbral: número ya resuelto (horas o días, según `unidad`).
    unidad: "horas" | "dias" -- determina el timedelta de la ventana.
    modalidades_target: lista de strings en mayúscula a buscar dentro del
        campo 'mod' de cada item de application_metrics.ris.
    construir_mensaje: función(modalidades_target, umbral) -> str, se llama
        solo cuando el KPI dispara (total == 0).
    """
    if not enabled:
        return

    print(f"📊 Verificando KPI de inactividad: {tipo_unico}...")

    followers = _followers_de(db, config, responsable_config_key)

    if unidad == "dias":
        fecha_limite = datetime.now() - timedelta(days=umbral)
    else:
        fecha_limite = datetime.now() - timedelta(hours=umbral)

    hospitales_ris = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True,
        database.HospitalMetadata.alerts_enabled == True,
        database.HospitalMetadata.has_ris == True
    ).all()

    for hosp in hospitales_ris:
        if not _kpi_habilitado(hosp, kpi_settings_key):
            continue

        reportes = db.query(database.ReporteUso).filter(
            database.ReporteUso.hospital_id == hosp.hospital_id,
            database.ReporteUso.timestamp >= fecha_limite
        ).all()

        total = 0
        for rep in reportes:
            if not rep.kpi_json_data:
                continue
            try:
                metrics = json.loads(rep.kpi_json_data)
                for item in metrics.get('ris', []):
                    mod_reportada = str(item.get('mod', '')).upper()
                    if any(m in mod_reportada for m in modalidades_target):
                        total += item.get('admitidos', 0)
            except Exception:
                continue

        if total == 0:
            mensaje = construir_mensaje(modalidades_target, umbral)
            print(f"⚠️ ALERTA KPI: {tipo_unico} en {hosp.hospital_id}. Generando ticket...")
            _crear_alerta_kpi_generica(db, hosp, tipo_unico, mensaje, followers)
        else:
            # Si hay admisiones, llamamos a actualizar_estado_alerta para que la CIERRE si estaba abierta
            actualizar_estado_alerta(db, hosp.hospital_id, tipo_unico, "OK", "Producción reanudada", hosp.asana_project_id, followers)


def _crear_alerta_kpi_generica(db, hosp, tipo, mensaje, followers):
    # Verificamos si ya existe para no duplicar
    existe = db.query(database.AlertaModel).filter_by(hospital_id=hosp.hospital_id, tipo=tipo, is_active=1).first()
    if not existe:
        gid = asana_conector.crear_tarea_alerta(hosp.hospital_id, tipo, "WARNING", mensaje, hosp.asana_project_id, extra_followers=followers) if asana_conector else None
        nueva = database.AlertaModel(hospital_id=hosp.hospital_id, tipo=tipo, mensaje=f"[KPI] {mensaje}", start_time=datetime.now(), is_active=1, asana_task_gid=gid)
        db.add(nueva)
        db.commit()
