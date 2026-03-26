import uvicorn
import sys
import os
import asyncio 
from fastapi import FastAPI
import maintenance
 
# --- 1. PREPARACIÓN DE RUTAS ---
current_dir = os.path.dirname(os.path.abspath(__file__))
dashboard_path = os.path.join(current_dir, "dashboard_app")
 
# Insertamos la ruta del dashboard para poder importarlo
sys.path.insert(0, dashboard_path)

# Importamos los módulos necesarios para la vigilancia
import database 
try:
    import alerts_engine
    print("✅ Alerts Engine cargado para servicio 24/7.")
except ImportError:
    # Fallback por si la ruta no se cargó a tiempo
    from dashboard_app import alerts_engine
 
# --- 2. IMPORTAR LAS APPS ---
try:
    from main import app as listener_app
    print("✅ Listener (main.py) cargado.")
except Exception as e:
    print(f"❌ Error cargando Listener: {e}")
 
try:
    from dashboard_app.dashboard import app as dashboard_app
    print("✅ Dashboard cargado.")
except Exception as e:
    print(f"❌ Error cargando Dashboard: {e}")
    sys.exit(1)
 
# --- 3. CREAR APP MAESTRA ---
master_app = FastAPI(title="TecnoMonitor Unificado")

# --- 4. BACKGROUND SERVICE (Vigilancia + Mantenimiento) ---
async def ciclo_vigilancia():
    """
    Este proceso corre en paralelo al servidor web.
    1. Cada 60s: Verifica alertas (offline, hardware).
    2. Cada 24h: Ejecuta limpieza y compresión de base de datos.
    """
    print("🔄 Iniciando Hilo de Vigilancia (Background Service)...")
    
    # Iniciamos el contador en 0.
    # El mantenimiento se ejecutará tras 24hs de encendido (1440 ticks).
    ticks_mantenimiento = 0 
    LIMIT_TICKS_DIA = 1440 
    
    while True:
        try:
            # --- TAREA A: VIGILANCIA (Cada minuto) ---
            try:
                # Crear sesión DB exclusiva para este hilo
                db = database.SessionLocal()
                # Ejecutar motor de alertas
                alerts_engine.procesar_offline(db)
            except Exception as e:
                print(f"⚠️ Error verificando alertas: {e}")
            finally:
                db.close() # Siempre cerrar sesión

            # --- TAREA B: MANTENIMIENTO (Cada 24 horas) ---
            ticks_mantenimiento += 1
            if ticks_mantenimiento >= LIMIT_TICKS_DIA:
                print("🧹 Ejecutando mantenimiento programado de DB...")
                try:
                    # Ejecutamos en un thread aparte para no congelar el servidor 
                    # mientras comprime datos (puede tardar unos segundos)
                    await asyncio.to_thread(maintenance.ejecutar_mantenimiento)
                except Exception as e:
                    print(f"❌ Error en tarea de mantenimiento: {e}")
                
                ticks_mantenimiento = 0 # Reiniciar contador para el próximo día

        except Exception as e:
            print(f"⚠️ Error crítico en ciclo de vigilancia: {e}")
        
        # Dormir 60 segundos hasta el próximo ciclo
        await asyncio.sleep(60)
        
@master_app.on_event("startup")
async def startup_event():
    # Al arrancar el servidor, iniciamos el ciclo en segundo plano
    asyncio.create_task(ciclo_vigilancia())
 
# --- 5. FUSIÓN DE RUTAS ---
master_app.include_router(listener_app.router)
master_app.mount("/", dashboard_app)
 
# --- 6. EJECUTAR ---
if __name__ == "__main__":
    uvicorn.run(master_app, host="0.0.0.0", port=8001)