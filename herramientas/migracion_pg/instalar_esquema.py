"""
Instala el esquema completo en una base Postgres vacía (con TimescaleDB disponible):
postgres/esquema.sql (histórico: métricas, inventario, software, KPIs, agregados) y las
tablas chicas de database.py (create_all).

    python3 instalar_esquema.py --dsn postgresql://usuario:clave@host:puerto/tecnomonitor

`CREATE EXTENSION timescaledb` necesita un usuario con permiso para crear extensiones
(en el server, correrlo una vez como administrador de la base).
"""
import argparse
import os
import sys

import psycopg2

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()

    with psycopg2.connect(args.dsn) as pg, pg.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        if cur.fetchone()[0]:
            sys.exit("La base no está vacía: este instalador es solo para una base nueva.")
        cur.execute(open(os.path.join(RAIZ, "postgres", "esquema.sql"), encoding="utf-8").read())
    print("Esquema histórico instalado (postgres/esquema.sql).")

    os.environ["DATABASE_URL"] = args.dsn
    sys.path.insert(0, RAIZ)
    import database  # noqa: F401  (create_all de las tablas chicas al importarse)
    print("Tablas chicas creadas (database.py).")


if __name__ == "__main__":
    main()
