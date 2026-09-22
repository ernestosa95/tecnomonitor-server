"""
Detector de integridad de bases SQL Server (DBCC CHECKDB post-reinicio):
alerta CRITICAL si el último chequeo encontró errores reales de corrupción
o asignación en alguna base. Ver tecnomonitor-agent/sql_integrity.py y
docs/PLAN_CHECKDB_POST_REINICIO.md.

Solo alerta por status ERROR -- NOT_ONLINE no dispara ticket a propósito
(una base dada de baja o en mantenimiento no es un problema de integridad).
Decisión del usuario (2026-09-22): sin responsable propio, reusa
'global_alert_responsible_email' (Infraestructura) en vez de un campo nuevo.
"""
import json

from sqlalchemy import text

from ..config import _followers_de
from ..estado import actualizar_estado_alerta


def verificar_integridad_bases(db, config, hospitales_activos):
    asana_followers = _followers_de(db, config)  # default: global_alert_responsible_email

    for hosp in hospitales_activos:
        # Última fila por base (mismo criterio que _ultimo_checkdb en
        # routers/hospital_detalle.py): un reinicio es un evento raro, así
        # que siempre se evalúa el chequeo más reciente, no una ventana.
        filas = db.execute(text("""
            WITH RankedData AS (
                SELECT component_id, status_value, metric_value, extra_data, timestamp,
                       ROW_NUMBER() OVER(PARTITION BY component_id ORDER BY timestamp DESC) as rn
                FROM software_monitoring
                WHERE hospital_id = :hid AND app_name = 'sql_integrity'
            )
            SELECT component_id, status_value, metric_value, extra_data, timestamp
            FROM RankedData WHERE rn = 1
        """), {"hid": hosp.hospital_id}).fetchall()

        for fila in filas:
            db_name = fila.component_id
            estado = (fila.status_value or "").upper()
            try:
                errores = int(fila.metric_value or 0)
            except (ValueError, TypeError):
                errores = 0

            # tipo_unico por (hospital, base) -- estable entre reinicios, así
            # un CHECKDB posterior en OK cierra el ticket en vez de dejarlo
            # huérfano o abrir uno nuevo.
            tipo_unico = f"CHECKDB_{db_name[:35]}"

            if estado == "ERROR":
                extra = json.loads(fila.extra_data) if fila.extra_data else {}
                detalle = extra.get("detail", "")
                nivel = "CRITICAL"
                mensaje = f"DBCC CHECKDB encontró {errores} error(es) en '{db_name}'."
                if detalle:
                    mensaje += f" {detalle}"
            else:
                nivel = "OK"
                mensaje = f"Última corrida: {estado}."

            actualizar_estado_alerta(
                db=db,
                hid=hosp.hospital_id,
                tipo_unico=tipo_unico,
                nivel=nivel,
                mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers,
                titulo_visible=f"Integridad de base: {db_name}",
            )
