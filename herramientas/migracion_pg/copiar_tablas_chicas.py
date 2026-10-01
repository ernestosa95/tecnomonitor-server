"""
Copia completa de las tablas chicas (alertas, configuración, usuarios, hospitales, Mirth,
módulos, exclusiones, baselines...) de SQLite a Postgres. Todo menos las tres históricas,
que van por cargar_infra.py y cargar_software.py.

    python3 copiar_tablas_chicas.py /ruta/monitor_hospitales.db --dsn postgresql://...

Se usa en la PC y el día del corte (docs/14 §9.3, A3/A5): reemplaza el contenido de cada
tabla en Postgres por el de SQLite, en una sola transacción, y deja las secuencias de los id
listas para seguir insertando. Lee SQLite en solo lectura. Las tablas las crea database.py
(create_all) al importarse con DATABASE_URL apuntando a Postgres.
"""
import argparse
import os
import sys

from sqlalchemy import create_engine, func, select, text

RAIZ = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, RAIZ)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite")
    ap.add_argument("--dsn", required=True)
    args = ap.parse_args()

    os.environ["DATABASE_URL"] = args.dsn          # database.py crea las tablas chicas en Postgres
    import database

    origen = create_engine(f"sqlite:///file:{os.path.abspath(args.sqlite)}?mode=ro&uri=true")
    tablas = [t for t in database.Base.metadata.sorted_tables
              if t.name not in database.TABLAS_HISTORICAS_SQLITE]

    with origen.connect() as src, database.engine.begin() as dst:
        dst.execute(text("TRUNCATE " + ", ".join(t.name for t in tablas) + " RESTART IDENTITY CASCADE"))
        for t in tablas:
            filas = [dict(r._mapping) for r in src.execute(select(t))]
            if filas:
                dst.execute(t.insert(), filas)
            # Secuencia del id: que el próximo insert siga después del máximo copiado.
            for col in t.primary_key.columns:
                if col.autoincrement is True or (col.autoincrement == "auto" and col.type.python_type is int
                                                 and len(t.primary_key.columns) == 1):
                    seq = dst.execute(text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": t.name, "c": col.name}).scalar()
                    if seq:
                        maximo = dst.execute(select(func.max(col))).scalar()
                        dst.execute(text("SELECT setval(:s, :v, :llamado)"),
                                    {"s": seq, "v": maximo or 1, "llamado": maximo is not None})
            print(f"  {t.name:28s} {len(filas):>7,}".replace(",", "."))
    print("Listo.")


if __name__ == "__main__":
    main()
