"""
KPIs de uso del RIS/PACS (`reportes_uso`): el bloque `application_metrics`
que manda el agente, ~1 por hospital y hora.

Cada reporte trae dos fechas: `timestamp` (cuándo se insertó la fila) y
`start_time_extraction` dentro del JSON (qué período representa). Los
backfills cargan historia vieja con `timestamp` = día del backfill, así que
todo lo que agrupa o filtra "por fecha" usa `fecha_evento`. Con Postgres
(docs/14 §4.3) la fecha del evento pasa a ser columna y los totales salen de
agregados diarios/mensuales en vez de recorrer todo el histórico.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import text

from .infra import json_a_dict
from . import pg
from .tiempo import es_postgres, parsear_ts

# Un backfill o un agente atrasado puede insertar un período hasta unos días
# después de que ocurrió: al filtrar por fecha del evento se lee con este margen
# sobre la fecha de inserción.
MARGEN_INSERCION = timedelta(days=3)


@dataclass
class ReporteUso:
    timestamp: Optional[datetime]        # inserción
    fecha_evento: Optional[datetime]     # período que representa (sin zona)
    metrics: dict = field(default_factory=dict)


def fecha_evento(metrics, timestamp):
    """`start_time_extraction` del JSON (sin zona); si falta o no parsea, la hora de inserción."""
    valor = metrics.get("start_time_extraction")
    if valor:
        try:
            return datetime.fromisoformat(valor).replace(tzinfo=None)
        except (ValueError, TypeError):
            pass
    return parsear_ts(timestamp)


def _reporte(fila):
    metrics = json_a_dict(fila.kpi_json_data)
    return ReporteUso(timestamp=parsear_ts(fila.timestamp),
                      fecha_evento=fecha_evento(metrics, fila.timestamp),
                      metrics=metrics)


def reportes_uso(db, hospital_id, desde=None):
    """Reportes del hospital insertados desde `desde` (todos si es None), por orden de inserción."""
    if es_postgres(db):
        return pg.reportes_uso(db, hospital_id=hospital_id, desde=desde)
    filtro = " AND timestamp >= :desde" if desde is not None else ""
    filas = db.execute(
        text(f"SELECT timestamp, kpi_json_data FROM reportes_uso WHERE hospital_id = :hid{filtro} "
             "ORDER BY timestamp ASC"),
        {"hid": hospital_id, "desde": desde},
    ).fetchall()
    return [_reporte(f) for f in filas]


def reportes_uso_por_evento(db, hospital_id, desde, hasta=None, limite=None):
    """
    Reportes cuya fecha del evento cae en [desde, hasta), en orden de
    inserción. `limite` corta la consulta (por fecha de inserción) antes de filtrar.
    """
    if es_postgres(db):
        return pg.reportes_uso_por_evento(db, hospital_id=hospital_id, desde=desde, hasta=hasta, limite=limite)
    filtro_limite = " LIMIT :limite" if limite else ""
    filas = db.execute(
        text("SELECT timestamp, kpi_json_data FROM reportes_uso "
             "WHERE hospital_id = :hid AND timestamp >= :desde "
             f"ORDER BY timestamp ASC{filtro_limite}"),
        {"hid": hospital_id, "desde": desde - MARGEN_INSERCION, "limite": limite},
    ).fetchall()
    return [r for r in (_reporte(f) for f in filas)
            if r.fecha_evento is not None and r.fecha_evento >= desde
            and (hasta is None or r.fecha_evento < hasta)]
