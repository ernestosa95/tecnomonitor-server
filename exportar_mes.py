import sqlite3
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_MAIN = os.path.join(BASE_DIR, "monitor_hospitales.db")
DB_ARCHIVE = os.path.join(BASE_DIR, "historico_2026_03.db")

# Definimos los límites para Febrero de 2026 (hasta el 1 de Marzo)
FECHA_INICIO = "2026-03-01 00:00:00"
FECHA_FIN = "2026-04-01 00:00:00"

# Quitamos 'reportes_uso' de la lista
tablas_a_exportar = ["reportes_historicos", "software_monitoring"]

def exportar_mes():
    if not os.path.exists(DB_MAIN):
        print(f"❌ No se encontró la DB principal: {DB_MAIN}")
        return

    print(f"--- 📦 INICIANDO EXPORTACIÓN ---")
    print(f"Creando base gemela: {DB_ARCHIVE}")
    print(f"Rango: {FECHA_INICIO} al {FECHA_FIN}\n")

    try:
        conn = sqlite3.connect(DB_MAIN, timeout=300)
        cursor = conn.cursor()

        # Adjuntamos el nuevo archivo como una base de datos secundaria
        cursor.execute(f"ATTACH DATABASE '{DB_ARCHIVE}' AS archive;")

        for tabla in tablas_a_exportar:
            print(f"⏳ Procesando tabla: {tabla}...")
            
            # 1. Copiamos la estructura exacta de la tabla original
            cursor.execute(f"SELECT sql FROM sqlite_master WHERE type='table' AND name='{tabla}'")
            result = cursor.fetchone()
            if not result:
                print(f"⚠️ No se encontró la estructura de {tabla}.")
                continue
            
            # Reemplazamos el nombre para crearla en el archivo de archivo
            schema_sql = result[0].replace(f"CREATE TABLE {tabla}", f"CREATE TABLE archive.{tabla}")
            
            # Si la tabla ya existe (por un intento previo), la borramos para empezar limpio
            cursor.execute(f"DROP TABLE IF EXISTS archive.{tabla}")
            cursor.execute(schema_sql)

            # 2. Insertamos únicamente los datos de Enero
            cursor.execute(f"""
                INSERT INTO archive.{tabla} 
                SELECT * FROM main.{tabla} 
                WHERE timestamp >= ? AND timestamp < ?
            """, (FECHA_INICIO, FECHA_FIN))
            
            filas = cursor.rowcount
            conn.commit()
            print(f"✅ Se exportaron {filas} registros de {tabla}.")

        cursor.execute("DETACH DATABASE archive;")
        conn.close()
        
        tamano_mb = os.path.getsize(DB_ARCHIVE) / (1024 * 1024)
        print(f"\n🎉 Exportación completada con éxito.")
        print(f"El archivo gemelo pesa: {tamano_mb:.2f} MB.")
        
    except sqlite3.OperationalError as e:
        print(f"\n❌ Error de SQLite: {e}")
        print("Si el error es de espacio, significa que el disco está 100% lleno y no podemos ni siquiera crear el archivo temporal.")
    except Exception as e:
        print(f"\n❌ Error durante la exportación: {e}")

if __name__ == "__main__":
    exportar_mes()