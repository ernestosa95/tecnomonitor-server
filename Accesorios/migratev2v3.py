import sys
import os
import shutil
import json
import logging
from datetime import datetime

# Importamos tus módulos
import database
import transformer

# Configurar Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("migracion-v3")

def backup_database():
    """Crea una copia de seguridad antes de migrar."""
    db_file = database.DB_PATH
    backup_file = f"{db_file}.bak_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    if os.path.exists(db_file):
        logger.info(f"📦 Creando backup de la base de datos: {backup_file}")
        shutil.copy2(db_file, backup_file)
        return True
    else:
        logger.error("❌ No se encontró el archivo de base de datos.")
        return False

def migrar_registros():
    db = database.SessionLocal()
    try:
        # Traemos TODOS los reportes (Cuidado si son millones, aquí asumimos miles)
        logger.info("🔍 Leyendo registros históricos...")
        reportes = db.query(database.ReporteModel).all()
        
        total = len(reportes)
        procesados = 0
        ignorados = 0
        errores = 0

        logger.info(f"📊 Total de registros encontrados: {total}")

        for i, row in enumerate(reportes):
            try:
                # 1. Leer JSON actual
                if not row.full_json_data:
                    continue

                # Si está guardado como string (casos raros sqlite), parsear
                current_data = row.full_json_data
                if isinstance(current_data, str):
                    current_data = json.loads(current_data)

                # 2. Verificar si YA es V3
                # La V3 tiene la clave "envelope" en la raíz
                if "envelope" in current_data:
                    ignorados += 1
                    continue

                # 3. Transformar V2 -> V3
                # Usamos tu transformer probado
                new_data_v3 = transformer.transformar_v2_a_v3(current_data)

                # 4. Actualizar el registro
                # SQLAlchemy maneja la conversión a JSON automáticamente si la columna es JSON
                row.full_json_data = new_data_v3
                
                # Opcional: Si quieres actualizar columnas indexadas que pudieran estar mal en v2
                # row.host_cpu_usage = new_data_v3['physical_layer']['telemetry']['cpu']['usage_percent']
                
                procesados += 1

                # Log de progreso cada 100 registros
                if (i + 1) % 100 == 0:
                    logger.info(f"⏳ Procesando... {i + 1}/{total}")

            except Exception as e:
                errores += 1
                logger.error(f"❌ Error migrando ID {row.id}: {e}")

        # 5. Commit final
        logger.info("💾 Guardando cambios en la base de datos...")
        db.commit()
        
        logger.info("✅ MIGRACIÓN COMPLETADA")
        logger.info(f"   - Convertidos a V3: {procesados}")
        logger.info(f"   - Ya eran V3 (Saltados): {ignorados}")
        logger.info(f"   - Errores: {errores}")

    except Exception as e:
        logger.error(f"❌ Error fatal en la migración: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    print("--- INICIANDO MIGRACIÓN A V3 ---")
    if backup_database():
        migrar_registros()
    else:
        print("Cancelando migración por fallo en backup.")