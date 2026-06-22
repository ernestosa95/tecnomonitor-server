import sys
import os
import json
import database
import matplotlib.dates as mdates
from database import HospitalMetadata, HistorialReportes, AlertaModel, ReporteModel
from database import HistorialReportes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
import io
import matplotlib
matplotlib.use('Agg') # Crucial para servidores: dibuja sin abrir ventanas
import matplotlib.pyplot as plt
from fastapi.responses import StreamingResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle
from reportlab.lib.utils import ImageReader
import numpy as np
import asana_conector
import auth
import time
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import re
from fastapi import Response
from fastapi.middleware.gzip import GZipMiddleware
import generator_report

import tempfile
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse
from schemas import DatosRISAnalytics

import csv
import os
from fastapi import FastAPI, Request, Form
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

ESTADOS_COLORS = {
    'Citados': '#cce5ff',
    'Admitidos': '#99ccff',
    'Ejecutados': '#66b2ff',
    'Asociados': '#3399ff',
    'Borradores': '#0080ff',
    'Definitivos': '#0066cc',
    'Suspendidos': '#004c99',
    'Almacenados': '#1abc9c' # Verde/Teal para PACS
}

# ==========================================
# ⚡ CACHÉ EN MEMORIA PARA EL DASHBOARD
# ==========================================
_cache_resumen = {"data": None, "ts": 0}
CACHE_TTL_SEGUNDOS = 30

# Ajuste de Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from database import HospitalMetadata 
import alerts_engine 
from dotenv import load_dotenv

load_dotenv()

base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "static")
templates_dir = os.path.join(base_dir, "templates")

app = FastAPI(title="TecnoXaas Dashboard")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# ==========================================
# 🛡️ RATE LIMITING (Control de tráfico)
# ==========================================
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ==========================================
# 🛡️ 1. CONFIGURACIÓN DE CORS
# ==========================================
# Aquí debes listar los dominios EXACTOS desde donde vas a entrar.
# Si lo vas a publicar, reemplaza el localhost por tu dominio real.
ORIGINES_PERMITIDOS = [
    "http://localhost",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
    "https://tecnomonitor.tecnoimagen.com.ar/"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINES_PERMITIDOS,
    allow_credentials=True,
    allow_methods=["*"], # Permite todos los métodos (GET, POST, PUT, etc)
    allow_headers=["Authorization", "Content-Type"], # Solo permitimos estas cabeceras
)

# ==========================================
# 🚀 1.5 COMPRESIÓN GZIP (Nuevo)
# ==========================================
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ==========================================
# 🛡️ 2. CABECERAS DE SEGURIDAD (Security Headers)
# ==========================================
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    
    # Evita que el sitio se incruste en un iframe malicioso (Clickjacking)
    response.headers["X-Frame-Options"] = "DENY"
    
    # Evita que el navegador adivine tipos de archivos maliciosos (MIME Sniffing)
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # Fuerza el uso de HTTPS por 1 año (HSTS) - Muy importante en producción
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    
    # Content-Security-Policy (CSP)
    # Adaptado específicamente para permitir Chart.js, Leaflet y los estilos inline de tu index.html
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com; "
        "img-src 'self' data: https://a.basemaps.cartocdn.com https://b.basemaps.cartocdn.com https://c.basemaps.cartocdn.com https://d.basemaps.cartocdn.com;"
    )
    response.headers["Content-Security-Policy"] = csp
    
    return response

# --- CONTROL DE SEGURIDAD EN MEMORIA ---
# Estructura: {"ip_cliente": {"intentos": int, "bloqueado_hasta": float}}
login_attempts_db = {}
MAX_INTENTOS_LOGIN = 5
TIEMPO_BLOQUEO_SEG = 300  # 5 minutos

# --- DTOs ACTUALIZADOS (Punto 1 y 2) ---
class ConfigRequest(BaseModel):
    # Generales
    offline_minutes: int
    disk_threshold: int
    
    # Host Físico
    temp_amb_max: int
    temp_cpu_max: int
    cpu_host_max: int      
    ram_host_max: int      
    
    # VMs
    cpu_vm_max: int        
    ram_vm_max: int        
    
    # Hardware Switches
    enable_fans: bool      
    enable_power: bool     
    enable_raid: bool     

    global_alert_responsible_email: str 

    # --- Parametros KPI ---
    kpi_execution_time: str
    kpi_rad_alert_enabled: bool
    kpi_rad_threshold_hours: int
    kpi_rad_modalities: str
    kpi_rad_responsible_email: str
    kpi_mamo_alert_enabled: bool
    kpi_mamo_threshold_days: int

    # --- Parametros Software ---
    mirth_alert_enabled: bool
    mirth_queued_threshold: int
    mirth_responsible_email: str

class LoginRequest(BaseModel):
    email: str
    password: str

class HospitalDTO(BaseModel):
    hospital_id: str
    nombre: str
    provincia: str = None
    latitud: str = None
    longitud: str = None
    asana_project_id: str = None
    is_visible: bool = True
    alerts_enabled: bool = True
    has_ris: bool = False

class ReportePDFRequest(BaseModel):
    hospital_id: str
    fecha_desde: str
    fecha_hasta: str
    alcance: str
    asana_task_id: str
    tipo_reporte: str = "clinico"

def get_db():
    db = database.SessionLocal()
    try: yield db
    finally: db.close()

# Gestor de WebSockets
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

# --- VISTAS ---
@app.get("/")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.websocket("/ws/alertas")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Mantenemos el canal abierto esperando mensajes del cliente (opcional)
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/api/internal/trigger-ws")
async def trigger_websocket_update():
    """Ruta interna para emitir el broadcast a todos los clientes conectados"""
    await manager.broadcast({"type": "ALERTA_UPDATE", "msg": "Hay cambios en las alertas"})
    return {"status": "ok"}

@app.get("/monitor")
def dashboard_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/api/login")
def verificar_login(request: Request, response: Response, login_data: LoginRequest, db: Session = Depends(get_db)):
    ip_cliente = request.client.host
    ahora = time.time()

    # 1. Verificar si la IP está bloqueada temporalmente
    estado_ip = login_attempts_db.get(ip_cliente)
    if estado_ip:
        if estado_ip['bloqueado_hasta'] > ahora:
            tiempo_restante = int((estado_ip['bloqueado_hasta'] - ahora) / 60)
            return {"success": False, "message": f"Demasiados intentos fallidos. Cuenta bloqueada por {tiempo_restante or 1} minuto(s) por seguridad."}
        elif estado_ip['bloqueado_hasta'] <= ahora and estado_ip['intentos'] >= MAX_INTENTOS_LOGIN:
            # El tiempo de castigo ya pasó, reseteamos el contador
            login_attempts_db[ip_cliente] = {'intentos': 0, 'bloqueado_hasta': 0}

    # Inicializar registro si la IP es nueva
    if ip_cliente not in login_attempts_db:
        login_attempts_db[ip_cliente] = {'intentos': 0, 'bloqueado_hasta': 0}

    # 2. Filtro de dominio obligatorio
    if not login_data.email.lower().endswith("@tecnoimagen.com.ar"):
        _registrar_intento_fallido(ip_cliente, ahora)
        return {"success": False, "message": "Dominio no autorizado"}

    # 3. Búsqueda de usuario y validación de clave
    user = db.query(database.UserModel).filter(database.UserModel.email == login_data.email.lower()).first()
    
    if not user or not auth.verify_password(login_data.password, user.hashed_password):
        bloqueado = _registrar_intento_fallido(ip_cliente, ahora)
        msg = "Demasiados intentos. Cuenta bloqueada." if bloqueado else "Credenciales inválidas"
        return {"success": False, "message": msg}

    if not user.is_active:
        return {"success": False, "message": "Usuario inactivo"}

    # 4. ¡Login Exitoso! Resetear los intentos de la IP
    login_attempts_db.pop(ip_cliente, None)

    # Generar token con el Rol incluido
    token = auth.create_access_token(data={"sub": user.email, "role": user.role})

    response.set_cookie(
        key="tecnomonitor_token",
        value=token,
        httponly=True,     # JS no puede leerla (Previene XSS)
        secure=True,      # Ponelo en True si ya estás usando HTTPS en producción
        samesite="Lax",    # Protege contra ataques CSRF
        max_age=28800      # Expira en 8 horas (en segundos)
    )

    return {
        "success": True,
        "token": token,
        "user": {
            "name": user.full_name,
            "email": user.email,
            "role": user.role
        }
    }

