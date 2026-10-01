"""
Carga reportes_historicos de una copia SQLite al esquema Postgres (postgres/esquema.sql).

Solo lectura sobre la copia. Pensado para la PC (docs/14 §9.1): se puede borrar
el esquema y repetir las veces que haga falta.

    python3 cargar_infra.py /ruta/monitor_copia.db --dsn postgresql://postgres:local@127.0.0.1:5433/tecnomonitor \
        --desde 2026-09-22 --hasta 2026-09-29

La hora local sin zona de los agentes se interpreta como America/Argentina/Buenos_Aires
(decisión 7). El inventario se arma en orden por hospital: una versión nueva solo si
cambia su hash. Se puede correr por tramos consecutivos (sirve para el diferencial del
corte): parte de la versión de inventario vigente que ya esté en Postgres. Los rangos no
se pueden repetir ni solapar (las métricas se agregan, no se reemplazan).
"""
import argparse
import csv
import io
import json
import os
import sqlite3
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import psycopg2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "dashboard_app"))
from datos.transformar import transformar  # noqa: E402

ZONA = ZoneInfo("America/Argentina/Buenos_Aires")


def _ts(valor):
    t = datetime.fromisoformat(str(valor).replace("Z", ""))
    return t if t.tzinfo else t.replace(tzinfo=ZONA)


class Copiador:
    """Acumula filas por tabla y las manda con COPY en bloques."""

    def __init__(self, cur, columnas, bloque=20000):
        self.cur, self.columnas, self.bloque = cur, columnas, bloque
        self.buf = {t: [] for t in columnas}
        self.total = {t: 0 for t in columnas}

    def add(self, tabla, fila):
        self.buf[tabla].append(fila)
        if len(self.buf[tabla]) >= self.bloque:
            self.flush(tabla)

    def flush(self, tabla=None):
        for t in ([tabla] if tabla else list(self.buf)):
            filas = self.buf[t]
            if not filas:
                continue
            s = io.StringIO()
            w = csv.writer(s)
            for f in filas:
                w.writerow(["\\N" if v is None else v for v in f])
            s.seek(0)
            cols = ", ".join(self.columnas[t])
            self.cur.copy_expert(f"COPY {t} ({cols}) FROM STDIN WITH (FORMAT csv, NULL '\\N')", s)
            self.total[t] += len(filas)
            self.buf[t] = []


COLUMNAS = {
    "metricas_host": ["ts", "hospital_id", "cpu_pct", "ram_pct", "ram_usada_gb", "potencia_w", "latencia_ms",
                      "subida_mbps", "bajada_mbps", "arranque", "host_status"],
    "metricas_sensor": ["ts", "hospital_id", "tipo", "nombre", "valor"],
    "metricas_vm": ["ts", "hospital_id", "vm", "cpu_pct", "ram_pct", "ram_usada_gb", "arranque",
                    "estado", "motivo", "error"],
    "metricas_disco": ["ts", "hospital_id", "vm", "montaje", "uso_pct", "libre_gb", "latencia_ms"],
    "metricas_servicio": ["ts", "hospital_id", "vm", "servicio", "cpu_pct", "ram_mb", "hilos", "handles"],
    "recoleccion": ["ts", "hospital_id", "meta"],
    "reporte_crudo": ["ts", "hospital_id", "datos"],
    "inventario": ["hospital_id", "vigente_desde", "vigente_hasta", "hash", "datos"],
}


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
    n = 0
    inv_actual = {}      # hospital -> [vigente_desde, hash, datos, ya_en_postgres]
    cur.execute("SELECT hospital_id, vigente_desde, hash FROM inventario WHERE vigente_hasta IS NULL")
    for hid, desde, h in cur.fetchall():
        inv_actual[hid] = [desde, h, None, True]
    ultimo = {}          # hospital -> (ts, host_status, datos)
    filas = src.execute(
        "SELECT hospital_id, timestamp, host_status, full_json_data FROM reportes_historicos "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY hospital_id, timestamp",
        (args.desde, args.hasta))

    for hid, ts_txt, host_status, js in filas:
        try:
            data = json.loads(js) if isinstance(js, str) else (js or {})
        except ValueError:
            continue
        ts = _ts(ts_txt)
        f = transformar(ts, data, host_status)
        h = f.host
        cp.add("metricas_host", [ts, hid, h["cpu_pct"], h["ram_pct"], h["ram_usada_gb"], h["potencia_w"],
                                 h["latencia_ms"], h["subida_mbps"], h["bajada_mbps"], h["arranque"], h["host_status"]])
        for s in f.sensores:
            cp.add("metricas_sensor", [ts, hid, s["tipo"], s["nombre"], s["valor"]])
        for v in f.vms:
            cp.add("metricas_vm", [ts, hid, v["vm"], v["cpu_pct"], v["ram_pct"], v["ram_usada_gb"], v["arranque"],
                                   v["estado"], v["motivo"], v["error"]])
        for d in f.discos:
            cp.add("metricas_disco", [ts, hid, d["vm"], d["montaje"], d["uso_pct"], d["libre_gb"], d["latencia_ms"]])
        for s in f.servicios:
            cp.add("metricas_servicio", [ts, hid, s["vm"], s["servicio"], s["cpu_pct"], s["ram_mb"], s["hilos"],
                                         s["handles"]])
        if f.meta is not None:
            cp.add("recoleccion", [ts, hid, json.dumps(f.meta, ensure_ascii=False)])
        cp.add("reporte_crudo", [ts, hid, json.dumps(data, ensure_ascii=False)])

        previo = inv_actual.get(hid)
        if previo is None or previo[1] != f.inventario_hash:
            if previo is not None and previo[3]:      # vigente de una carga anterior: se cierra ahí
                cur.execute("UPDATE inventario SET vigente_hasta = %s WHERE hospital_id = %s AND vigente_desde = %s",
                            (ts, hid, previo[0]))
            elif previo is not None:
                cp.add("inventario", [hid, previo[0], ts, previo[1], json.dumps(previo[2], ensure_ascii=False)])
            inv_actual[hid] = [ts, f.inventario_hash, f.inventario, False]
        ultimo[hid] = (ts, host_status, data)
        n += 1
        if n % 20000 == 0:
            print(f"  {n} reportes, {time.time() - t0:.0f} s", flush=True)

    for hid, (desde, h, datos, ya_en_postgres) in inv_actual.items():
        if not ya_en_postgres:
            cp.add("inventario", [hid, desde, None, h, json.dumps(datos, ensure_ascii=False)])
    cp.flush()
    for hid, (ts, st, data) in ultimo.items():
        cur.execute("""
            INSERT INTO estado_actual_hospital (hospital_id, ts, host_status, datos) VALUES (%s, %s, %s, %s)
            ON CONFLICT (hospital_id) DO UPDATE SET ts = EXCLUDED.ts, host_status = EXCLUDED.host_status,
                                                    datos = EXCLUDED.datos
            WHERE estado_actual_hospital.ts < EXCLUDED.ts
        """, (hid, ts, st, json.dumps(data, ensure_ascii=False)))
    pg.commit()
    print(f"Listo: {n} reportes en {time.time() - t0:.0f} s")
    for t, c in cp.total.items():
        print(f"  {t:18s} {c:>10,}".replace(",", "."))


if __name__ == "__main__":
    main()
