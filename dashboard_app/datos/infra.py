"""
Reportes de infraestructura (`reportes_historicos`).

Hoy cada reporte es una fila con el JSON completo del agente. Con Postgres,
`ultimo_reporte` / `ultimos_reportes` leerán `estado_actual_hospital` (una
fila por hospital, docs/14 §4.1) en vez de buscar el máximo en el histórico,
y `serie_infra` leerá `metricas_host` / `metricas_vm` (o sus agregados) en vez
de parsear el JSON de cada reporte.

Ojo con las columnas sueltas de `reportes_historicos`: `host_ram_usage` guarda
GB usados (main.py le asigna `used_gb`), no porcentaje, y `host_cpu_usage` vale
0 cuando el reporte no trae CPU. Las métricas se leen siempre del JSON.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy import text

from . import pg
from .tiempo import es_postgres, parsear_ts


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
    if es_postgres(db):
        return pg.ultimo_timestamp(db, hospital_id=hospital_id)
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


def ultimo_reporte(db, hospital_id, hasta=None):
    """Último reporte del hospital (anterior a `hasta`, si se pasa), o None si no hay."""
    if es_postgres(db):
        return pg.ultimo_reporte(db, hospital_id=hospital_id, hasta=hasta)
    filtro_hasta = " AND timestamp <= :hasta" if hasta is not None else ""
    fila = db.execute(
        text("SELECT hospital_id, timestamp, host_status, full_json_data FROM reportes_historicos "
             f"WHERE hospital_id = :hid{filtro_hasta} ORDER BY timestamp DESC LIMIT 1"),
        {"hid": hospital_id, "hasta": hasta},
    ).fetchone()
    return _reporte(fila) if fila else None


def ultimos_reportes(db):
    """El último reporte de cada hospital que alguna vez reportó (una consulta para todos)."""
    if es_postgres(db):
        return pg.ultimos_reportes(db)
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
    if es_postgres(db):
        return pg.valores_recientes(db, hospital_id=hospital_id, ruta_json=ruta_json, desde=desde)
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


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------
@dataclass
class PuntoInfra:
    """Métricas de un reporte, ya extraídas del JSON (lo que serán `metricas_host` / `metricas_vm`)."""
    timestamp: datetime
    cpu_host: Optional[float] = None       # %
    ram_host: Optional[float] = None       # %
    temp_amb: Optional[float] = None       # °C
    temperaturas: dict = field(default_factory=dict)   # sensor -> valor
    red: dict = field(default_factory=dict)            # {"lat", "up", "dw"}
    vms: dict = field(default_factory=dict)            # id -> {"cpu", "ram"} (%)


def _num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def metricas_de(timestamp, data):
    """
    Extrae las métricas de un JSON de reporte. Tolera el formato viejo del
    agente (`physical_host`, `environment.thermal`, `vms` como dict).
    """
    phy = data.get("physical_layer") or data.get("physical_host") or {}
    tele = phy.get("telemetry") or {}
    sensors = phy.get("sensors") or (data.get("environment") or {}).get("thermal") or {}
    temps_list = sensors.get("temperatures") or sensors.get("cpu_temps") or []

    temperaturas = {}
    for x in temps_list:
        val = x.get("value") if x.get("value") is not None else x.get("temp_c")
        nombre = x.get("name") or x.get("sensor")
        if _num(val) is not None and nombre:
            temperaturas[nombre] = _num(val)

    temp_amb = _num(sensors.get("ambient_temp_c"))
    if temp_amb is None:
        for x in temps_list:
            if "Ambient" in (x.get("name") or ""):
                temp_amb = _num(x.get("value"))
                break

    net = phy.get("network_health") or {}

    vms = {}
    if isinstance(data.get("virtual_layer"), list):
        for vm in data["virtual_layer"]:
            vid = vm.get("id")
            if vid:
                vt = vm.get("telemetry") or {}
                vms[vid] = {"cpu": (vt.get("cpu") or {}).get("usage_percent", 0),
                            "ram": (vt.get("ram") or {}).get("usage_percent", 0)}
    elif isinstance(data.get("vms"), dict):
        for k, v in data["vms"].items():
            m = v.get("metrics") or {}
            vms[k] = {"cpu": m.get("cpu_load_percent", 0), "ram": (m.get("ram") or {}).get("percent", 0)}

    return PuntoInfra(
        timestamp=timestamp,
        cpu_host=_num((tele.get("cpu") or {}).get("usage_percent")),
        ram_host=_num((tele.get("ram") or {}).get("usage_percent")),
        temp_amb=temp_amb,
        temperaturas=temperaturas,
        red={"lat": net.get("cloud_latency_ms"), "up": net.get("upload_usage_mbps"),
             "dw": net.get("download_usage_mbps")},
        vms=vms,
    )


def serie_infra(db, hospital_id, desde, hasta=None, max_puntos=None, limite=None):
    """
    Métricas de los reportes del hospital en [desde, hasta], en orden
    cronológico. `limite` corta la consulta (los más viejos primero, como el
    gráfico de siempre); `max_puntos` submuestrea parejo antes de parsear.
    Los reportes con timestamp ilegible se omiten.
    """
    if es_postgres(db):
        return pg.serie_infra(db, hospital_id=hospital_id, desde=desde, hasta=hasta, max_puntos=max_puntos, limite=limite)
    filtro_hasta = " AND timestamp <= :hasta" if hasta is not None else ""
    filtro_limite = " LIMIT :limite" if limite else ""
    filas = db.execute(
        text("SELECT timestamp, full_json_data FROM reportes_historicos "
             f"WHERE hospital_id = :hid AND timestamp >= :desde{filtro_hasta} "
             f"ORDER BY timestamp ASC{filtro_limite}"),
        {"hid": hospital_id, "desde": desde, "hasta": hasta, "limite": limite},
    ).fetchall()

    if max_puntos and len(filas) > max_puntos:
        filas = filas[::max(1, int(len(filas) / max_puntos))]

    puntos = []
    for f in filas:
        ts = parsear_ts(f.timestamp)
        if ts is not None:
            puntos.append(metricas_de(ts, json_a_dict(f.full_json_data)))
    return puntos
