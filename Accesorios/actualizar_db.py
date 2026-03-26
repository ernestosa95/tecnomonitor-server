import sqlite3
import os

# Ruta a la base de datos
DB_NAME = "monitor_hospitales.db"

if not os.path.exists(DB_NAME):
    print(f"❌ No se encontró {DB_NAME}. Asegúrate de estar en la carpeta correcta.")
else:
    print(f"🔄 Conectando a {DB_NAME}...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    try:
        # Intentamos agregar la columna alerts_enabled
        cursor.execute("ALTER TABLE hospitales_metadata ADD COLUMN alerts_enabled BOOLEAN DEFAULT 1")
        print("✅ Columna 'alerts_enabled' agregada correctamente.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("ℹ️ La columna 'alerts_enabled' ya existía. No se hicieron cambios.")
        else:
            print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada.")