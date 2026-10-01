"""
Escritura de la ingesta (main.py) en el motor que corresponda.

En SQLite hace exactamente lo de siempre (filas de reportes_historicos, reportes_uso y
software_monitoring). En Postgres reparte cada reporte con datos.transformar en el esquema
nuevo: métricas, inventario (solo si cambió), estado actual, crudo, software y KPIs. Todo
dentro de la transacción de la sesión: main.py hace el commit, igual que antes.

Hora del reporte (docs/14, decisión 7): en Postgres se guarda también la hora de recepción
del server y, si la del agente difiere más de TOLERANCIA_RELOJ, se usa la del server (el
reloj del equipo de H03 llegó a estar 4 h adelantado y eso demoraba la alerta OFFLINE).
"""
import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

import database

from . import transformar as tr
from .tiempo import ZONA, a_pg, es_postgres

logger = logging.getLogger("ingest-v4")

TOLERANCIA_RELOJ = timedelta(minutes=10)


def hora_del_reporte(db, hospital_id, ts_agente, recibido):
    """La hora con la que se guarda el reporte (hora local sin zona, como la usa main.py)."""
    if not es_postgres(db) or ts_agente is None:
        return ts_agente
    if abs(ts_agente - recibido) > TOLERANCIA_RELOJ:
        logger.warning(f"⏱️ [Ingesta] {hospital_id}: hora del agente {ts_agente:%Y-%m-%d %H:%M:%S} difiere "
                       f"{(ts_agente - recibido).total_seconds() / 60:+.0f} min de la del server; se usa la del server.")
        return recibido
    return ts_agente


