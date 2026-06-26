import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_MAIN = os.path.join(BASE_DIR, "monitor_hospitales.db")

# Definimos los límites para Febrero de 2026 (hasta el 1 de Marzo)
FECHA_INICIO = "2026-03-01 00:00:00"
FECHA_FIN = "2026-04-01 00:00:00"

tablas_a_borrar = ["reportes_historicos", "software_monitoring"]

def borrar_mes():
    if not os.path.exists(DB_MAIN):
        print(f"❌ No se encontró la DB principal: {DB_MAIN}")
        return

    print(f"--- 🗑️ INICIANDO BORRADO SEGURO ---")
    print(f"Rango a eliminar: {FECHA_INICIO} al {FECHA_FIN}\n")

    try:
        conn = sqlite3.connect(DB_MAIN, timeout=300)
        cursor = conn.cursor()

        for tabla in tablas_a_borrar:
            print(f"⏳ Eliminando registros de la tabla: {tabla}...")
            
            cursor.execute(f"""
                DELETE FROM {tabla} 
                WHERE timestamp >= ? AND timestamp < ?
            """, (FECHA_INICIO, FECHA_FIN))
            
            filas_borradas = cursor.rowcount
            conn.commit()
            print(f"✅ Se eliminaron {filas_borradas} registros de {tabla}.")

        # Forzamos a que SQLite aplique los cambios del WAL al archivo principal
        cursor.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        conn.close()
        
        print(f"\n🎉 Borrado completado con éxito.")
        print(f"⚠️ NOTA: El archivo de la base de datos seguirá pesando lo mismo hasta que ejecutemos el VACUUM.")
        
    except Exception as e:
        print(f"\n❌ Error durante el borrado: {e}")

if __name__ == "__main__":
    borrar_mes()