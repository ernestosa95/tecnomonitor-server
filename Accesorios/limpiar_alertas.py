import sys
import asana
from asana.rest import ApiException
from datetime import datetime

# Importamos tu configuración de base de datos
import database

# --- CONFIGURACIÓN ASANA ---
ASANA_ACCESS_TOKEN = '2/1204918676406253/1212721843116117:412402d867f1baad79ddbe7138761cb7'

def limpiar_alertas_huerfanas():
    print("🔄 Iniciando limpieza manual de base de datos...")
    
    # 1. Configurar cliente Asana
    configuration = asana.Configuration()
    configuration.access_token = ASANA_ACCESS_TOKEN
    api_client = asana.ApiClient(configuration)
    tasks_api = asana.TasksApi(api_client)

    # 2. Abrir conexión a la base de datos local
    db = database.SessionLocal()
    
    try:
        # Buscar TODAS las alertas que figuran como activas localmente
        alertas_activas = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1).all()
        
        if not alertas_activas:
            print("✅ No hay alertas activas en la base de datos local.")
            return

        print(f"📊 Se encontraron {len(alertas_activas)} alertas activas localmente. Verificando con Asana...")
        
        cerradas = 0
        ahora = datetime.now()

        for alerta in alertas_activas:
            gid = alerta.asana_task_gid
            debe_cerrarse = False
            razon = ""

            if not gid:
                # Si por algún error pasado se creó la alerta pero no se guardó el ID de Asana
                debe_cerrarse = True
                razon = "No tiene ID de Asana asignado"
            else:
                try:
                    # Consultamos a Asana solo por el campo "completed" para que sea rápido
                    result = tasks_api.get_task(gid, {'opt_fields': 'completed'})
                    
                    # Extraer el valor según cómo responda la librería (dict u object)
                    is_completed = False
                    if isinstance(result, dict):
                        is_completed = result.get('data', {}).get('completed', False) or result.get('completed', False)
                    else:
                        is_completed = getattr(result, 'completed', False)

                    if is_completed:
                        debe_cerrarse = True
                        razon = "Marcada como Completada en Asana"

                except ApiException as e:
                    if e.status == 404:
                        debe_cerrarse = True
                        razon = "Tarea eliminada o no encontrada en Asana (Error 404)"
                    else:
                        print(f"⚠️ Error de API consultando tarea {gid}: {e.status} - {e.reason}")
                except Exception as e:
                    print(f"⚠️ Error desconocido consultando tarea {gid}: {e}")

            # 3. Aplicar cierre local si se cumplieron las condiciones
            if debe_cerrarse:
                print(f"🧹 Cerrando alerta ID {alerta.id} | {alerta.hospital_id} ({alerta.tipo}) -> Razón: {razon}")
                
                # Modificamos los valores para cerrarla
                alerta.is_active = 0
                alerta.end_time = ahora
                
                # Agregamos una nota al mensaje original para saber que se cerró por este script
                mensaje_limpio = alerta.mensaje.split(" (Cierre")[0] # Evitar duplicar textos si se corre dos veces
                alerta.mensaje = f"{mensaje_limpio} (Cierre por script: {razon})"
                
                cerradas += 1

        # Guardamos todos los cambios juntos
        db.commit()
        print(f"\n✅ Proceso finalizado. Total de alertas regularizadas (cerradas): {cerradas}")

    except Exception as e:
        print(f"❌ Error crítico durante la limpieza: {e}")
    finally:
        db.close() # Siempre cerramos la sesión

if __name__ == '__main__':
    limpiar_alertas_huerfanas()