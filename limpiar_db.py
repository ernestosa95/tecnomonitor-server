import sqlite3
import os
import time

# Usamos la misma ruta definida en la arquitectura actual
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "monitor_hospitales.db")

def vacuum_database():
    if not os.path.exists(DB_NAME):
        print(f"❌ No se encontró la base de datos en: {DB_NAME}")
        return

    print(f"--- 🧹 INICIANDO LIBERACIÓN DE ESPACIO (VACUUM) ---")
    
    # Obtener tamaño ANTES
    tamano_antes = os.path.getsize(DB_NAME) / (1024 * 1024)
    print(f"Tamaño actual de la DB: {tamano_antes:.2f} MB")
    
    inicio = time.time()
    
    try:
        # Aumentamos el timeout a 5 minutos por si la DB es gigante
        conn = sqlite3.connect(DB_NAME, timeout=300)
        cursor = conn.cursor()
        
        print("Ejecutando instrucción VACUUM... (Esto puede demorar varios minutos. Por favor, espera).")
        
        # VACUUM reconstruye el archivo completo eliminando las páginas vacías
        cursor.execute("VACUUM;")
        conn.commit()
        
        # Opcional: Forzar un checkpoint del archivo WAL para consolidar datos
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        
        conn.close()
        
        # Obtener tamaño DESPUÉS
        tamano_despues = os.path.getsize(DB_NAME) / (1024 * 1024)
        espacio_recuperado = tamano_antes - tamano_despues
        
        fin = time.time()
        print(f"\n✅ Proceso completado en {round(fin - inicio, 2)} segundos.")
        print(f"Tamaño final de la DB: {tamano_despues:.2f} MB")
        print(f"🎉 Espacio total recuperado en disco: {espacio_recuperado:.2f} MB")
        
    except sqlite3.OperationalError as e:
        print(f"\n❌ Error de SQLite: {e}")
        print("Nota: Si el error es 'database is locked', detén temporalmente la aplicación TecnoMonitor antes de correr el script.")
    except Exception as e:
        print(f"\n❌ Error inesperado: {e}")

if __name__ == "__main__":
    vacuum_database()