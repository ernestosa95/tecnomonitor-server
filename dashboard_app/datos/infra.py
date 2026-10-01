"""
Reportes de infraestructura (`reportes_historicos`).

Hoy cada reporte es una fila con el JSON completo del agente. Con Postgres,
`ultimo_reporte` / `ultimos_reportes` leerán `estado_actual_hospital` (una
fila por hospital, docs/14 §4.1) en vez de buscar el máximo en el histórico.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from .tiempo import parsear_ts


@dataclass
class Reporte:
    hospital_id: str
    timestamp: Optional[datetime]
    host_status: Optional[str] = None
    data: dict = field(default_factory=dict)   # JSON del agente; {} si no hay o no parsea


def json_a_dict(valor):
    """JSON de la base (str o dict) a dict. {} si está vacío, roto o no es un objeto."""
    if isinstance(valor, dict):
        return valor
    if not valor:
        return {}
    try:
        d = json.loads(valor)
    except (TypeError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _reporte(fila):
    return Reporte(
        hospital_id=fila.hospital_id,
        timestamp=parsear_ts(fila.timestamp),
        host_status=fila.host_status,
        data=json_a_dict(fila.full_json_data),
    )


def ultimo_timestamp(db, hospital_id):
    """Hora del último reporte del hospital, o None si nunca reportó (o es ilegible, con aviso)."""
    fila = db.execute(
        text("SELECT timestamp FROM reportes_historicos WHERE hospital_id = :hid "
             "ORDER BY timestamp DESC LIMIT 1"),
        {"hid": hospital_id},
    ).fetchone()
    if not fila:
        return None
    ts = parsear_ts(fila.timestamp)
    if ts is None:
        print(f"⚠️ [datos] '{hospital_id}' tiene reporte pero timestamp ilegible: {fila.timestamp!r}.")
    return ts


def ultimo_reporte(db, hospital_id):
    """Último reporte del hospital, o None si nunca reportó."""
    fila = db.execute(
        text("SELECT hospital_id, timestamp, host_status, full_json_data FROM reportes_historicos "
             "WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1"),
        {"hid": hospital_id},
    ).fetchone()
    return _reporte(fila) if fila else None


def ultimos_reportes(db):
    """El último reporte de cada hospital que alguna vez reportó (una consulta para todos)."""
    filas = db.execute(text("""
        SELECT h.hospital_id, h.timestamp, h.host_status, h.full_json_data
        FROM reportes_historicos h
        INNER JOIN (SELECT hospital_id, MAX(timestamp) AS max_t
                    FROM reportes_historicos GROUP BY hospital_id) m
          ON h.hospital_id = m.hospital_id AND h.timestamp = m.max_t
    """)).fetchall()
    return [_reporte(f) for f in filas]


def valores_recientes(db, hospital_id, ruta_json, desde):
    """
    Valor de `ruta_json` (p. ej. '$.collection_meta.dicom_routing') en cada
    reporte del hospital desde `desde`, en orden cronológico. Lee solo esa
    parte del JSON. Cada elemento es el valor parseado, o None si falta.
    """
    filas = db.execute(
        text("SELECT json_extract(full_json_data, :ruta) AS valor FROM reportes_historicos "
             "WHERE hospital_id = :hid AND timestamp >= :desde ORDER BY timestamp ASC"),
        {"hid": hospital_id, "desde": desde, "ruta": ruta_json},
    ).fetchall()
    valores = []
    for f in filas:
        v = f.valor
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except ValueError:
                pass
        valores.append(v)
    return valores
