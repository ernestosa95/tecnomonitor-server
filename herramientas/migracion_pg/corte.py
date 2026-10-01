"""
Guion del corte a Postgres (docs/14 §9.3, C). Tres pasos:

  1. Después de la carga completa desde la foto (cargar_infra + cargar_software):
       python3 corte.py marcar --sqlite /ruta/foto.db --dsn postgresql://...
     Guarda en Postgres hasta qué id llegó la foto en cada tabla histórica.

  2. El día del corte, con la app DETENIDA, sobre la base de producción:
       python3 corte.py diferencial --sqlite /ruta/monitor_hospitales.db --dsn postgresql://...
     Carga lo que entró después de la foto (por id: el reloj de algunos agentes no es
     confiable), vuelve a copiar enteras las tablas chicas y los eventos de CHECKDB/backups
     (la ingesta renueva filas viejas en el lugar), recalcula los agregados y verifica.

  3. Verificación sola (la corre `diferencial` al final; se puede repetir):
       python3 corte.py verificar --sqlite /ruta/monitor_hospitales.db --dsn postgresql://...
     Reportes, KPIs y lecturas de software por hospital y día en los dos motores, y filas
     de las tablas chicas. Sale con código 1 si algo no coincide.

Solo lee SQLite. Vuelta atrás: DATABASE_URL de nuevo a SQLite (queda intacto).
"""
import argparse
import os
import sqlite3
import subprocess
import sys
import time
from collections import defaultdict

import psycopg2

AQUI = os.path.dirname(os.path.abspath(__file__))
TABLAS = ("reportes_historicos", "software_monitoring", "reportes_uso")
AGREGADOS = ("host_1h", "vm_1h", "sensor_1h", "cola_dicom_1h", "mirth_1h", "portal_1h", "host_1d", "vm_1d", "sensor_1d")
ZONA = "America/Argentina/Buenos_Aires"


def _sqlite(ruta):
    return sqlite3.connect(f"file:{os.path.abspath(ruta)}?mode=ro", uri=True)


def _correr(*args):
    print("  $", " ".join(os.path.basename(a) if a.endswith(".py") else a for a in args), flush=True)
    subprocess.run([sys.executable, *args], check=True)


def marcar(args):
    src = _sqlite(args.sqlite)
    if args.marcas:      # pruebas: simular una foto más vieja
        marcas = dict(zip(TABLAS, map(int, args.marcas.split(","))))
    else:
        marcas = {t: src.execute(f"SELECT COALESCE(max(id), 0) FROM {t}").fetchone()[0] for t in TABLAS}
    with psycopg2.connect(args.dsn) as pg, pg.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS migracion_marcas (tabla text PRIMARY KEY, max_id bigint NOT NULL, "
                    "actualizado timestamptz NOT NULL DEFAULT now())")
        for t, m in marcas.items():
            cur.execute("INSERT INTO migracion_marcas (tabla, max_id) VALUES (%s, %s) ON CONFLICT (tabla) "
                        "DO UPDATE SET max_id = EXCLUDED.max_id, actualizado = now()", (t, m))
    print("Marcas de la foto:", marcas)


def diferencial(args):
    t0 = time.time()
    with psycopg2.connect(args.dsn) as pg, pg.cursor() as cur:
        cur.execute("SELECT tabla, max_id FROM migracion_marcas")
        marcas = dict(cur.fetchall())
    if set(marcas) != set(TABLAS):
        sys.exit("Faltan las marcas de la foto: correr `corte.py marcar` después de la carga completa.")
    print("Desde las marcas de la foto:", marcas)
    # Reanudable: cada paso carga hasta un tope fijado antes de empezar (una sola transacción)
    # y su marca se actualiza apenas termina. Si algo se corta, volver a correr `diferencial`
    # sigue desde donde quedó, sin duplicar filas.
    src = _sqlite(args.sqlite)
    topes = {t: src.execute(f"SELECT COALESCE(max(id), 0) FROM {t}").fetchone()[0] for t in TABLAS}
    mas_viejo = min(filter(None, (
        src.execute("SELECT min(timestamp) FROM reportes_historicos WHERE id > ?", (marcas["reportes_historicos"],)).fetchone()[0],
        src.execute("SELECT min(timestamp) FROM software_monitoring WHERE id > ?", (marcas["software_monitoring"],)).fetchone()[0],
    )), default=None)

    def _marcar(*tablas):
        with psycopg2.connect(args.dsn) as pg, pg.cursor() as cur:
            for t in tablas:
                cur.execute("UPDATE migracion_marcas SET max_id = %s, actualizado = now() WHERE tabla = %s", (topes[t], t))

    print("1/5 Reportes de infraestructura nuevos")
    if marcas["reportes_historicos"] < topes["reportes_historicos"]:
        _correr(os.path.join(AQUI, "cargar_infra.py"), args.sqlite, "--dsn", args.dsn,
                "--id-mayor", str(marcas["reportes_historicos"]), "--id-hasta", str(topes["reportes_historicos"]))
        _marcar("reportes_historicos")
    print("2/5 Software y KPIs nuevos (sin CHECKDB/backups)")
    if marcas["software_monitoring"] < topes["software_monitoring"] or marcas["reportes_uso"] < topes["reportes_uso"]:
        _correr(os.path.join(AQUI, "cargar_software.py"), args.sqlite, "--dsn", args.dsn,
                "--id-mayor", str(marcas["software_monitoring"]), "--id-hasta", str(topes["software_monitoring"]),
                "--uso-id-mayor", str(marcas["reportes_uso"]), "--uso-id-hasta", str(topes["reportes_uso"]),
                "--sin-apps", "sql_integrity,sql_backup")
        _marcar("software_monitoring", "reportes_uso")
    print("3/5 CHECKDB y backups completos (la ingesta renueva filas viejas)")
    with psycopg2.connect(args.dsn) as pg, pg.cursor() as cur:
        cur.execute("TRUNCATE sql_eventos RESTART IDENTITY")
    _correr(os.path.join(AQUI, "cargar_software.py"), args.sqlite, "--dsn", args.dsn,
            "--apps", "sql_integrity,sql_backup", "--sin-kpi")
    print("4/5 Tablas chicas completas")
    _correr(os.path.join(AQUI, "copiar_tablas_chicas.py"), args.sqlite, "--dsn", args.dsn)

    print("5/5 Agregados (desde lo más viejo que entró en el diferencial)")
    pg = psycopg2.connect(args.dsn)
    pg.autocommit = True                     # refresh_continuous_aggregate no corre dentro de una transacción
    with pg.cursor() as cur:
        for v in AGREGADOS:
            # Un día antes, para cubrir los diarios; sin datos nuevos no hay nada que recalcular.
            if mas_viejo is not None:
                cur.execute(f"CALL refresh_continuous_aggregate(%s, (%s::timestamp AT TIME ZONE '{ZONA}') "
                            "- interval '1 day', NULL)", (v, mas_viejo))
    pg.close()
    print(f"Diferencial cargado en {time.time() - t0:.0f} s. Verificando...")
    return verificar(args)


