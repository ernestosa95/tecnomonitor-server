"""
Migración: agrega el índice compuesto (hospital_id, app_name, timestamp) a
software_monitoring. Ver docs/05-performance.md#p4.

`Base.metadata.create_all()` (database.py) NO agrega índices nuevos a una
tabla que ya existe -- solo crea tablas que faltan. Por eso este índice
necesita este script aparte, igual que las columnas agregadas con
ALTER TABLE en el resto de Accesorios/. Es idempotente: correrlo de nuevo
no rompe nada.
"""
import sqlite3
import os

DB_NAME = "monitor_hospitales.db"

if not os.path.exists(DB_NAME):
    print(f"❌ No se encontró {DB_NAME}. Asegúrate de estar en la carpeta correcta.")
else:
    print(f"🔄 Conectando a {DB_NAME}...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    try:
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_swmon_hosp_app_ts
            ON software_monitoring (hospital_id, app_name, timestamp)
        """)
        print("✅ Índice 'idx_swmon_hosp_app_ts' creado (o ya existía).")
    except sqlite3.OperationalError as e:
        print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada.")
