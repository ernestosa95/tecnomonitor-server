import uvicorn
import sys
import os
import asyncio 
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from contextlib import asynccontextmanager 
import maintenance

# --- IMPORTACIÓN DEL SCRIPT DE LIMPIEZA ---
try:
    import limpiar_alertas
    print("✅ Script de limpieza de alertas cargado correctamente.")
except ImportError:
    from dashboard_app import limpiar_alertas
 
# --- 1. PREPARACIÓN DE RUTAS ---
current_dir = os.path.dirname(os.path.abspath(__file__))
dashboard_path = os.path.join(current_dir, "dashboard_app")
sys.path.insert(0, dashboard_path)

import database 
try:
    import alerts_engine
    print("✅ Alerts Engine cargado para servicio 24/7.")
except ImportError:
    from dashboard_app import alerts_engine
 
try:
    from main import app as listener_app
except Exception as e:
    print(f"❌ Error cargando Listener: {e}")
 
try:
    from dashboard_app.dashboard import app as dashboard_app
except Exception as e:
    print(f"❌ Error cargando Dashboard: {e}")
    sys.exit(1)

try:
    import asana_conector
except ImportError:
    from dashboard_app import asana_conector

# ============================================================
# INFORMES IA — módulo nuevo, montaje aislado. Si falla, no tumba el resto.
# ============================================================
from pathlib import Path
from dotenv import load_dotenv as _load_dotenv_informes_ia

# OJO: esto tiene que ejecutarse ANTES de cualquier "from informes_ia import...",
# porque informes_ia.config hace su propio load_dotenv() al importarse, y si
# no encuentra el .env correcto, revienta con ConfigError.
_INFORMES_IA_ENV_PATH = "/home/tecnoxaas/Documents/informes_ia_proyectos/.env"
_load_dotenv_informes_ia(_INFORMES_IA_ENV_PATH)

informes_ia_disponible = True
try:
    from informes_ia.config import get_settings as get_settings_ia
    from informes_ia.historial.almacen import AlmacenReportes
    from informes_ia.historial.servicio import ServicioReportes
    from informes_ia.pipeline_real import PipelineReal
    from informes_ia.worker import WorkerReportes
    try:
        import informes_ia_router as _informes_ia_router_mod
    except ImportError:
        from dashboard_app import informes_ia_router as _informes_ia_router_mod
    informes_ia_api_router = _informes_ia_router_mod.router
except Exception as e:
    print(f"⚠️ informes_ia no se pudo importar, el módulo queda desactivado: {repr(e)}")
    informes_ia_disponible = False

# --- BACKGROUND SERVICE ---
async def ciclo_vigilancia():
    print("🔄 Iniciando Hilo de Vigilancia (Background Service)...")
    ticks_mantenimiento = 0 
    ticks_limpieza = 0             # <--- NUEVO: Contador para la limpieza
    LIMIT_TICKS_DIA = 1440 
    LIMIT_TICKS_DOCE_HORAS = 720   # <--- NUEVO: Límite para ejecutar 2 veces al día (12 horas)
    
    while True:
        try:
            with database.SessionLocal() as db:
                # 1. Alertas de Hardware (Tiempo real)
                alerts_engine.procesar_offline(db)
                
                # 2. Alertas de Negocio (Programadas)
                alerts_engine.verificar_kpis_programados(db)

                # 3. Alertas de Integración/Software (Tiempo real)
                alerts_engine.verificar_estado_software(db)
                
        except Exception as e:
            print(f"⚠️ Error verificando alertas: {e}")

        # --- TAREA A: MANTENIMIENTO PROGRAMADO DE DB (Cada 24 horas) ---
        ticks_mantenimiento += 1
        if ticks_mantenimiento >= LIMIT_TICKS_DIA:
            print("🧹 Ejecutando mantenimiento programado de DB...")
            try:
                await asyncio.to_thread(maintenance.ejecutar_mantenimiento)
            except Exception as e:
                print(f"❌ Error en tarea de mantenimiento: {e}")
            ticks_mantenimiento = 0

        # --- TAREA B: LIMPIEZA DE ALERTAS HUÉRFANAS (Cada 12 horas) ---
        ticks_limpieza += 1
        if ticks_limpieza >= LIMIT_TICKS_DOCE_HORAS:
            print("🔄 [Automatización] Iniciando limpieza semestral/diaria de alertas huérfanas...")
            try:
                # Se ejecuta en un hilo separado para no bloquear las solicitudes web ni los WebSockets
                await asyncio.to_thread(limpiar_alertas.limpiar_alertas_huerfanas)
            except Exception as e:
                print(f"❌ Error en la limpieza automática de alertas: {e}")
            ticks_limpieza = 0

        await asyncio.sleep(60)