def _registrar_intento_fallido(ip_cliente: str, ahora: float) -> bool:
    """Suma un intento fallido y bloquea si es necesario. Devuelve True si se bloqueó."""
    login_attempts_db[ip_cliente]['intentos'] += 1
    if login_attempts_db[ip_cliente]['intentos'] >= MAX_INTENTOS_LOGIN:
        login_attempts_db[ip_cliente]['bloqueado_hasta'] = ahora + TIEMPO_BLOQUEO_SEG
        return True
    return False

@app.post("/api/logout")
def logout_usuario(response: Response):
    # Le decimos al navegador que borre la cookie
    response.delete_cookie("tecnomonitor_token", httponly=True, samesite="Lax")
    return {"success": True}

@app.get("/api/resumen-hospitales")
def obtener_resumen(db: Session = Depends(get_db), current_user: dict = Depends(auth.get_current_user)):
    global _cache_resumen
    ahora = time.time()

    # 1. Verificación del caché local
    if _cache_resumen["data"] is not None and (ahora - _cache_resumen.get("ts", 0)) < 30:
        return _cache_resumen["data"]
    
    hospitales_meta = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True
    ).all()
    
    resultado_final = []
    
    for hosp in hospitales_meta:
        # --- SECCIÓN A: INFRAESTRUCTURA (Último reporte de estado) ---
        ultimo_reporte = db.query(database.ReporteModel).filter(
            database.ReporteModel.hospital_id == hosp.hospital_id
        ).order_by(database.ReporteModel.timestamp.desc()).first()
        
        fecha_reporte = "Sin datos"
        estado_texto = "Offline"
        elementos = []
        
        if ultimo_reporte:
            fecha_reporte = str(ultimo_reporte.timestamp)[:19] 
            estado_texto = ultimo_reporte.host_status or "Offline"
            
            # 🛠️ FIX 1: Restaurada la lógica original para extraer los Nodos (VMs)
            try:
                if isinstance(ultimo_reporte.full_json_data, str):
                    data_json = json.loads(ultimo_reporte.full_json_data)
                else:
                    data_json = ultimo_reporte.full_json_data or {}
                    
                virtual_layer = data_json.get("virtual_layer", [])
                
                if isinstance(virtual_layer, list):
                    for vm in virtual_layer:
                        estado_vm = vm.get("state", "unknown").lower()
                        color_state = "success" if estado_vm in ["running", "online"] else ("warning" if estado_vm == "warning" else "error")
                        
                        elementos.append({
                            "label": vm.get("id", "VM"), 
                            "state": color_state
                        })
            except Exception:
                pass 

        # --- SECCIÓN B: MÉTRICAS HISTÓRICAS (PACS KPIs) ---
        todos_los_usos = db.query(database.ReporteUso).filter(
            database.ReporteUso.hospital_id == hosp.hospital_id
        ).all()
        
        estudios_pacs = 0
        estudios_ia = 0
        equipos_pacs = set()
        
        for uso in todos_los_usos:
            if uso.kpi_json_data:
                try:
                    kpis = json.loads(uso.kpi_json_data) if isinstance(uso.kpi_json_data, str) else uso.kpi_json_data
                    for item in kpis.get("pacs", []):
                        aet = item.get("aet", "").upper().strip()
                        
                        if aet and aet not in ['CLIENT', 'WADO', 'PACS']:
                            if aet.startswith("ENT_"):
                                estudios_ia += item.get("almacenados", 0)
                            else:
                                # Si no, es un estudio de equipo médico estándar
                                estudios_pacs += item.get("almacenados", 0)
                                equipos_pacs.add(aet)
                except Exception:
                    pass

        # --- SECCIÓN C: CONSTRUCCIÓN DEL OBJETO FINAL ---
        resultado_final.append({
            "raw_id": hosp.hospital_id,
            "id": hosp.hospital_id,
            "name": hosp.nombre,
            "timestamp": fecha_reporte,
            "status": estado_texto,
            "elements": elementos,
            "kpi_estudios": estudios_pacs,
            "kpi_equipos": list(equipos_pacs),
            "kpi_estudios_ia": estudios_ia
        })
        
    # Guardar en la caché global (Nota: usamos "ts" como espera script.js)
    _cache_resumen["data"] = resultado_final
    _cache_resumen["ts"] = ahora
    
    return resultado_final


@app.get("/api/hospital/{hospital_id}")
def obtener_detalle_hospital(hospital_id: str,
                             db: Session = Depends(get_db),
                             current_user: dict = Depends(auth.get_current_user)):
    query = text("SELECT * FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1")
    result = db.execute(query, {"hid": hospital_id}).fetchone()
    if not result: return {"error": "Hospital no encontrado"}
    try:
        full_data = json.loads(result.full_json_data) if result.full_json_data else {}
        full_data['db_timestamp'] = str(result.timestamp)[:19].replace("T", " ")
        return full_data
    except Exception: return {"error": "Error procesando datos"}