def _conteos_sqlite(src, sql):
    out = defaultdict(int)
    for h, d, n in src.execute(sql):
        out[(h, d)] += n
    return out


def _conteos_pg(cur, sql):
    cur.execute(sql)
    return {(h, str(d)): n for h, d, n in cur.fetchall()}


def verificar(args):
    src = _sqlite(args.sqlite)
    pg = psycopg2.connect(args.dsn)
    cur = pg.cursor()
    dia = f"(ts AT TIME ZONE '{ZONA}')::date"
    # Solo la infraestructura se puede cargar parcial (es lo pesado); el resto se compara entero.
    desde = f"timestamp >= '{args.desde}'" if args.desde else "1=1"
    desde_pg = f"ts >= '{args.desde}'::timestamp AT TIME ZONE '{ZONA}'" if args.desde else "true"
    controles = [
        ("reportes de infraestructura",
         f"SELECT hospital_id, date(timestamp), count(*) FROM reportes_historicos WHERE {desde} GROUP BY 1, 2",
         f"SELECT hospital_id, {dia}, count(*) FROM metricas_host WHERE {desde_pg} GROUP BY 1, 2"),
        ("reportes de KPIs",
         "SELECT hospital_id, date(timestamp), count(*) FROM reportes_uso GROUP BY 1, 2",
         f"SELECT hospital_id, (insertado AT TIME ZONE '{ZONA}')::date, count(*) FROM kpi_reporte GROUP BY 1, 2"),
    ]
    for app, tabla, filtro in (("mirth", "mirth_canal_metricas", ""), ("dicom_routing", "cola_dicom_metricas", ""),
                               ("patient_portal", "portal_estado_metricas", ""),
                               ("ssl_certificate", "software_eventos", "WHERE app = 'ssl_certificate'"),
                               ("elasticsearch", "software_eventos", "WHERE app = 'elasticsearch'"),
                               ("sql_integrity", "sql_eventos", "WHERE app = 'sql_integrity'"),
                               ("sql_backup", "sql_eventos", "WHERE app = 'sql_backup'")):
        controles.append((f"lecturas de {app}",
                          f"SELECT hospital_id, date(timestamp), count(*) FROM software_monitoring "
                          f"WHERE app_name = '{app}' GROUP BY 1, 2",
                          f"SELECT hospital_id, {dia}, count(*) FROM {tabla} {filtro} GROUP BY 1, 2"))
    errores = 0
    for nombre, q_lite, q_pg in controles:
        a, b = _conteos_sqlite(src, q_lite), _conteos_pg(cur, q_pg)
        malos = sorted(k for k in set(a) | set(b) if a.get(k, 0) != b.get(k, 0))
        print(f"  {nombre:32s} {sum(a.values()):>10,} / {sum(b.values()):>10,}  "
              f"{'OK' if not malos else f'{len(malos)} hospital-día distintos'}".replace(",", "."))
        for k in malos[:5]:
            print(f"      {k}: SQLite {a.get(k, 0)}, Postgres {b.get(k, 0)}")
        errores += len(malos)

    sys.path.insert(0, os.path.abspath(os.path.join(AQUI, "..", "..")))
    os.environ["DATABASE_URL"] = args.dsn
    import database
    for t in database.Base.metadata.sorted_tables:
        if t.name in database.TABLAS_HISTORICAS_SQLITE:
            continue
        n_lite = src.execute(f"SELECT count(*) FROM {t.name}").fetchone()[0]
        cur.execute(f"SELECT count(*) FROM {t.name}")
        n_pg = cur.fetchone()[0]
        if n_lite != n_pg:
            print(f"  tabla {t.name}: SQLite {n_lite}, Postgres {n_pg}")
            errores += 1
    print("VERIFICACIÓN OK" if not errores else f"VERIFICACIÓN CON {errores} DIFERENCIAS")
    return 0 if not errores else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paso", choices=("marcar", "diferencial", "verificar"))
    ap.add_argument("--sqlite", required=True)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--marcas", help="marcar: ids a mano 'reportes,software,uso' (solo pruebas)")
    ap.add_argument("--desde", help="verificar: comparar la infraestructura desde esta fecha local")
    args = ap.parse_args()
    r = {"marcar": marcar, "diferencial": diferencial, "verificar": verificar}[args.paso](args)
    sys.exit(r or 0)


if __name__ == "__main__":
    main()