# --- LIFESPAN MANAGER ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 Iniciando eventos de ciclo de vida (Lifespan)...")
    try:
        asana_conector.verificar_conexion_asana()   # deja en el log si el token funciona o no
    except Exception as e:
        print(f"⚠️ No se pudo verificar Asana al inicio: {repr(e)}")
    vigilancia_task = asyncio.create_task(ciclo_vigilancia())

    # --- INFORMES IA: montaje aislado (no debe poder tumbar el resto de la app) ---
    app.state.servicio_reportes_ia = None
    app.state.almacen_reportes_ia = None
    app.state.worker_reportes_ia = None

    if informes_ia_disponible:
        try:
            settings_ia = get_settings_ia()
            pipeline_ia = PipelineReal(settings_ia)
            almacen_ia = AlmacenReportes(Path(current_dir) / "informes_ia_historial.sqlite")
            servicio_ia = ServicioReportes(
                almacen_ia,
                generador_ia=pipeline_ia.generador_ia,
                render_pdf=pipeline_ia.render_pdf,
                generar_grafico=pipeline_ia.generar_grafico_png,
                preparar_contexto=pipeline_ia.preparar_contexto,
                backoff_base_s=2.0,
            )
            worker_ia = WorkerReportes(servicio_ia)
            worker_ia.iniciar_en_thread()

            app.state.servicio_reportes_ia = servicio_ia
            app.state.almacen_reportes_ia = almacen_ia
            app.state.worker_reportes_ia = worker_ia
            print("✅ Módulo informes_ia montado, worker corriendo en background.")
        except Exception as e:
            print(f"⚠️ informes_ia no pudo inicializarse (queda en 503): {repr(e)}")

    yield 
    print("🛑 Apagando servidor, cancelando hilo de vigilancia...")
    vigilancia_task.cancel()

    # --- INFORMES IA: apagado limpio del worker, si llegó a levantar ---
    worker_ia_obj = getattr(app.state, "worker_reportes_ia", None)
    if worker_ia_obj:
        print("🛑 Deteniendo worker de informes_ia...")
        worker_ia_obj.detener()

# --- CREAR APP MAESTRA ---
master_app = FastAPI(title="TecnoMonitor Unificado", lifespan=lifespan)

# --- GESTOR DE WEBSOCKETS (Movido desde dashboard.py) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except:
                pass

manager = ConnectionManager()

# --- ENDPOINTS WEBSOCKET EN APP MAESTRA ---
@master_app.websocket("/ws/alertas")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@master_app.post("/api/internal/trigger-ws")
async def trigger_websocket_update():
    await manager.broadcast({"type": "ALERTA_UPDATE", "msg": "Hay cambios en las alertas"})
    return {"status": "ok"}

# --- FUSIÓN DE RUTAS ---
master_app.include_router(listener_app.router)
if informes_ia_disponible:
    master_app.include_router(informes_ia_api_router)   # ← NUEVO, antes del mount
master_app.mount("/", dashboard_app)
 
# --- EJECUTAR ---
if __name__ == "__main__":
    uvicorn.run(master_app, host="0.0.0.0", port=8001)