# --- EN DASHBOARD.PY ---
# 1. Devuelve esta función a su estado original simplificado
@app.get("/api/hospital/{hospital_id}/history")
def obtener_historial(hospital_id: str, horas: int = 24,
                      db: Session = Depends(get_db),
                      current_user: dict = Depends(auth.get_current_user)):
    flimit = datetime.now() - timedelta(hours=horas)
    
    # 🛡️ FIX: Agregamos LIMIT 15000 para evitar desbordamientos de memoria
    query = text("""
        SELECT timestamp, host_cpu_usage, full_json_data 
        FROM reportes_historicos 
        WHERE hospital_id = :hid AND timestamp >= :flimit 
        ORDER BY timestamp ASC
        LIMIT 15000
    """)
    result = db.execute(query, {"hid": hospital_id, "flimit": flimit}).fetchall()
    
    if not result: return []

    # 🛡️ FIX: Downsampling agresivo. Nunca devolvemos más de ~600 puntos al frontend.
    total_registros = len(result)
    step = 1
    if total_registros > 600: 
        step = max(1, int(total_registros / 600))
    muestras = result[::step]
    
    historial = []
    for row in muestras:
        try:
            if isinstance(row.full_json_data, str):
                d = json.loads(row.full_json_data) if row.full_json_data else {}
            else:
                d = row.full_json_data if row.full_json_data else {}
                
            # 1. Datos Físicos
            phy = d.get("physical_layer") or d.get("physical_host") or {}
            tele = phy.get("telemetry") or {}
            
            cpu_val = row.host_cpu_usage
            if cpu_val is None:
                cpu_val = (tele.get("cpu") or {}).get("usage_percent", 0)
                
            sensors = phy.get("sensors") or (d.get("environment") or {}).get("thermal") or {}
            temps_list = sensors.get("temperatures") or sensors.get("cpu_temps") or []
            
            cpu_s = {}
            for x in temps_list:
                val = x.get("value") if x.get("value") is not None else x.get("temp_c")
                name = x.get("name") or x.get("sensor")
                if val is not None and name:
                    cpu_s[name] = val
            
            amb_val = sensors.get("ambient_temp_c")
            if amb_val is None:
                for x in temps_list:
                    if "Ambient" in (x.get("name") or ""): 
                        amb_val = x.get("value")
                        break

            # 2. Datos Virtuales
            vms_data = {}
            if "virtual_layer" in d and isinstance(d["virtual_layer"], list):
                for vm in d["virtual_layer"]:
                    vid = vm.get("id")
                    if vid: 
                        vms_data[vid] = {
                            "cpu": (vm.get("telemetry") or {}).get("cpu", {}).get("usage_percent", 0), 
                            "ram": (vm.get("telemetry") or {}).get("ram", {}).get("usage_percent", 0)
                        }
            elif "vms" in d and isinstance(d["vms"], dict):
                for k, v in d["vms"].items():
                    m = v.get("metrics") or {}
                    vms_data[k] = {
                        "cpu": m.get("cpu_load_percent", 0),
                        "ram": (m.get("ram") or {}).get("percent", 0)
                    }

            historial.append({
                "timestamp": str(row.timestamp)[:19].replace("T", " "),
                "global": {"cpu_host": cpu_val, "temp_amb": amb_val, "cpu_sensors": cpu_s},
                "vms": vms_data
            })
        except Exception as e:
            continue
        
    return historial

# --- NUEVA RUTA PARA KPIS (Añadir a dashboard.py) ---
@app.get("/api/hospital/{hospital_id}/kpi-history")
def obtener_historial_kpi(hospital_id: str, horas: int = 24,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.get_current_user)):
    
    fecha_limite_real = datetime.now() - timedelta(hours=horas)
    fecha_limite_sql = fecha_limite_real - timedelta(days=3)
    
    # 🛡️ FIX: Agregamos LIMIT 15000 como cap absoluto
    query = text("""
        SELECT timestamp, kpi_json_data 
        FROM reportes_uso 
        WHERE hospital_id = :hid AND timestamp >= :flimit 
        ORDER BY timestamp ASC
        LIMIT 15000
    """)
    result = db.execute(query, {"hid": hospital_id, "flimit": fecha_limite_sql}).fetchall()
    
    if not result: return []

    historial_kpi = []
    
    for row in result:
        try:
            metrics = json.loads(row.kpi_json_data) if row.kpi_json_data else {}
            fecha_extraccion_str = metrics.get("start_time_extraction")
            
            if fecha_extraccion_str:
                try:
                    fecha_evento = datetime.fromisoformat(fecha_extraccion_str)
                except ValueError:
                    fecha_evento = datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp
            else:
                fecha_evento = datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp
                
            if fecha_evento >= fecha_limite_real:
                historial_kpi.append({
                    "timestamp": fecha_evento.strftime("%Y-%m-%d %H:%M:%S"),
                    "application_metrics": metrics
                })
        except Exception as e:
            continue
            
    historial_kpi.sort(key=lambda x: x["timestamp"])
    
    return historial_kpi

@app.get("/api/alertas")
def obtener_alertas(db: Session = Depends(get_db),
                    # CORRECCIÓN: Solo roles autorizados pueden ver alertas
                    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial"))):
    activas = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1).order_by(database.AlertaModel.start_time.desc()).all()
    historial = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 0).order_by(database.AlertaModel.end_time.desc()).limit(50).all()
    return {"activas": activas, "historial": historial}

