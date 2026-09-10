# Migración: agrega la columna 'datos_manuales' a 'hospitales_metadata'.
#
# La tabla nueva 'hospital_manual_kpi' NO se crea acá: SQLAlchemy la crea
# sola (Base.metadata.create_all) la próxima vez que arranque la app,
# igual que cualquier tabla nueva agregada a database.py. Este script solo
# resuelve la columna nueva sobre la tabla existente 'hospitales_metadata'
# (ALTER TABLE no lo hace create_all).
#
# Ejecutar desde la raíz del repo: python Accesorios/migrar_datos_manuales.py
import sqlite3
import os

DB_NAME = "monitor_hospitales.db"

if not os.path.exists(DB_NAME):
    print(f"❌ No se encontró {DB_NAME}. Asegúrate de estar en la carpeta raíz del proyecto.")
else:
    print(f"🔄 Conectando a {DB_NAME}...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE hospitales_metadata ADD COLUMN datos_manuales BOOLEAN DEFAULT 0")
        print("✅ Columna 'datos_manuales' agregada correctamente.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("ℹ️ La columna 'datos_manuales' ya existía. No se hicieron cambios.")
        else:
            print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada. La tabla 'hospital_manual_kpi' se crea sola al arrancar la app.")
