"""
Carga software_monitoring y reportes_uso de una copia SQLite al esquema Postgres
(bloque 2 de postgres/esquema.sql). Solo lectura sobre la copia.

    python3 cargar_software.py /ruta/monitor_copia.db --dsn postgresql://postgres:local@127.0.0.1:5433/tecnomonitor \
        --desde 2026-09-22 --hasta 2026-09-29

Asume las tablas vacías para el rango. `dicom_reglas` queda con los datos de la
última lectura de cada regla.
"""
import argparse
import json
import os
import sqlite3
import sys
import time

import psycopg2

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "dashboard_app"))
from cargar_infra import ZONA, Copiador, _ts  # noqa: E402
from datos.transformar import kpis, software  # noqa: E402

COLUMNAS = {
    "mirth_canal_metricas": ["ts", "hospital_id", "componente", "instancia", "channel_id", "estado", "encolados",
                             "recibidos", "enviados", "errores", "ultimo_error"],
    "cola_dicom_metricas": ["ts", "hospital_id", "regla", "pendientes"],
    "portal_estado_metricas": ["ts", "hospital_id", "componente", "origen", "codigo", "estado", "total",
                               "ultimas_24h", "sin_iso", "con_iso", "mas_antiguo", "fuente"],
    "software_eventos": ["ts", "hospital_id", "app", "componente", "estado", "valor", "extra"],
    "sql_eventos": ["ts", "hospital_id", "app", "base", "estado", "valor", "extra"],
    "kpi_reporte": ["id", "hospital_id", "insertado", "desde", "hasta", "intervalo_horas"],
    "kpi_ris": ["reporte_id", "orden", "equipo", "aet", "modalidad", "totales", "citados", "admitidos", "ejecutados",
                "con_imagen", "borradores", "definitivos", "suspendidos"],
    "kpi_pacs": ["reporte_id", "orden", "aet", "modalidad", "almacenados"],
    "kpi_usuarios": ["reporte_id", "orden", "rol", "usuarios_unicos", "inicios_sesion"],
}


def _json(v):
    return json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite")
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--desde", required=True)
    ap.add_argument("--hasta", required=True)
    args = ap.parse_args()

    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    pg = psycopg2.connect(args.dsn)
    cur = pg.cursor()
    cp = Copiador(cur, COLUMNAS)
    t0 = time.time()

    # --- Software: en orden de id (sql_eventos conserva el orden de inserción) ---
    reglas = {}
    n = 0
    for hid, app, comp, estado, valor, extra, ts_txt in src.execute(
            "SELECT hospital_id, app_name, component_id, status_value, metric_value, extra_data, timestamp "
            "FROM software_monitoring WHERE timestamp >= ? AND timestamp < ? ORDER BY id", (args.desde, args.hasta)):
        try:
            extra = json.loads(extra) if isinstance(extra, str) else extra
        except ValueError:
            extra = {}
        ts = _ts(ts_txt)
        tabla, fila = software(app, comp, estado, valor, extra, ZONA)
        regla = fila.pop("_regla", None)
        if regla is not None:
            reglas[(hid, comp)] = (regla, ts)
        cp.add(tabla, [ts, hid] + [_json(fila[c]) for c in COLUMNAS[tabla][2:]])
        n += 1
        if n % 200000 == 0:
            print(f"  software: {n} lecturas, {time.time() - t0:.0f} s", flush=True)
    cp.flush()
    for (hid, regla), (r, ts) in reglas.items():
        cur.execute("""
            INSERT INTO dicom_reglas (hospital_id, regla, etiqueta, origen_key, origen_nick, origen_host,
                                      destino_key, destino_nick, destino_host, visto)
            VALUES (%(h)s, %(g)s, %(etiqueta)s, %(origen_key)s, %(origen_nick)s, %(origen_host)s,
                    %(destino_key)s, %(destino_nick)s, %(destino_host)s, %(visto)s)
            ON CONFLICT (hospital_id, regla) DO UPDATE SET etiqueta = EXCLUDED.etiqueta,
                origen_key = EXCLUDED.origen_key, origen_nick = EXCLUDED.origen_nick, origen_host = EXCLUDED.origen_host,
                destino_key = EXCLUDED.destino_key, destino_nick = EXCLUDED.destino_nick,
                destino_host = EXCLUDED.destino_host, visto = EXCLUDED.visto
            WHERE dicom_reglas.visto < EXCLUDED.visto
        """, dict(r, h=hid, g=regla, visto=ts))
    print(f"Software: {n} lecturas, {len(reglas)} reglas de autoenrute, {time.time() - t0:.0f} s")

    # --- KPIs de uso ---
    filas = src.execute("SELECT hospital_id, timestamp, kpi_json_data FROM reportes_uso "
                        "WHERE timestamp >= ? AND timestamp < ? ORDER BY id", (args.desde, args.hasta)).fetchall()
    cur.execute("SELECT nextval('kpi_reporte_id_seq') FROM generate_series(1, %s)", (len(filas),))
    ids = [r[0] for r in cur.fetchall()]
    for rid, (hid, ts_txt, js) in zip(ids, filas):
        try:
            metrics = json.loads(js) if isinstance(js, str) else (js or {})
        except ValueError:
            metrics = {}
        cab, ris, pacs, usuarios = kpis(metrics, ZONA)
        cp.add("kpi_reporte", [rid, hid, _ts(ts_txt), cab["desde"], cab["hasta"], cab["intervalo_horas"]])
        for tabla, items in (("kpi_ris", ris), ("kpi_pacs", pacs), ("kpi_usuarios", usuarios)):
            for it in items:
                cp.add(tabla, [rid] + [it[c] for c in COLUMNAS[tabla][1:]])
    cp.flush()
    pg.commit()
    print(f"Listo en {time.time() - t0:.0f} s")
    for t, c in cp.total.items():
        print(f"  {t:24s} {c:>10,}".replace(",", "."))


if __name__ == "__main__":
    main()
