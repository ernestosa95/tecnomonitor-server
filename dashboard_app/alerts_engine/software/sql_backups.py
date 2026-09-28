"""
Detector del último backup completo de las bases SQL Server (REQ-06, agente 4.5.3): alerta
CRITICAL si una base pasa más de `sql_backup_max_hours` (24 h por defecto) sin backup completo,
o si nunca tuvo uno. El siguiente backup completo la cierra. Ver docs/16 (REQ-06) y
docs/10-contrato-ingesta-agente.md §7.6.

`estado_backups()` es el criterio único: lo usan este detector y la tarjeta de la pestaña Software
(routers/hospital_detalle.py), para que el panel no muestre verde con un ticket abierto o al revés.

Una base cuya última lectura tiene más de LECTURA_VIGENTE_HORAS no se evalúa (queda como estaba):
sin lecturas no se puede afirmar nada, y el agente pudo haberse apagado o el módulo desactivado.
Sin responsable propio: reusa 'global_alert_responsible_email' (Infraestructura), como CHECKDB.
"""
import json
from datetime import datetime

from sqlalchemy import text

from ..config import _followers_de
from ..estado import actualizar_estado_alerta

LECTURA_VIGENTE_HORAS = 6

ESTADO_OK, ESTADO_VENCIDO, ESTADO_NUNCA, ESTADO_SIN_LECTURA = "OK", "VENCIDO", "NUNCA", "SIN_LECTURA"


def _fecha(valor):
    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, str) and valor:
        try:
            return datetime.fromisoformat(valor[:19])
        except ValueError:
            return None
    return None


def estado_backups(db, hospital_id, max_horas, ahora=None):
    """
    Una entrada por base con su último backup completo, la antigüedad en horas y el estado
    (OK / VENCIDO / NUNCA / SIN_LECTURA). Toma la última fila por base (la ingesta agrega una
    por backup nuevo y renueva `last_seen` en la última).
    """
    ahora = ahora or datetime.now()
    filas = db.execute(text("""
        WITH RankedData AS (
            SELECT component_id, status_value, extra_data,
                   ROW_NUMBER() OVER(PARTITION BY component_id ORDER BY id DESC) as rn
            FROM software_monitoring
            WHERE hospital_id = :hid AND app_name = 'sql_backup'
        )
        SELECT component_id, status_value, extra_data FROM RankedData WHERE rn = 1
    """), {"hid": hospital_id}).fetchall()

    bases = []
    for f in filas:
        extra = f.extra_data
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except ValueError:
                extra = {}
        extra = extra or {}
        ultimo = _fecha(extra.get("last_full"))
        leido = _fecha(extra.get("last_seen"))
        horas = round((ahora - ultimo).total_seconds() / 3600, 1) if ultimo else None

        if leido is None or (ahora - leido).total_seconds() > LECTURA_VIGENTE_HORAS * 3600:
            estado = ESTADO_SIN_LECTURA
        elif ultimo is None:
            estado = ESTADO_NUNCA
        elif horas > max_horas:
            estado = ESTADO_VENCIDO
        else:
            estado = ESTADO_OK
        bases.append({
            "db": f.component_id,
            "last_full": ultimo.strftime("%Y-%m-%d %H:%M:%S") if ultimo else None,
            "age_hours": horas,
            "last_seen": leido.strftime("%Y-%m-%d %H:%M:%S") if leido else None,
            "status": estado,
        })
    bases.sort(key=lambda b: b["db"].lower())
    return bases


def verificar_backups_bases(db, config, hospitales_activos):
    asana_followers = _followers_de(db, config)  # default: global_alert_responsible_email
    try:
        max_horas = max(1, int(config.get("sql_backup_max_hours", 24) or 24))
    except (TypeError, ValueError):
        max_horas = 24

    for hosp in hospitales_activos:
        for b in estado_backups(db, hosp.hospital_id, max_horas):
            if b["status"] == ESTADO_SIN_LECTURA:
                continue

            if b["status"] == ESTADO_NUNCA:
                nivel = "CRITICAL"
                mensaje = f"La base '{b['db']}' no tiene ningún backup completo registrado en SQL Server."
            elif b["status"] == ESTADO_VENCIDO:
                nivel = "CRITICAL"
                mensaje = (f"La base '{b['db']}' no tiene backup completo hace {b['age_hours']:.0f} h "
                           f"(último: {b['last_full']}; umbral: {max_horas} h).")
            else:
                nivel = "OK"
                mensaje = f"Último backup completo: {b['last_full']}."

            # tipo_unico por (hospital, base): el siguiente backup cierra el mismo ticket.
            actualizar_estado_alerta(
                db=db,
                hid=hosp.hospital_id,
                tipo_unico=f"SQLBACKUP_{b['db'][:35]}",
                nivel=nivel,
                mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers,
                titulo_visible=f"Backup de base: {b['db']}",
            )
