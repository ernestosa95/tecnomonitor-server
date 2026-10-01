"""
Lecturas de software (`software_monitoring`): Mirth, autoenrute DICOM,
certificados SSL, logs de Elastic, CHECKDB, backups SQL y portal paciente.

Hoy es una tabla genérica (app_name + component_id + valor + `extra_data`
JSON). Con Postgres se separa en tablas tipadas por app con agregados
horarios (docs/14 §4.2); mientras tanto, todo pasa por acá.

Las lecturas conservan los nombres de las columnas (`component_id`,
`status_value`, `metric_value`, `extra_data`, `timestamp`) porque los
detectores y el panel ya trabajan con esa forma; lo que cambia es que
`extra_data` llega siempre como dict y `timestamp` como datetime.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import bindparam, text

from .infra import json_a_dict
from .tiempo import parsear_ts

MIRTH = "mirth"
DICOM_ROUTING = "dicom_routing"
SSL = "ssl_certificate"
ELASTIC = "elasticsearch"
SQL_INTEGRITY = "sql_integrity"
SQL_BACKUP = "sql_backup"
PORTAL = "patient_portal"


@dataclass
class Lectura:
    app_name: str
    component_id: str
    status_value: Optional[str] = None
    metric_value: Optional[int] = None
    extra_data: dict = field(default_factory=dict)
    timestamp: Optional[datetime] = None
    rn: int = 1                      # en ultimas_lecturas: 1 = la más reciente del componente


def _lectura(f, rn=1):
    return Lectura(app_name=f.app_name, component_id=f.component_id, status_value=f.status_value,
                   metric_value=f.metric_value, extra_data=json_a_dict(f.extra_data),
                   timestamp=parsear_ts(f.timestamp), rn=rn)


def _apps(apps):
    return (apps,) if isinstance(apps, str) else tuple(apps)


def ultimas_lecturas(db, hospital_id, apps, n=1, por_id=False):
    """
    Las `n` lecturas más recientes de cada (app, componente) del hospital, de
    la más nueva a la más vieja (campo `rn`). `por_id` ordena por orden de
    inserción en vez de por timestamp (backups SQL: la ingesta renueva la
    última fila en el lugar).
    """
    orden = "id DESC" if por_id else "timestamp DESC"
    filas = db.execute(
        text(f"""
            WITH r AS (
                SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp,
                       ROW_NUMBER() OVER (PARTITION BY app_name, component_id ORDER BY {orden}) AS rn
                FROM software_monitoring
                WHERE hospital_id = :hid AND app_name IN :apps
            )
            SELECT * FROM r WHERE rn <= :n ORDER BY app_name, component_id, rn
        """).bindparams(bindparam("apps", expanding=True)),
        {"hid": hospital_id, "apps": list(_apps(apps)), "n": n},
    ).fetchall()
    return [_lectura(f, f.rn) for f in filas]


def lecturas(db, hospital_id, apps, desde, limite=None, por_componente=False):
    """
    Lecturas del hospital desde `desde`, en orden cronológico (o por
    componente y después cronológico, si `por_componente`).
    """
    orden = "component_id, timestamp ASC" if por_componente else "timestamp ASC"
    filtro_limite = " LIMIT :limite" if limite else ""
    filas = db.execute(
        text(f"""
            SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp
            FROM software_monitoring
            WHERE hospital_id = :hid AND app_name IN :apps AND timestamp >= :desde
            ORDER BY {orden}{filtro_limite}
        """).bindparams(bindparam("apps", expanding=True)),
        {"hid": hospital_id, "apps": list(_apps(apps)), "desde": desde, "limite": limite},
    ).fetchall()
    return [_lectura(f) for f in filas]


def ultima_foto(db, hospital_id, app):
    """
    (hora, [lecturas]) de la última lectura completa de una app cuyas filas
    comparten el timestamp (portal paciente). (None, []) si nunca reportó.
    """
    # El valor crudo de MAX() se reusa tal cual en el filtro de igualdad:
    # convertirlo a datetime y volver a bindearlo puede no coincidir con el
    # texto guardado (microsegundos en SQLite).
    ultimo_raw = db.execute(
        text("SELECT MAX(timestamp) FROM software_monitoring WHERE hospital_id = :hid AND app_name = :app"),
        {"hid": hospital_id, "app": app},
    ).scalar()
    ultimo = parsear_ts(ultimo_raw)
    if ultimo is None:
        return None, []
    filas = db.execute(
        text("SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp "
             "FROM software_monitoring WHERE hospital_id = :hid AND app_name = :app AND timestamp = :ts"),
        {"hid": hospital_id, "app": app, "ts": ultimo_raw},
    ).fetchall()
    return ultimo, [_lectura(f) for f in filas]