# --- CONFIGURACIÓN ACTUALIZADA (Punto 1) ---
@app.get("/api/config")
def obtener_configuracion(db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    
    # NUEVA FUNCIÓN 'g' (Igual a la del alerts_engine)
    def g(k, d, is_bool=False):
        r = db.query(database.ConfigModel).filter_by(clave=k).first()
        if r:
            if is_bool:
                return r.valor == '1'
            if isinstance(d, int):
                try:
                    return int(r.valor)
                except (ValueError, TypeError):
                    return d
            return r.valor
        return d

    return {
        "offline_minutes": g("offline_minutes", 10),
        "disk_threshold": g("disk_threshold", 90),
        "temp_amb_max": g("temp_amb_max", 27),
        "temp_cpu_max": g("temp_cpu_max", 75),
        "cpu_host_max": g("cpu_host_max", 85),
        "ram_host_max": g("ram_host_max", 90),
        "cpu_vm_max": g("cpu_vm_max", 90),
        "ram_vm_max": g("ram_vm_max", 90),
        "enable_fans": g("enable_fans", True, is_bool=True),
        "enable_power": g("enable_power", True, is_bool=True),
        "enable_raid": g("enable_raid", True, is_bool=True),
        "global_alert_responsible_email": g("global_alert_responsible_email", ""),
        
        # --- PARÁMETROS KPI ---
        "kpi_execution_time": g("kpi_execution_time", "08:00"),
        "kpi_rad_alert_enabled": g("kpi_rad_alert_enabled", False, is_bool=True),
        "kpi_rad_threshold_hours": g("kpi_rad_threshold_hours", 24),
        "kpi_rad_modalities": g("kpi_rad_modalities", "DX,CR,MAMO"),
        "kpi_mamo_alert_enabled": g("kpi_mamo_alert_enabled", False, is_bool=True),
        "kpi_mamo_threshold_days": g("kpi_mamo_threshold_days", 7),
        "kpi_rad_responsible_email": g("kpi_rad_responsible_email", ""),

        # --- NUEVAS CONFIGURACIONES DE MIRTH ---
        "mirth_alert_enabled": g("mirth_alert_enabled", False, is_bool=True),
        "mirth_queued_threshold": g("mirth_queued_threshold", 100),
        "mirth_responsible_email": g("mirth_responsible_email", "")
    }


@app.post("/api/config")
def guardar_configuracion(cfg: ConfigRequest,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    def s(k, v):
        c = db.query(database.ConfigModel).filter_by(clave=k).first()
        val_str = "1" if v is True else "0" if v is False else str(v)
        if not c: db.add(database.ConfigModel(clave=k, valor=val_str))
        else: c.valor = val_str
    
    s("offline_minutes", cfg.offline_minutes)
    s("disk_threshold", cfg.disk_threshold)
    s("temp_amb_max", cfg.temp_amb_max)
    s("temp_cpu_max", cfg.temp_cpu_max)
    s("cpu_host_max", cfg.cpu_host_max)
    s("ram_host_max", cfg.ram_host_max)
    s("cpu_vm_max", cfg.cpu_vm_max)
    s("ram_vm_max", cfg.ram_vm_max)
    s("enable_fans", cfg.enable_fans)
    s("enable_power", cfg.enable_power)
    s("enable_raid", cfg.enable_raid)
    s("kpi_execution_time", cfg.kpi_execution_time)
    s("kpi_rad_alert_enabled", cfg.kpi_rad_alert_enabled)
    s("kpi_rad_threshold_hours", cfg.kpi_rad_threshold_hours)
    s("kpi_rad_modalities", cfg.kpi_rad_modalities)
    s("kpi_rad_responsible_email", cfg.kpi_rad_responsible_email)
    s("global_alert_responsible_email", cfg.global_alert_responsible_email)
    s("kpi_mamo_alert_enabled", cfg.kpi_mamo_alert_enabled)
    s("kpi_mamo_threshold_days", cfg.kpi_mamo_threshold_days)
    # --- GUARDAR CONFIGURACIONES DE MIRTH ---
    s("mirth_alert_enabled", cfg.mirth_alert_enabled)
    s("mirth_queued_threshold", cfg.mirth_queued_threshold)
    s("mirth_responsible_email", cfg.mirth_responsible_email)
    
    db.commit()
    return {"status": "ok", "msg": "Configuración actualizada"}

# --- METADATA HOSPITALES (Punto 2) ---
@app.get("/api/hospitales-metadata")
def listar_hospitales_metadata(db: Session = Depends(get_db),
                               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    return db.query(HospitalMetadata).all()

@app.post("/api/hospitales-metadata")
def crear_hospital_metadata(dto: HospitalDTO,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    existe = db.query(HospitalMetadata).filter_by(hospital_id=dto.hospital_id).first()
    if existe: raise HTTPException(status_code=400, detail="El ID existe")
    nuevo = HospitalMetadata(**dto.dict())
    db.add(nuevo); db.commit()
    return {"status": "ok", "msg": "Creado"}

@app.put("/api/hospitales-metadata/{hid}")
def editar_hospital_metadata(hid: str, dto: HospitalDTO,
                             db: Session = Depends(get_db),
                             current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    
    # Actualizamos campos
    h.nombre = dto.nombre
    h.provincia = dto.provincia
    h.latitud = dto.latitud
    h.longitud = dto.longitud
    h.asana_project_id = dto.asana_project_id
    h.is_visible = dto.is_visible
    h.alerts_enabled = dto.alerts_enabled
    h.has_ris = dto.has_ris
    
    db.commit()
    return {"status": "ok", "msg": "Actualizado"}

@app.patch("/api/hospitales-metadata/{hid}/toggle")
def toggle_visibilidad(hid: str,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.is_visible = not h.is_visible
    db.commit()
    return {"status": "ok", "new_state": h.is_visible}

# Nueva ruta para togglear alertas (Punto 2)
@app.patch("/api/hospitales-metadata/{hid}/toggle-alerts")
def toggle_alertas(hid: str,
                   db: Session = Depends(get_db),
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.alerts_enabled = not h.alerts_enabled
    db.commit()
    return {"status": "ok", "alerts_enabled": h.alerts_enabled}

@app.patch("/api/hospitales-metadata/{hid}/toggle-ris")
def toggle_ris(hid: str,
               db: Session = Depends(get_db),
               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    
    # Invertimos el valor actual (asume False si es None)
    h.has_ris = not getattr(h, 'has_ris', False)
    db.commit()
    return {"status": "ok", "has_ris": h.has_ris}

@app.delete("/api/hospitales-metadata/{hid}")
def eliminar_hospital_metadata(hid: str,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(auth.require_roles("Admin"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    db.delete(h); db.commit()
    return {"status": "ok", "msg": "Eliminado"}

@app.get("/api/mapa-data")
def obtener_datos_mapa(db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.get_current_user)):
    conf = db.query(database.ConfigModel).filter_by(clave="offline_minutes").first()
    limit_min = int(conf.valor) if (conf and conf.valor) else 10
    limit_delta = timedelta(minutes=limit_min)
    ahora = datetime.now()

    hospitales = db.query(HospitalMetadata).filter(
        HospitalMetadata.is_visible == True,
        HospitalMetadata.latitud != None,
        HospitalMetadata.latitud != "",
        HospitalMetadata.longitud != None,
        HospitalMetadata.longitud != ""
    ).all()

    mapa_data = []

    for h in hospitales:
        last_report = db.execute(
            text("SELECT timestamp FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1"),
            {"hid": h.hospital_id}
        ).fetchone()

        status = "Offline"
        if last_report:
            last_seen = last_report.timestamp
            if isinstance(last_seen, str):
                try: last_seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S.%f")
                except: pass
            if (ahora - last_seen) <= limit_delta:
                status = "Online"

        try:
            mapa_data.append({
                "id": h.hospital_id,
                "nombre": h.nombre,
                "status": status,
                "lat": float(h.latitud),
                "lng": float(h.longitud)
            })
        except ValueError: continue

    return mapa_data

# ==========================================
# --- MOTOR DE GENERACIÓN DE PDF ---
# ==========================================

# Paleta de colores oficial del frontend
COLORS_CHART = ['#004c99', '#0066cc', '#0080ff', '#3399ff', '#66b2ff', '#99ccff', '#cce5ff']

def generar_grafico_dona(datos: dict):
    """ Dibuja el gráfico en memoria y devuelve la imagen lista """
    labels = list(datos.keys())
    sizes = list(datos.values())
    total = sum(sizes)
    
    if total == 0:
        labels, sizes = ["Sin Datos"], [1]
        colores = ['#ecf0f1']
    else:
        colores = COLORS_CHART[:len(labels)]

    fig, ax = plt.subplots(figsize=(3, 3), subplot_kw=dict(aspect="equal"))
    
    # Dibujar Dona
    wedges, texts = ax.pie(sizes, colors=colores, wedgeprops=dict(width=0.3, edgecolor='white', linewidth=2))
    
    # Texto Central
    ax.text(0, 0.15, "TOTAL", ha='center', va='center', fontsize=10, color='#7f8c8d', fontweight='bold')
    ax.text(0, -0.15, f"{total:,}".replace(',', '.'), ha='center', va='center', fontsize=20, fontweight='bold', color='#2c3e50')
    
    # Guardar en memoria RAM
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf, total, colores

def generar_grafico_temporal(datos_equipo):
    """ Dibuja barras apiladas (RIS) vs barras simples (PACS) por tiempo """
    labels = sorted(list(datos_equipo.keys()))
    if not labels:
        return None
        
    x = np.arange(len(labels))
    width = 0.35  # Ancho de las barras

    # AJUSTE: Más margen inferior (bottom) para que entren las fechas largas
    fig, ax = plt.subplots(figsize=(8, 3.5))
    fig.subplots_adjust(bottom=0.45) 
    
    bottom_ris = np.zeros(len(labels))
    
    # 1. Dibujar barras apiladas de RIS
    estados_ris = ['Citados', 'Admitidos', 'Ejecutados', 'Asociados', 'Borradores', 'Definitivos', 'Suspendidos']
    for estado in estados_ris:
        valores = [datos_equipo[l].get(estado.lower(), 0) for l in labels]
        if sum(valores) > 0:
            ax.bar(x - width/2, valores, width, bottom=bottom_ris, color=ESTADOS_COLORS[estado], label=estado)
            bottom_ris += np.array(valores)

    # 2. Dibujar barra de PACS
    valores_pacs = [datos_equipo[l].get('almacenados', 0) for l in labels]
    if sum(valores_pacs) > 0:
        ax.bar(x + width/2, valores_pacs, width, color=ESTADOS_COLORS['Almacenados'], label='Almacenados')

    # Estética del gráfico
    ax.set_xticks(x)
    # AJUSTE: Fuente más chica (6) y rotación de 60 grados
    ax.set_xticklabels(labels, rotation=60, ha='right', fontsize=6) 
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.25), ncol=4, fontsize=8, frameon=False)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf

@app.post("/api/informes/pdf")
@limiter.limit("5/minute")
def generar_reporte_pdf(request: Request,
                        req: ReportePDFRequest,
                        db: Session = Depends(get_db),
                        current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial"))):
    
    # 1. Delegar a la nueva lógica separada
    if req.tipo_reporte == "infra":
        result = generator_report.generar_pdf_infra(req, db)
    else:
        result = generator_report.generar_pdf_clinico(req, db)

    # 2. Manejo de errores controlados
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    # 3. Respuesta según resultado de Asana
    pdf_bytes = result["pdf_bytes"]
    filename = result["filename"]
    asana_url = result["asana_url"]

    if asana_url:
        return {
            "status": "success", 
            "asana_url": asana_url, 
            "message": "Adjuntado a Asana correctamente"
        }
    else:
        # Descarga forzada local en caso de error en Asana
        buffer_pdf = io.BytesIO(pdf_bytes)
        buffer_pdf.seek(0)
        return StreamingResponse(
            buffer_pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    
def generar_reporte_infra_pdf(req, db):
    hospital = db.query(HospitalMetadata).filter_by(hospital_id=req.hospital_id).first()
    nombre_hosp = hospital.nombre if hospital else "Hospital Desconocido"

    def parse_f(s): return datetime.strptime(s, "%Y-%m-%d")
    f_ini = parse_f(req.fecha_desde)
    f_fin = parse_f(req.fecha_hasta) + timedelta(days=1)
    
    query = text("""
        SELECT timestamp, host_cpu_usage, host_ram_usage, full_json_data 
        FROM reportes_historicos 
        WHERE hospital_id = :hid AND timestamp BETWEEN :f1 AND :f2
        ORDER BY timestamp ASC
    """)
    result = db.execute(query, {"hid": req.hospital_id, "f1": f_ini, "f2": f_fin}).fetchall()

    if not result:
        return {"error": "No hay datos para el periodo"}

    # --- PROCESAMIENTO DE KPIs ---
    metrics_host = {"cpu": [], "ram": []}
    for row in result:
        data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
        tele = (data.get("physical_layer") or {}).get("telemetry") or {}
        cpu_p = tele.get("cpu", {}).get("usage_percent")
        ram_p = tele.get("ram", {}).get("usage_percent")
        if cpu_p is not None: metrics_host["cpu"].append(cpu_p)
        if ram_p is not None: metrics_host["ram"].append(ram_p)

    ultimo_json = json.loads(result[-1].full_json_data) if isinstance(result[-1].full_json_data, str) else result[-1].full_json_data
    phy = ultimo_json.get("physical_layer") or {}
    vms_raw = ultimo_json.get("virtual_layer") or []

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    ancho, alto = A4

    # --- HELPER LOCAL: Encabezado y pie de página ---
    def _encabezado(titulo_hoja, num_pagina):
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.setFillColorRGB(0.9, 0.9, 0.9)
        c.roundRect(40, alto - 60, 45, 25, 4, fill=1, stroke=1)
        c.setFillColorRGB(0.17, 0.24, 0.31)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(45, alto - 53, req.hospital_id)
        c.drawString(95, alto - 53, nombre_hosp[:40])
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, alto - 85, titulo_hoja)
        c.setFont("Helvetica-Bold", 14)
        c.setFillColorRGB(0.16, 0.5, 0.72)
        c.drawString(ancho / 2 - 60, 30, "TECNOIMAGEN")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawString(ancho - 100, 30, f"Página {num_pagina}")
        return alto - 130

    # --- PÁGINA 1 ---
    pos_y = _encabezado("REPORTE DE SALUD DE INFRAESTRUCTURA (IT)", 1)

    # Bloque KPIs
    c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setFillColorRGB(0.98, 0.98, 0.98)
    c.roundRect(40, pos_y - 70, ancho - 80, 70, 10, fill=1, stroke=1)
    
    def draw_kpi(label, val, x, y, color=(0,0,0)):
        c.setFillColorRGB(0.5, 0.5, 0.5); c.setFont("Helvetica", 8); c.drawString(x, y, label)
        c.setFillColorRGB(*color); c.setFont("Helvetica-Bold", 14); c.drawString(x, y - 18, val)

    uptime_pct = min(100.0, (len(result) / ((f_fin - f_ini).total_seconds() / 60)) * 100)
    draw_kpi("UPTIME ESTIMADO", f"{round(uptime_pct, 2)}%", 60, pos_y - 25, (0.15, 0.68, 0.37))
    draw_kpi("AVG CPU HOST", f"{round(np.mean(metrics_host['cpu']), 1) if metrics_host['cpu'] else 'N/A'}%", 210, pos_y - 25)
    draw_kpi("AVG RAM HOST", f"{round(np.mean(metrics_host['ram']), 1) if metrics_host['ram'] else 'N/A'}%", 360, pos_y - 25)
    pos_y -= 95

    # Gráfico Suavizado
    c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, pos_y, "ESTADO DE SENSORES Y EVOLUCIÓN TÉRMICA")
    pos_y -= 10
    
    img_temp = generar_grafico_temperaturas_infra(result)
    if img_temp:
        c.drawImage(ImageReader(img_temp), 35, pos_y - 210, width=ancho-70, height=210, mask='auto')
        pos_y -= 230

    # Tabla Sensores y RAID
    if phy:
        # Sensores
        temps = phy.get("sensors", {}).get("temperatures", [])
        if temps:
            data_t = [["Sensor", "Valor Actual", "Estado"]] + [[t.get("name")[:40], f"{t.get('value')} {t.get('unit')}", t.get("status")] for t in temps[:4]]
            t = Table(data_t, colWidths=[200, 80, 80])
            t.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),8), ('GRID',(0,0),(-1,-1),0.5,colors.grey), ('BACKGROUND',(0,0),(-1,0),colors.whitesmoke)]))
            tw, th = t.wrap(0,0); t.drawOn(c, 40, pos_y - th); pos_y -= (th + 20)

        # RAID (Añadido)
        vols = phy.get("storage_layer", {}).get("logical_volumes", [])
        if vols:
            c.setFont("Helvetica-Bold", 10); c.drawString(40, pos_y, "Almacenamiento Físico (RAID)")
            pos_y -= 12
            data_v = [["Volumen", "RAID", "Tamaño", "Estado"]] + [[v.get("name"), v.get("raid_level"), f"{v.get('size_gb')} GB", v.get("status")] for v in vols]
            t_v = Table(data_v, colWidths=[120, 100, 70, 70])
            t_v.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.5,colors.grey)]))
            tw, th = t_v.wrap(0,0); t_v.drawOn(c, 40, pos_y - th); pos_y -= (th + 25)

    # Referencias al pie
    pos_ref = 80
    c.setDash(1, 2); c.setStrokeColorRGB(0.7, 0.7, 0.7); c.line(40, pos_ref + 15, ancho - 40, pos_ref + 15); c.setDash()
    c.setFillColorRGB(0.4, 0.4, 0.4); c.setFont("Helvetica-BoldOblique", 8); c.drawString(40, pos_ref, "REFERENCIAS TÉCNICAS:")
    glosario = [
        ("• Uptime Estimado:", "Disponibilidad del agente basada en el conteo de reportes de telemetría recibidos."),
        ("• AVG CPU / RAM:", "Carga promedio de procesamiento y memoria del servidor físico durante el período."),
        ("• Sensores / RAID:", "Estado de salud del hardware capturado en el último reporte válido enviado.")
    ]
    gy = pos_ref - 12
    for tit, des in glosario:
        c.setFont("Helvetica-Bold", 7); c.drawString(40, gy, tit)
        c.setFont("Helvetica", 7); c.drawString(120, gy, des); gy -= 10

    # --- PÁGINA 2 ---
    c.showPage()
    pos_y = _encabezado("DETALLE DE CAPA VIRTUAL E INCIDENTES", 2)

    if vms_raw:
        c.setFont("Helvetica-Bold", 11); c.drawString(40, pos_y, "RECURSOS POR MÁQUINA VIRTUAL")
        pos_y -= 20
        for vm in vms_raw:
            if pos_y < 150: # Salto de página
                c.showPage(); pos_y = _encabezado("DETALLE CAPA VIRTUAL (CONT.)", 3)
            
            c.setFont("Helvetica-Bold", 9); c.setFillColorRGB(0.2, 0.4, 0.6)
            c.drawString(40, pos_y, f"■ {vm.get('id')} - Estado: {vm.get('state')}")
            pos_y -= 15
            
            # Discos
            discos = vm.get("storage", [])
            if discos:
                data_d = [["Disco", "Uso %", "Libre"]] + [[d.get("mount_point"), f"{d.get('usage_percent')}%", f"{d.get('free_gb')} GB"] for d in discos]
                t_d = Table(data_d, colWidths=[80, 50, 80])
                t_d.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.2,colors.grey)]))
                tw, th = t_d.wrap(0,0); t_d.drawOn(c, 60, pos_y - th); pos_y -= (th + 15)

    # Tabla Incidentes
    alertas = db.query(AlertaModel).filter(AlertaModel.hospital_id == req.hospital_id, AlertaModel.start_time >= f_ini).all()
    if alertas:
        c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(0.7, 0.1, 0.1)
        c.drawString(40, pos_y - 10, "HISTORIAL DE INCIDENTES RELEVANTES"); pos_y -= 30
        data_a = [["Inicio", "Tipo", "Mensaje", "Estado"]] + [[a.start_time.strftime("%d/%m %H:%M"), a.tipo[:15], a.mensaje[:65], "OK"] for a in alertas[:12]]
        t_a = Table(data_a, colWidths=[70, 110, 285, 50])
        t_a.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.5,colors.grey), ('VALIGN',(0,0),(-1,-1),'MIDDLE'), ('TEXTCOLOR',(0,1),(-1,-1),colors.darkred)]))
        tw, th = t_a.wrap(0,0); t_a.drawOn(c, 40, pos_y - th)

    c.save()
    
    # Manejo de Historial y Asana (Como lo tenías en el archivo original)
    filename = f"Infra_{req.hospital_id}_{req.fecha_desde}.pdf"
    pdf_bytes = buffer.getvalue()
    asana_url = asana_conector.adjuntar_pdf_a_tarea(req.asana_task_id, pdf_bytes, filename)
    
    nuevo_reg = HistorialReportes(hospital_id=req.hospital_id, tipo_reporte="Infraestructura IT", fecha_desde=req.fecha_desde, fecha_hasta=req.fecha_hasta,
                                  estado="Completado" if asana_url else "Descargado", asana_url=asana_url)
    db.add(nuevo_reg); db.commit()

    if asana_url: return {"status": "success", "asana_url": asana_url}
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})