# ---------------------------------------------------------------------------
# Software
# ---------------------------------------------------------------------------
def guardar_lectura(db, hospital_id, app, componente, estado, valor, extra, ts):
    if not es_postgres(db):
        db.add(database.SoftwareMonitoring(hospital_id=hospital_id, app_name=app, component_id=componente,
                                           status_value=estado, metric_value=valor, extra_data=extra, timestamp=ts))
        return
    tabla, fila = tr.software(app, componente, estado, valor, extra, ZONA)
    regla = fila.pop("_regla", None)
    fila = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v) for k, v in fila.items()}
    cols = ["ts", "hospital_id"] + list(fila)
    db.execute(text(f"INSERT INTO {tabla} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
               {"ts": a_pg(ts), "hospital_id": hospital_id, **fila})
    if regla is not None:
        db.execute(text("""
            INSERT INTO dicom_reglas (hospital_id, regla, etiqueta, origen_key, origen_nick, origen_host,
                                      destino_key, destino_nick, destino_host, visto)
            VALUES (:h, :g, :etiqueta, :origen_key, :origen_nick, :origen_host, :destino_key, :destino_nick,
                    :destino_host, :visto)
            ON CONFLICT (hospital_id, regla) DO UPDATE SET etiqueta = EXCLUDED.etiqueta,
                origen_key = EXCLUDED.origen_key, origen_nick = EXCLUDED.origen_nick, origen_host = EXCLUDED.origen_host,
                destino_key = EXCLUDED.destino_key, destino_nick = EXCLUDED.destino_nick,
                destino_host = EXCLUDED.destino_host, visto = EXCLUDED.visto
            WHERE dicom_reglas.visto < EXCLUDED.visto
        """), {"h": hospital_id, "g": componente, "visto": a_pg(ts), **regla})


def existe_lectura(db, hospital_id, app, ts, componente=None):
    """¿Ya hay una lectura de esa app (y componente) con esa hora? Para no repetir reenvíos del agente."""
    if not es_postgres(db):
        q = db.query(database.SoftwareMonitoring.id).filter_by(hospital_id=hospital_id, app_name=app, timestamp=ts)
        if componente is not None:
            q = q.filter_by(component_id=componente)
        return q.first() is not None
    tabla, col = {"sql_integrity": ("sql_eventos", "base"), "sql_backup": ("sql_eventos", "base"),
                  "patient_portal": ("portal_estado_metricas", "componente")}[app]
    filtro_app = " AND app = :app" if tabla == "sql_eventos" else ""
    filtro_comp = f" AND {col} = :c" if componente is not None else ""
    return db.execute(text(f"SELECT 1 FROM {tabla} WHERE hospital_id = :h AND ts = :ts{filtro_app}{filtro_comp} LIMIT 1"),
                      {"h": hospital_id, "ts": a_pg(ts), "app": app, "c": componente}).first() is not None


def ultima_lectura_sql(db, hospital_id, app, base):
    """(manija, extra) de la última fila de una base (orden de inserción), o (None, None)."""
    if not es_postgres(db):
        fila = db.query(database.SoftwareMonitoring).filter_by(
            hospital_id=hospital_id, app_name=app, component_id=base
        ).order_by(database.SoftwareMonitoring.id.desc()).first()
        extra = fila.extra_data if fila is not None else None
        if isinstance(extra, str):
            try:
                extra = json.loads(extra)
            except ValueError:
                extra = None
        return fila, extra
    f = db.execute(text("SELECT id, extra FROM sql_eventos WHERE hospital_id = :h AND app = :app AND base = :b "
                        "ORDER BY id DESC LIMIT 1"), {"h": hospital_id, "app": app, "b": base}).fetchone()
    return (f.id, f.extra) if f else (None, None)


def actualizar_extra_sql(db, manija, extra):
    """Reemplaza los datos extra de la fila devuelta por ultima_lectura_sql (renueva last_seen de backups)."""
    if not es_postgres(db):
        manija.extra_data = extra
        return
    db.execute(text("UPDATE sql_eventos SET extra = CAST(:e AS jsonb) WHERE id = :id"),
               {"e": json.dumps(extra, ensure_ascii=False), "id": manija})


# ---------------------------------------------------------------------------
# KPIs de uso
# ---------------------------------------------------------------------------
def guardar_uso(db, hospital_id, ts, metrics):
    if not es_postgres(db):
        db.add(database.ReporteUso(hospital_id=hospital_id, timestamp=ts, kpi_json_data=json.dumps(metrics)))
        return
    cab, ris, pacs, usuarios = tr.kpis(metrics, ZONA)
    rid = db.execute(text("INSERT INTO kpi_reporte (hospital_id, insertado, desde, hasta, intervalo_horas) "
                          "VALUES (:h, :ins, :desde, :hasta, :iv) RETURNING id"),
                     {"h": hospital_id, "ins": a_pg(ts), "desde": cab["desde"], "hasta": cab["hasta"],
                      "iv": cab["intervalo_horas"]}).scalar()
    for tabla, items in (("kpi_ris", ris), ("kpi_pacs", pacs), ("kpi_usuarios", usuarios)):
        for it in items:
            cols = ["reporte_id"] + list(it)
            db.execute(text(f"INSERT INTO {tabla} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                       {"reporte_id": rid, **it})


# ---------------------------------------------------------------------------
# Reporte de infraestructura
# ---------------------------------------------------------------------------
def guardar_reporte(db, hospital_id, ts, host_status, host_cpu, host_ram, p_watts, data_dict, recibido=None):
    """Devuelve el id de la fila en SQLite (None en Postgres, donde no hay una fila única)."""
    if not es_postgres(db):
        fila = database.ReporteModel(hospital_id=hospital_id, timestamp=ts, host_status=host_status,
                                     host_cpu_usage=host_cpu, host_ram_usage=host_ram, power_watts=p_watts,
                                     full_json_data=data_dict)
        db.add(fila)
        db.flush()
        return fila.id

    t = a_pg(ts)
    f = tr.transformar(t, data_dict, host_status)
    h = f.host
    db.execute(text("""
        INSERT INTO metricas_host (ts, hospital_id, cpu_pct, ram_pct, ram_usada_gb, potencia_w, latencia_ms,
                                   subida_mbps, bajada_mbps, arranque, host_status, recibido)
        VALUES (:ts, :h, :cpu_pct, :ram_pct, :ram_usada_gb, :potencia_w, :latencia_ms, :subida_mbps,
                :bajada_mbps, :arranque, :host_status, :recibido)
    """), {"ts": t, "h": hospital_id, "recibido": a_pg(recibido or datetime.now()), **h})
    for tabla, filas in (("metricas_sensor", f.sensores), ("metricas_vm", f.vms),
                         ("metricas_disco", f.discos), ("metricas_servicio", f.servicios)):
        if filas:
            cols = ["ts", "hospital_id"] + list(filas[0])
            db.execute(text(f"INSERT INTO {tabla} ({', '.join(cols)}) VALUES ({', '.join(':' + c for c in cols)})"),
                       [{"ts": t, "hospital_id": hospital_id, **x} for x in filas])
    if f.meta is not None:
        db.execute(text("INSERT INTO recoleccion (ts, hospital_id, meta) VALUES (:ts, :h, CAST(:m AS jsonb))"),
                   {"ts": t, "h": hospital_id, "m": json.dumps(f.meta, ensure_ascii=False)})
    datos = json.dumps(data_dict, ensure_ascii=False)
    db.execute(text("INSERT INTO reporte_crudo (ts, hospital_id, datos) VALUES (:ts, :h, CAST(:d AS jsonb))"),
               {"ts": t, "h": hospital_id, "d": datos})
    db.execute(text("""
        INSERT INTO estado_actual_hospital (hospital_id, ts, host_status, datos) VALUES (:h, :ts, :st, CAST(:d AS jsonb))
        ON CONFLICT (hospital_id) DO UPDATE SET ts = EXCLUDED.ts, host_status = EXCLUDED.host_status, datos = EXCLUDED.datos
        WHERE estado_actual_hospital.ts < EXCLUDED.ts
    """), {"h": hospital_id, "ts": t, "st": host_status, "d": datos})

    # Inventario: versión nueva solo si cambió. Un reporte atrasado (más viejo que la versión
    # vigente) no toca el inventario: sus métricas ya quedaron guardadas.
    actual = db.execute(text("SELECT vigente_desde, hash FROM inventario WHERE hospital_id = :h "
                             "AND vigente_hasta IS NULL FOR UPDATE"), {"h": hospital_id}).fetchone()
    if actual is None or (actual.hash != f.inventario_hash and actual.vigente_desde < t):
        if actual is not None:
            db.execute(text("UPDATE inventario SET vigente_hasta = :ts WHERE hospital_id = :h AND vigente_desde = :d"),
                       {"ts": t, "h": hospital_id, "d": actual.vigente_desde})
        db.execute(text("INSERT INTO inventario (hospital_id, vigente_desde, hash, datos) "
                        "VALUES (:h, :ts, :hash, CAST(:d AS jsonb))"),
                   {"h": hospital_id, "ts": t, "hash": f.inventario_hash,
                    "d": json.dumps(f.inventario, ensure_ascii=False)})
    return None


def guardar_fila_software(db, fila):
    """
    Adaptador para main.py: recibe la fila de software_monitoring que la ingesta arma (sin
    agregar a la sesión) y la guarda en el motor que corresponda.
    """
    guardar_lectura(db, fila.hospital_id, fila.app_name, fila.component_id, fila.status_value,
                    fila.metric_value, fila.extra_data, fila.timestamp)
