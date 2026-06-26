import sqlite3
import os

# Ruta a la base de datos (asegúrate de ejecutarlo donde esté el .db)
DB_NAME = "monitor_hospitales.db"

if not os.path.exists(DB_NAME):
    print(f"❌ No se encontró {DB_NAME}. Asegúrate de estar en la carpeta correcta.")
else:
    print(f"🔄 Conectando a {DB_NAME}...")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    try:
        # Intentamos agregar la columna asana_id (como texto/string, permitiendo nulos)
        cursor.execute("ALTER TABLE users ADD COLUMN asana_id VARCHAR(255) DEFAULT NULL")
        print("✅ Columna 'asana_id' agregada correctamente a la tabla users.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("ℹ️ La columna 'asana_id' ya existía. No se hicieron cambios.")
        else:
            print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada.")