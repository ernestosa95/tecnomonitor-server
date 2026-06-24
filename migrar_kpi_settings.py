import sqlite3
import os

# Ruta a la base de datos
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitor_hospitales.db")

def migrar_db():
    print(f"Conectando a {DB_PATH}...")
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Agregamos la columna. SQLite no soporta tipo JSON nativo en ALTER TABLE, 
        # así que usamos TEXT con un default de '{}' (diccionario vacío en string)
        cursor.execute("ALTER TABLE hospitales_metadata ADD COLUMN kpi_settings TEXT DEFAULT '{}'")
        conn.commit()
        
        print("✅ Migración exitosa: Columna 'kpi_settings' agregada a 'hospitales_metadata'.")
    except sqlite3.OperationalError as e:
        if "duplicate column name" in str(e).lower():
            print("⚠️ La columna 'kpi_settings' ya existe en la base de datos.")
        else:
            print(f"❌ Error operativo: {e}")
    except Exception as e:
        print(f"❌ Error inesperado: {e}")
    finally:
        if 'conn' in locals():
            conn.close()

if __name__ == "__main__":
    migrar_db()