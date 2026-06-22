import json
import sys
import os

# Agregamos la ruta del directorio padre para poder importar database.py
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import SessionLocal, LogDictionary

def cargar_diccionario(ruta_json: str):
    """
    Lee el archivo JSON de diccionario de errores y realiza un upsert
    (actualiza si existe, inserta si no) en la tabla log_dictionary.
    """
    if not os.path.exists(ruta_json):
        print(f"❌ Error: No se encontró el archivo en la ruta: {ruta_json}")
        return

    try:
        with open(ruta_json, 'r', encoding='utf-8') as file:
            data = json.load(file)
            
        app_name = data.get("app_name", "unknown")
        dictionary = data.get("dictionary", {})
        
        db = SessionLocal()
        
        registros_nuevos = 0
        registros_actualizados = 0

        for error_code, details in dictionary.items():
            # Buscar si el código de error (event_id) ya existe para esta app
            existing_log = db.query(LogDictionary).filter(
                LogDictionary.event_id == error_code,
                LogDictionary.app_name == app_name
            ).first()

            if existing_log:
                # Actualizar el registro existente mapeando el JSON a tu modelo
                existing_log.title = details.get("titulo", existing_log.title)
                existing_log.description = details.get("descripcion", existing_log.description)
                existing_log.action = details.get("accion", existing_log.action)
                existing_log.severity = details.get("severidad_default", existing_log.severity)
                registros_actualizados += 1
            else:
                # Crear un registro nuevo
                nuevo_log = LogDictionary(
                    app_name=app_name,
                    event_id=error_code,
                    title=details.get("titulo"),
                    description=details.get("descripcion"),
                    action=details.get("accion"),
                    severity=details.get("severidad_default")
                )
                db.add(nuevo_log)
                registros_nuevos += 1

        db.commit()
        print(f"✅ Importación completada para la aplicación: '{app_name}'")
        print(f"   ➡️ Nuevos errores insertados: {registros_nuevos}")
        print(f"   🔄 Errores actualizados: {registros_actualizados}")

    except Exception as e:
        db.rollback()
        print(f"❌ Ocurrió un error en la base de datos durante la importación: {str(e)}")
    finally:
        db.close()

if __name__ == "__main__":
    # Define la ruta al JSON. Asumiendo que el JSON está en la raíz (una carpeta arriba de Accesorios)
    ruta_archivo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "diccionario_error.json")
    
    cargar_diccionario(ruta_archivo)