def generar_grafico_temperaturas_infra(result):
    if not result: return None
    
    # --- PASO 1: Recolección de datos en estructura plana ---
    # Usamos una lista de tuplas (timestamp, nombre_sensor, valor)
    # para evitar cualquier desincronización entre fechas y arrays de sensores.
    registros_planos = []
    
    for row in result:
        try:
            ts = row.timestamp
            if isinstance(ts, str):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                    try:
                        ts = datetime.strptime(ts, fmt)
                        break
                    except:
                        continue
            
            # Si después del parsing ts sigue siendo string, saltamos este registro
            if isinstance(ts, str):
                continue

            data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
            if not data:
                continue
                
            phy = data.get("physical_layer") or {}
            sensors = phy.get("sensors") or {}
            temps = sensors.get("temperatures") or []
            
            for t in temps:
                name = t.get("name") or "Desc"
                val = t.get("value")
                # Solo registramos si el valor es numérico válido
                if val is not None:
                    try:
                        registros_planos.append((ts, name, float(val)))
                    except (TypeError, ValueError):
                        continue
        except:
            continue

    if not registros_planos:
        return None

    # --- PASO 2: Pivot — construimos una serie temporal por sensor ---
    # Recolectamos todos los timestamps únicos (ordenados) y todos los sensores únicos.
    todos_ts = sorted(set(r[0] for r in registros_planos))
    
    if len(todos_ts) < 5:
        return None

    todos_sensores = sorted(set(r[1] for r in registros_planos))
    
    # Índice rápido: {(ts, sensor): valor}
    indice = {(r[0], r[1]): r[2] for r in registros_planos}
    
    # Para cada sensor construimos un array del mismo largo que todos_ts,
    # rellenando con NaN donde no hay dato. Esto garantiza len(fechas) == len(y).
    data_sensores = {}
    for sname in todos_sensores:
        data_sensores[sname] = [
            indice.get((ts, sname), float('nan')) for ts in todos_ts
        ]

    if not data_sensores:
        return None

    # --- PASO 3: Dibujo con suavizado ---
    window_size = min(12, max(1, len(todos_ts) // 20))  # Adaptativo según cantidad de puntos
    
    fig, ax = plt.subplots(figsize=(11, 4))
    
    for sname, valores in data_sensores.items():
        y = np.array(valores, dtype=float)
        
        # Interpolación de NaNs internos (no extrapolamos extremos)
        indices_validos = np.where(~np.isnan(y))[0]
        if len(indices_validos) < 2:
            continue  # Sensor con casi sin datos, no lo graficamos
        
        # Interpolamos solo los huecos internos
        y_interp = np.copy(y)
        y_interp[np.isnan(y_interp)] = np.interp(
            np.where(np.isnan(y_interp))[0],
            indices_validos,
            y[indices_validos]
        )
        
        # Suavizado con ventana adaptativa
        kernel = np.ones(window_size) / window_size
        y_smooth = np.convolve(y_interp, kernel, mode='same')
        
        # Verificación de seguridad: ambas dimensiones deben coincidir
        if len(todos_ts) != len(y_smooth):
            # Fallback: graficamos sin suavizado
            y_smooth = y_interp
        
        ax.plot(todos_ts, y_smooth, label=sname, linewidth=1.5, alpha=0.8)
    
    # Si ningún sensor pudo graficarse, cerramos y retornamos None
    if not ax.lines:
        plt.close(fig)
        return None

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m %H:%M'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=3, fontsize=8, frameon=False)
    ax.grid(True, linestyle='--', alpha=0.3)
    plt.xticks(rotation=15, fontsize=8)
    plt.tight_layout()
    
    img_buf = io.BytesIO()
    fig.savefig(img_buf, format='png', dpi=130)
    plt.close(fig)
    img_buf.seek(0)
    return img_buf

@app.get("/api/informes/historial")
def obtener_historial_reportes(db: Session = Depends(get_db),
                               # CORRECCIÓN: Restringimos los roles explícitamente
                               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial"))):
    # Traemos los últimos 15 reportes generados
    historial = db.query(HistorialReportes).order_by(HistorialReportes.fecha_generacion.desc()).limit(15).all()
    
    resultados = []
    for h in historial:
        resultados.append({
            "id": h.id,
            "hospital_id": h.hospital_id,
            "tipo_reporte": h.tipo_reporte,
            "periodo": f"{h.fecha_desde} ➔ {h.fecha_hasta}",
            "fecha_generacion": h.fecha_generacion.strftime("%d/%m/%Y %H:%M"),
            "estado": h.estado,
            "asana_url": h.asana_url
        })
    return resultados

@app.get("/herramientas")
async def get_herramientas(request: Request):
    # Asumiendo que tu variable de templates se llama 'templates'
    return templates.TemplateResponse("herramientas.html", {"request": request})

@app.get("/ris-analytics")
async def get_ris_analytics(request: Request):
    return templates.TemplateResponse("solucion2.html", {"request": request})

@app.get("/hl7-analytics")
async def get_hl7_analytics(request: Request):
    return templates.TemplateResponse("solucion1.html", {"request": request})

@app.get("/tecno-solution")
async def get_tecno_solutions(request: Request):
    return templates.TemplateResponse("links.html", {"request": request})

@app.post("/submit-lead")
async def handle_form(
    nombre_apellido: str = Form(...),
    institucion: str = Form(...),
    cargo: str = Form(...),
    provincia: str = Form(...),
    volumen_estudios: str = Form(...),
    desafio_principal: str = Form(...),
    preferencia_contacto: str = Form(...),
    interes_poc: str = Form(...)
):
    file_path = "leads_evento_links.csv"
    file_exists = os.path.isfile(file_path)

    # Definimos los encabezados según tus requerimientos 
    headers = [
        "Nombre y Apellido", "Institución", "Cargo", "Provincia", 
        "Volumen Estudios", "Desafío Principal", "Preferencia Contacto", "Interés POC"
    ]

    try:
        with open(file_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # Si el archivo es nuevo, escribimos la cabecera
            if not file_exists:
                writer.writerow(headers)
            
            # Escribimos los datos del usuario
            writer.writerow([
                nombre_apellido, institucion, cargo, provincia, 
                volumen_estudios, desafio_principal, preferencia_contacto, interes_poc
            ])
        
        return JSONResponse(content={"status": "success", "message": "Datos guardados correctamente"})
    
    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

# --- DTO para cambio de clave ---
class ChangePasswordRequest(BaseModel):
    email: str
    current_password: str
    new_password: str

# --- FUNCIÓN DE VALIDACIÓN DE CONTRASEÑAS ---
def validar_password(pw: str):
    if len(pw) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres.")
    if not re.search(r"[A-Z]", pw):
        raise ValueError("La contraseña debe contener al menos una letra mayúscula.")
    if not re.search(r"[0-9]", pw):
        raise ValueError("La contraseña debe contener al menos un número.")
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", pw):
        raise ValueError("La contraseña debe contener al menos un carácter especial.")

# --- ENDPOINT ACTUALIZADO ---
@app.post("/api/user/change-password")
def cambiar_contrasena(req: ChangePasswordRequest,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.get_current_user)):
    
    # Seguridad: el usuario solo puede cambiar su propia contraseña.
    if current_user["email"].lower() != req.email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No podés modificar la contraseña de otro usuario."
        )

    # 1. Buscar al usuario
    user = db.query(database.UserModel).filter(database.UserModel.email == req.email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # 2. Verificar que la clave actual sea correcta
    if not auth.verify_password(req.current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta")

    # 3. VERIFICAR COMPLEJIDAD DE LA NUEVA CLAVE
    try:
        validar_password(req.new_password)
    except ValueError as e:
        # Si la contraseña no cumple las reglas, devolvemos el error al frontend
        raise HTTPException(status_code=400, detail=str(e))

    # 4. Hashear la nueva clave y guardar
    user.hashed_password = auth.get_password_hash(req.new_password)
    db.commit()
    
    return {"status": "ok", "message": "Contraseña actualizada correctamente"}

# --- DTO para Solicitud de Acceso ---
class UserAccessRequest(BaseModel):
    email: str
    nombre: str
    apellido: str
    motivo: str

@app.post("/api/user/request-access")
def solicitar_acceso(req: UserAccessRequest):
    # 1. Validación de dominio corporativo (Seguridad de Backend)
    email_clean = req.email.lower().strip()
    if not email_clean.endswith("@tecnoimagen.com.ar"):
        raise HTTPException(
            status_code=400, 
            detail="Acceso denegado: Solo se permiten correos corporativos @tecnoimagen.com.ar"
        )
    
    # 2. Disparar tarea en Asana
    success = asana_conector.notificar_solicitud_acceso(
        email_clean, req.nombre, req.apellido, req.motivo
    )
    
    if success:
        return {"status": "ok", "message": "Solicitud enviada con éxito"}
    else:
        raise HTTPException(status_code=500, detail="Error al conectar con Asana")

@app.get("/api/hospital/{hospital_id}/software")
def obtener_estado_software(hospital_id: str, minutos: int = 0, db: Session = Depends(get_db), current_user: dict = Depends(auth.get_current_user)):
    
    # 1. Si minutos es 0, queremos el HISTÓRICO TOTAL
    if minutos == 0:
        query = text("""
            WITH RankedData AS (
                SELECT app_name, component_id, status_value, metric_value, extra_data,
                       ROW_NUMBER() OVER(PARTITION BY app_name, component_id ORDER BY timestamp DESC) as rn
                FROM software_monitoring
                WHERE hospital_id = :hid AND app_name IN ('mirth', 'ssl_certificate', 'elasticsearch')
            )
            SELECT app_name, component_id, status_value, metric_value, extra_data, NULL as timestamp 
            FROM RankedData WHERE rn = 1
        """)
        resultados = db.execute(query, {"hid": hospital_id}).fetchall()
        is_historical = False
    else:
        # Lógica de intervalos de tiempo (Deltas)
        time_limit = datetime.now() - timedelta(minutes=minutos)
        query = text("""
            SELECT app_name, component_id, status_value, metric_value, extra_data, timestamp
            FROM software_monitoring
            WHERE hospital_id = :hid AND app_name IN ('mirth', 'ssl_certificate', 'elasticsearch')
              AND timestamp >= :time_limit
            ORDER BY timestamp ASC
        """)
        resultados = db.execute(query, {"hid": hospital_id, "time_limit": time_limit}).fetchall()
        
        # Fallback si es un hospital nuevo sin historial reciente
        if not resultados:
            query_last = text("""
                WITH RankedData AS (
                    SELECT app_name, component_id, status_value, metric_value, extra_data,
                           ROW_NUMBER() OVER(PARTITION BY app_name, component_id ORDER BY timestamp DESC) as rn
                    FROM software_monitoring
                    WHERE hospital_id = :hid AND app_name IN ('mirth', 'ssl_certificate', 'elasticsearch')
                )
                SELECT app_name, component_id, status_value, metric_value, extra_data, NULL as timestamp 
                FROM RankedData WHERE rn = 1
            """)
            resultados = db.execute(query_last, {"hid": hospital_id}).fetchall()
            is_historical = False
        else:
            is_historical = True

    # 2. Agrupamos por aplicación y luego por canal/id
    canales_mirth = {}
    certificados_ssl = {}
    elastic_logs = {}
    
    for row in resultados:
        if row.app_name == 'mirth':
            if row.component_id not in canales_mirth:
                canales_mirth[row.component_id] = []
            canales_mirth[row.component_id].append(row)
        elif row.app_name == 'ssl_certificate':
            if row.component_id not in certificados_ssl:
                certificados_ssl[row.component_id] = []
            certificados_ssl[row.component_id].append(row)
        elif row.app_name == 'elasticsearch':
            if row.component_id not in elastic_logs:
                elastic_logs[row.component_id] = []
            elastic_logs[row.component_id].append(row)

    software_data = {
        "metadata": {"minutos": minutos, "is_historical": is_historical},
        "mirth": {},
        "ssl_certificates": [],
        "elasticsearch": []
    }
    
    # 3. Procesamos los datos de MIRTH (Lógica con histórico para el gráfico)
    for cid, history in canales_mirth.items():
        if not history: continue
        actual = history[-1]
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}
        instancia = extra_actual.get("instancia", "Default")
        
        if instancia not in software_data["mirth"]:
            software_data["mirth"][instancia] = []
            
        canal_nombre = cid.replace(f"[{instancia}] ", "") if cid.startswith(f"[{instancia}] ") else cid
        
        historial_canal = []
        
        if minutos == 0:
            total_recibidos = extra_actual.get("recibidos", 0)
            total_enviados = extra_actual.get("enviados", 0)
        else:
            total_recibidos, total_enviados = 0, 0
            if is_historical and len(history) > 1:
                prev_r, prev_s = None, None
                for row in history:
                    extra = json.loads(row.extra_data) if row.extra_data else {}
                    r = extra.get("recibidos", 0)
                    s = extra.get("enviados", 0)
                    
                    # Calcular el Delta (Tráfico en ese momento específico)
                    delta_r = (r - prev_r) if prev_r is not None and r >= prev_r else 0
                    delta_s = (s - prev_s) if prev_s is not None and s >= prev_s else 0
                    
                    if prev_r is not None:
                        total_recibidos += delta_r
                        total_enviados += delta_s
                        
                        # --- FIX: Validación de tipo (String vs Datetime) ---
                        if row.timestamp:
                            if isinstance(row.timestamp, str):
                                ts_str = row.timestamp[:19] 
                            else:
                                ts_str = row.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                        else:
                            ts_str = ""
                            
                        historial_canal.append({
                            "ts": ts_str,
                            "q": row.metric_value, # Encolados
                            "traffic": delta_r + delta_s # Tráfico (Recibidos + Enviados)
                        })
                        
                    prev_r, prev_s = r, s

        software_data["mirth"][instancia].append({
            "channel": canal_nombre,
            "status": actual.status_value,
            "queued": actual.metric_value,
            "received": total_recibidos,
            "sent": total_enviados,
            "last_error": extra_actual.get("last_error", ""),
            "history": historial_canal # Agregamos el array histórico para el gráfico
        })
        
    # 4. Procesamos los datos de CERTIFICADOS SSL (NUEVO)
    for url, history in certificados_ssl.items():
        if not history: continue
        actual = history[-1] # Tomamos siempre la última medición de días
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}
        
        software_data["ssl_certificates"].append({
            "url": url,
            "status": actual.status_value,
            "days_remaining": actual.metric_value, # Guardamos los días en metric_value
            "expiration_date": extra_actual.get("expiration_date", ""),
            "issuer": extra_actual.get("issuer", "")
        })
        
    # --- 5. NUEVO: PROCESAMOS LOS DATOS DE ELASTICSEARCH ---
    for rule_id, history in elastic_logs.items():
        if not history: continue
        actual = history[-1]
        extra_actual = json.loads(actual.extra_data) if actual.extra_data else {}
        
        # --- FIX: Validación segura del tipo de dato del timestamp ---
        last_seen_str = ""
        if actual.timestamp:
            if isinstance(actual.timestamp, str):
                last_seen_str = actual.timestamp[:19] # Cortamos milisegundos si es string
            else:
                last_seen_str = actual.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        
        software_data["elasticsearch"].append({
            "rule_id": rule_id,
            "severity": actual.status_value,
            "count": actual.metric_value,
            "services": extra_actual.get("services", []),
            "evidence": extra_actual.get("evidence", ""),
            "last_seen": last_seen_str # <- Usamos la variable segura
        })
        
    return software_data

@app.post("/v1/generar-reporte-ris")
async def api_generar_reporte_ris(
    datos: DatosRISAnalytics, 
    background_tasks: BackgroundTasks,
    # Puedes descomentar la siguiente linea si quieres que solo usuarios logueados lo usen:
    # user: dict = Depends(auth.get_current_user) 
):
    """
    Endpoint para recibir la estadística de solucion2.html (RIS Analytics)
    y devolver un PDF con el formato core de TecnoMonitor.
    """
    try:
        # 1. Crear nombre de archivo seguro
        nombre_limpio = re.sub(r'[^\w\s-]', '', datos.hospital_name).strip().replace(' ', '_')
        timestamp = int(time.time())
        filename = f"Reporte_RIS_{nombre_limpio}_{timestamp}.pdf"
        
        # 2. Crear archivo temporal
        temp_dir = tempfile.gettempdir()
        ruta_pdf = os.path.join(temp_dir, filename)

        # 3. Llamar a la nueva función del motor (debes agregarla a generator_report.py)
        # Usamos model_dump() para Pydantic v2
        generator_report.generar_reporte_ris_corporativo(datos.model_dump(), ruta_pdf)

        # 4. Programar borrado del temporal tras el envío
        background_tasks.add_task(os.remove, ruta_pdf)

        # 5. Retornar archivo
        return FileResponse(
            path=ruta_pdf,
            filename=filename,
            media_type='application/pdf'
        )

    except Exception as e:
        print(f"Error generando reporte RIS: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/users/responsables")
def listar_usuarios_responsables(db: Session = Depends(get_db),
                                 current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    """Devuelve la lista de usuarios activos para el selector de responsables de alertas."""
    usuarios = db.query(database.UserModel).filter(database.UserModel.is_active == True).all()
    
    resultados = []
    for u in usuarios:
        resultados.append({
            "email": u.email,
            "nombre": u.full_name or u.email,
            "tiene_asana": bool(u.asana_id) # Para mostrar un aviso si elegimos a alguien sin Asana ID
        })
    return resultados


