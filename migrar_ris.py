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
        # Intentamos agregar la columna has_ris (0 = False en SQLite)
        cursor.execute("ALTER TABLE hospitales_metadata ADD COLUMN has_ris BOOLEAN DEFAULT 0")
        print("✅ Columna 'has_ris' agregada correctamente.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e):
            print("ℹ️ La columna 'has_ris' ya existía. No se hicieron cambios.")
        else:
            print(f"❌ Error SQL: {e}")

    conn.commit()
    conn.close()
    print("🚀 Migración finalizada.")