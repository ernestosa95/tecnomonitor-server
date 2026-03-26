import sys
import os
import json
from database import HistorialReportes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException
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

# Ajuste de Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from database import HospitalMetadata 
import alerts_engine 

load_dotenv()
#CODIGO_ACCESO = os.getenv("ACCESS_CODE", "Tecno2026")
CODIGO_ACCESO = os.environ["ACCESS_CODE"]

base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "static")
templates_dir = os.path.join(base_dir, "templates")

app = FastAPI(title="TecnoXaas Dashboard")
app.mount("/static", StaticFiles(directory=static_dir), name="static")
templates = Jinja2Templates(directory=templates_dir)

# --- DTOs ACTUALIZADOS (Punto 1 y 2) ---
class ConfigRequest(BaseModel):
    # Generales
    offline_minutes: int
    disk_threshold: int
    
    # Host Físico
    temp_amb_max: int
    temp_cpu_max: int
    cpu_host_max: int      # Nuevo
    ram_host_max: int      # Nuevo
    
    # VMs
    cpu_vm_max: int        # Nuevo
    ram_vm_max: int        # Nuevo
    
    # Hardware Switches
    enable_fans: bool      # Nuevo
    enable_power: bool     # Nuevo
    enable_raid: bool      # Nuevo

class LoginRequest(BaseModel):
    code: str

class HospitalDTO(BaseModel):
    hospital_id: str
    nombre: str
    provincia: str = None
    latitud: str = None
    longitud: str = None
    asana_project_id: str = None
    is_visible: bool = True
    alerts_enabled: bool = True # Nuevo

class ReportePDFRequest(BaseModel):
    hospital_id: str
    fecha_desde: str
    fecha_hasta: str
    alcance: str
    asana_task_id: str

def get_db():
    db = database.SessionLocal()
    try: yield db
    finally: db.close()

# --- VISTAS ---
@app.get("/")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.get("/monitor")
def dashboard_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# --- API ---
@app.post("/api/login")
def verificar_login(login_data: LoginRequest):
    return {"success": login_data.code == CODIGO_ACCESO}

@app.get("/api/resumen-hospitales")
def obtener_resumen(db: Session = Depends(get_db)):
    # Procesar lógica de offline background
    alerts_engine.procesar_offline(db) 
    
    conf = db.query(database.ConfigModel).filter_by(clave="offline_minutes").first()
    try: limit_min = int(conf.valor) if (conf and conf.valor) else 10
    except: limit_min = 10
    limit_delta = timedelta(minutes=limit_min)

    hospitales_meta = db.query(HospitalMetadata).all()
    whitelist = {h.hospital_id: h for h in hospitales_meta}

    query = text("""
        SELECT h.hospital_id, h.timestamp, h.host_status, h.host_cpu_usage, h.full_json_data
        FROM reportes_historicos h
        INNER JOIN (
            SELECT hospital_id, MAX(timestamp) as max_time
            FROM reportes_historicos
            GROUP BY hospital_id
        ) max_h ON h.hospital_id = max_h.hospital_id AND h.timestamp = max_h.max_time
        ORDER BY h.timestamp DESC
    """)
    result = db.execute(query).fetchall()
    
    data = []
    ahora = datetime.now()

    for row in result:
        if row.hospital_id not in whitelist: continue
        if not whitelist[row.hospital_id].is_visible: continue
        
        nombre_real = whitelist[row.hospital_id].nombre
        detalle = json.loads(row.full_json_data) if row.full_json_data else {}
        
        # --- LÓGICA V3 (ADAPTADA) ---
        # 1. Detectar capas
        phy = detalle.get('physical_layer', {})
        env = detalle.get('environment', {}) # Fallback v2
        
        # En v3 'sensors' está dentro de 'physical_layer', en v2 estaba en 'environment'
        sensors = phy.get('sensors') or env.get('thermal') or {} 
        
        # VMs: En v3 es una lista, en v2 un dict
        v_layer = detalle.get('virtual_layer') or detalle.get('vms') or []
        
        # Normalizar VMs a una lista simple para iterar
        lista_vms = []
        if isinstance(v_layer, list): # Es V3
            lista_vms = v_layer
        elif isinstance(v_layer, dict): # Es V2
            for k, v in v_layer.items():
                # Creamos un objeto similar a V3 al vuelo
                v['id'] = k 
                lista_vms.append(v)

        # 2. Análisis de Estado
        # Si es V3, error suele venir en sensors.status != OK
        # Si es V2, error venía en physical_host.error
        error_msg = None
        if isinstance(sensors, dict) and sensors.get('status') == 'Error':
            error_msg = "Fallo de Sensores Físicos"

        elementos_status = []
        
        has_vms_data = len(lista_vms) > 0
        # En V3 siempre asumimos que hay datos físicos si llegó el JSON
        has_physical_data = bool(phy) 

        last_seen = row.timestamp
        if isinstance(last_seen, str):
            try: last_seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S.%f")
            except: pass
        
        is_offline_by_time = (ahora - last_seen) > limit_delta
        
        # Lógica de Semáforo
        calculated_status = "Offline"
        if not is_offline_by_time:
            if error_msg: 
                calculated_status = "Warning"
            elif has_physical_data and has_vms_data: 
                calculated_status = "Online"
            elif not has_physical_data and has_vms_data: 
                calculated_status = "Solo VMs"
            else: 
                calculated_status = "Parcial"

        # Generar chips de estado (Server + VMs)
        if has_physical_data:
            lbl_state = "danger" if is_offline_by_time else ("warning" if error_msg else "success")
            elementos_status.append({"label": "SERVER", "state": lbl_state})

        # Iterar VMs (Compatible v2 y v3)
        for vm in lista_vms:
            # En V3 es vm.id, en V2 lo inyectamos arriba
            vm_name = vm.get('id') or "Unknown"
            # En V3 es vm.state, en V2 vm.status
            st = vm.get('state') or vm.get('status')
            
            vm_state = "danger" if is_offline_by_time else ("success" if st in ['Online', 'Running'] else "danger")
            elementos_status.append({"label": vm_name, "state": vm_state})
        
        # Limitar chips visuales si hay muchos
        if len(elementos_status) > 6:
            restantes = len(elementos_status) - 5
            elementos_status = elementos_status[:5]
            elementos_status.append({"label": f"+{restantes}", "state": "success"}) # Neutral

        ts_str = str(row.timestamp)
        data.append({
            "id": row.hospital_id,
            "name": nombre_real,
            "raw_id": row.hospital_id,
            "timestamp": ts_str[:19].replace("T", " "),
            "status": calculated_status, 
            "error_detail": error_msg,
            "elements": elementos_status
        })
    return data

@app.get("/api/hospital/{hospital_id}")
def obtener_detalle_hospital(hospital_id: str, db: Session = Depends(get_db)):
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
def obtener_historial(hospital_id: str, horas: int = 24, db: Session = Depends(get_db)):
    flimit = datetime.now() - timedelta(hours=horas)
    query = text("SELECT timestamp, host_cpu_usage, full_json_data FROM reportes_historicos WHERE hospital_id = :hid AND timestamp >= :flimit ORDER BY timestamp ASC")
    result = db.execute(query, {"hid": hospital_id, "flimit": flimit}).fetchall()
    
    if not result: return []

    # Downsampling simple
    total_registros = len(result)
    step = 1
    if total_registros > 600: step = int(total_registros / 600)
    muestras = result[::step]
    
    historial = []
    for row in muestras:
        try:
            # Blindaje extra por si el JSON viene dañado en la base de datos
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
            # Si un registro falla, pasamos al siguiente silenciosamente
            print(f"Error procesando fila historial: {e}")
            continue
        
    return historial

# --- NUEVA RUTA PARA KPIS (Añadir a dashboard.py) ---
@app.get("/api/hospital/{hospital_id}/kpi-history")
def obtener_historial_kpi(hospital_id: str, horas: int = 24, db: Session = Depends(get_db)):
    
    # 1. Calculamos la fecha límite real que pidió el usuario
    fecha_limite_real = datetime.now() - timedelta(hours=horas)
    
    # 2. Margen de seguridad para la consulta SQL (3 días antes)
    # Esto previene que perdamos datos si el agente se desconectó y mandó info atrasada
    fecha_limite_sql = fecha_limite_real - timedelta(days=3)
    
    # 3. Consulta SQL inicial
    query = text("SELECT timestamp, kpi_json_data FROM reportes_uso WHERE hospital_id = :hid AND timestamp >= :flimit ORDER BY timestamp ASC")
    result = db.execute(query, {"hid": hospital_id, "flimit": fecha_limite_sql}).fetchall()
    
    if not result: return []

    historial_kpi = []
    
    # 4. Filtro fino en Python usando la fecha real de extracción clínica
    for row in result:
        try:
            metrics = json.loads(row.kpi_json_data) if row.kpi_json_data else {}
            
            # Buscar la fecha de extracción dentro del JSON
            fecha_extraccion_str = metrics.get("start_time_extraction")
            
            if fecha_extraccion_str:
                try:
                    # Convertir el string ISO ("2025-07-03T00:00:00") a objeto datetime
                    fecha_evento = datetime.fromisoformat(fecha_extraccion_str)
                except ValueError:
                    # Si el formato falla, usar timestamp como respaldo
                    fecha_evento = datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp
            else:
                # Si el campo no existe, usar timestamp como respaldo
                fecha_evento = datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp
                
            # 5. Filtrar estrictamente por la fecha límite real
            if fecha_evento >= fecha_limite_real:
                historial_kpi.append({
                    "timestamp": fecha_evento.strftime("%Y-%m-%d %H:%M:%S"), # Enviamos la fecha clínica real
                    "application_metrics": metrics
                })
        except Exception as e:
            # Ignorar errores de procesamiento en filas individuales
            continue
            
    # Reordenar cronológicamente por la fecha real antes de enviar
    historial_kpi.sort(key=lambda x: x["timestamp"])
    
    return historial_kpi

@app.get("/api/alertas")
def obtener_alertas(db: Session = Depends(get_db)):
    activas = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1).order_by(database.AlertaModel.start_time.desc()).all()
    historial = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 0).order_by(database.AlertaModel.end_time.desc()).limit(50).all()
    return {"activas": activas, "historial": historial}

# --- CONFIGURACIÓN ACTUALIZADA (Punto 1) ---
@app.get("/api/config")
def obtener_configuracion(db: Session = Depends(get_db)):
    def g(k, d, is_bool=False): 
        r = db.query(database.ConfigModel).filter_by(clave=k).first()
        if r:
            return (r.valor == '1') if is_bool else int(r.valor)
        return d

    return {
        "offline_minutes": g("offline_minutes", 10),
        "disk_threshold": g("disk_threshold", 90),
        # Nuevos parámetros
        "temp_amb_max": g("temp_amb_max", 27),
        "temp_cpu_max": g("temp_cpu_max", 75),
        "cpu_host_max": g("cpu_host_max", 85),
        "ram_host_max": g("ram_host_max", 90),
        "cpu_vm_max": g("cpu_vm_max", 90),
        "ram_vm_max": g("ram_vm_max", 90),
        "enable_fans": g("enable_fans", True, is_bool=True),
        "enable_power": g("enable_power", True, is_bool=True),
        "enable_raid": g("enable_raid", True, is_bool=True)
    }

@app.post("/api/config")
def guardar_configuracion(cfg: ConfigRequest, db: Session = Depends(get_db)):
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
    
    db.commit()
    return {"status": "ok", "msg": "Configuración actualizada"}

# --- METADATA HOSPITALES (Punto 2) ---
@app.get("/api/hospitales-metadata")
def listar_hospitales_metadata(db: Session = Depends(get_db)):
    return db.query(HospitalMetadata).all()

@app.post("/api/hospitales-metadata")
def crear_hospital_metadata(dto: HospitalDTO, db: Session = Depends(get_db)):
    existe = db.query(HospitalMetadata).filter_by(hospital_id=dto.hospital_id).first()
    if existe: raise HTTPException(status_code=400, detail="El ID existe")
    nuevo = HospitalMetadata(**dto.dict())
    db.add(nuevo); db.commit()
    return {"status": "ok", "msg": "Creado"}

@app.put("/api/hospitales-metadata/{hid}")
def editar_hospital_metadata(hid: str, dto: HospitalDTO, db: Session = Depends(get_db)):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    
    # Actualizamos campos
    h.nombre = dto.nombre
    h.provincia = dto.provincia
    h.latitud = dto.latitud
    h.longitud = dto.longitud
    h.asana_project_id = dto.asana_project_id
    h.is_visible = dto.is_visible
    h.alerts_enabled = dto.alerts_enabled # Nuevo campo
    
    db.commit()
    return {"status": "ok", "msg": "Actualizado"}

@app.patch("/api/hospitales-metadata/{hid}/toggle")
def toggle_visibilidad(hid: str, db: Session = Depends(get_db)):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.is_visible = not h.is_visible
    db.commit()
    return {"status": "ok", "new_state": h.is_visible}

# Nueva ruta para togglear alertas (Punto 2)
@app.patch("/api/hospitales-metadata/{hid}/toggle-alerts")
def toggle_alertas(hid: str, db: Session = Depends(get_db)):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.alerts_enabled = not h.alerts_enabled
    db.commit()
    return {"status": "ok", "alerts_enabled": h.alerts_enabled}

@app.delete("/api/hospitales-metadata/{hid}")
def eliminar_hospital_metadata(hid: str, db: Session = Depends(get_db)):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    db.delete(h); db.commit()
    return {"status": "ok", "msg": "Eliminado"}

@app.get("/api/mapa-data")
def obtener_datos_mapa(db: Session = Depends(get_db)):
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

    # Ajuste: Le damos 0.35 de "piso" al gráfico para que las fechas rotadas no se corten
    fig, ax = plt.subplots(figsize=(8, 3.5))
    fig.subplots_adjust(bottom=0.35) 
    
    bottom_ris = np.zeros(len(labels))
    
    # 1. Dibujar barras apiladas de RIS (lado izquierdo)
    estados_ris = ['Citados', 'Admitidos', 'Ejecutados', 'Asociados', 'Borradores', 'Definitivos', 'Suspendidos']
    for estado in estados_ris:
        valores = [datos_equipo[l].get(estado.lower(), 0) for l in labels]
        if sum(valores) > 0:
            ax.bar(x - width/2, valores, width, bottom=bottom_ris, color=ESTADOS_COLORS[estado], label=estado)
            bottom_ris += np.array(valores)

    # 2. Dibujar barra de PACS (lado derecho)
    valores_pacs = [datos_equipo[l].get('almacenados', 0) for l in labels]
    if sum(valores_pacs) > 0:
        ax.bar(x + width/2, valores_pacs, width, color=ESTADOS_COLORS['Almacenados'], label='Almacenados')

    # Estética del gráfico
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=35, ha='right', fontsize=8)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.20), ncol=4, fontsize=8, frameon=False)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf

@app.post("/api/informes/pdf")
def generar_reporte_pdf(req: ReportePDFRequest, db: Session = Depends(get_db)):
    # 1. Obtener Nombre del Hospital
    hospital = db.query(HospitalMetadata).filter_by(hospital_id=req.hospital_id).first()
    nombre_hosp = hospital.nombre if hospital else "Hospital Desconocido"

    # 2. Convertir fechas y obtener datos
    def parsear_fecha(fecha_str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try:
                return datetime.strptime(fecha_str, fmt)
            except ValueError:
                continue
        raise ValueError("Formato desconocido")

    try:
        f_desde = parsear_fecha(req.fecha_desde)
        f_hasta = parsear_fecha(req.fecha_hasta) + timedelta(days=1)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Formato de fecha inválido. Recibimos: {req.fecha_desde}")

    f_desde_sql = f_desde - timedelta(days=3)
    query = text("SELECT timestamp, kpi_json_data FROM reportes_uso WHERE hospital_id = :hid AND timestamp >= :f1")
    result = db.execute(query, {"hid": req.hospital_id, "f1": f_desde_sql}).fetchall()

    # 3. Agrupar Datos
    datos_ris = {}
    datos_pacs = {}
    datos_temporales = {}
    
    EXCLUDED_AETS = ['CLIENT', 'WADO', 'PACS']
    EXCLUDED_MODS = ['DOC']
    diccionario_aet = {}
    
    agrupar_por_mes = (f_hasta - f_desde).days > 45

    for row in result:
        metrics = json.loads(row.kpi_json_data) if row.kpi_json_data else {}
        fecha_extraccion_str = metrics.get("start_time_extraction")
        
        try:
            if fecha_extraccion_str: 
                fecha_evento = datetime.fromisoformat(fecha_extraccion_str).replace(tzinfo=None)
            else: 
                fecha_evento = (datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp).replace(tzinfo=None)
        except: 
            continue

        if f_desde <= fecha_evento < f_hasta:
            k_tiempo = fecha_evento.strftime("%Y-%m") if agrupar_por_mes else fecha_evento.strftime("%Y-%m-%d")
            
            # PASO 1: RIS
            for item in metrics.get("ris", []):
                eq = item.get("equipo")
                aet = item.get("aet")
                mod = item.get("mod", "")
                
                if aet and eq: 
                    diccionario_aet[aet] = eq
                    
                nombre_final_ris = eq or aet or "Desc"
                
                if nombre_final_ris not in EXCLUDED_AETS and aet not in EXCLUDED_AETS and mod not in EXCLUDED_MODS:
                    if nombre_final_ris not in datos_temporales: datos_temporales[nombre_final_ris] = {}
                    if k_tiempo not in datos_temporales[nombre_final_ris]: datos_temporales[nombre_final_ris][k_tiempo] = {}
                    
                    val = item.get("totales", 0)
                    if val == 0:
                        val = sum([item.get(k, 0) for k in ["citados", "admitidos", "ejecutados", "con_imagen", "borradores", "definitivos", "suspendidos"]])
                    
                    if val > 0:
                        datos_ris[nombre_final_ris] = datos_ris.get(nombre_final_ris, 0) + val
                        
                    for st in ["citados", "admitidos", "ejecutados", "con_imagen", "borradores", "definitivos", "suspendidos"]:
                        val_st = item.get(st, 0)
                        if val_st > 0:
                            key_st = 'asociados' if st == 'con_imagen' else st
                            datos_temporales[nombre_final_ris][k_tiempo][key_st] = datos_temporales[nombre_final_ris][k_tiempo].get(key_st, 0) + val_st
            
            # PASO 2: PACS
            for item in metrics.get("pacs", []):
                aet = item.get("aet") or "Desc"
                mod = item.get("mod", "")
                nombre_final_pacs = diccionario_aet.get(aet, aet)
                
                if aet not in EXCLUDED_AETS and nombre_final_pacs not in EXCLUDED_AETS and mod not in EXCLUDED_MODS:
                    if nombre_final_pacs not in datos_temporales: datos_temporales[nombre_final_pacs] = {}
                    if k_tiempo not in datos_temporales[nombre_final_pacs]: datos_temporales[nombre_final_pacs][k_tiempo] = {}
                    
                    val = item.get("almacenados", 0)
                    if val > 0:
                        datos_pacs[nombre_final_pacs] = datos_pacs.get(nombre_final_pacs, 0) + val
                        datos_temporales[nombre_final_pacs][k_tiempo]['almacenados'] = datos_temporales[nombre_final_pacs][k_tiempo].get('almacenados', 0) + val

    datos_ris = dict(sorted(datos_ris.items(), key=lambda x: x[1], reverse=True))
    datos_pacs = dict(sorted(datos_pacs.items(), key=lambda x: x[1], reverse=True))

    # ==========================================
    # NORMALIZACIÓN GLOBAL
    # ==========================================
    todos_los_tiempos = set()
    for eq in datos_temporales:
        todos_los_tiempos.update(datos_temporales[eq].keys())
    
    todos_los_tiempos = sorted(list(todos_los_tiempos))
    
    columnas_posibles = ['citados', 'admitidos', 'ejecutados', 'asociados', 'borradores', 'definitivos', 'suspendidos', 'almacenados']
    cols_activas_globales = []
    
    for col in columnas_posibles:
        if any(datos_temporales[eq].get(t, {}).get(col, 0) > 0 for eq in datos_temporales for t in datos_temporales[eq]):
            cols_activas_globales.append(col)
            
    if not cols_activas_globales:
        cols_activas_globales = ['citados', 'ejecutados', 'almacenados']
            
    for eq in datos_temporales:
        for t in todos_los_tiempos:
            if t not in datos_temporales[eq]:
                datos_temporales[eq][t] = {col: 0 for col in cols_activas_globales}
            else:
                for col in cols_activas_globales:
                    if col not in datos_temporales[eq][t]:
                        datos_temporales[eq][t][col] = 0

    # ==========================================
    # 4. CREAR DOCUMENTO PDF
    # ==========================================
    buffer_pdf = io.BytesIO()
    c = canvas.Canvas(buffer_pdf, pagesize=A4)
    ancho, alto = A4

    def dibujar_encabezado_y_pie(c_canvas, titulo_hoja, num_pagina):
        c_canvas.setFont("Helvetica-Bold", 16)
        c_canvas.setFillColorRGB(0.17, 0.24, 0.31)
        c_canvas.setStrokeColorRGB(0.6, 0.6, 0.6)
        c_canvas.setFillColorRGB(0.9, 0.9, 0.9)
        c_canvas.roundRect(40, alto - 60, 45, 25, 4, fill=1, stroke=1)
        c_canvas.setFillColorRGB(0.17, 0.24, 0.31)
        c_canvas.drawString(45, alto - 53, req.hospital_id)
        c_canvas.drawString(95, alto - 53, nombre_hosp)
        c_canvas.setFont("Helvetica-Bold", 14)
        c_canvas.drawString(40, alto - 85, titulo_hoja)
        c_canvas.setFont("Helvetica", 11)
        c_canvas.drawString(40, alto - 105, f"Período: {req.fecha_desde} - {req.fecha_hasta}")
        
        c_canvas.setFont("Helvetica-Bold", 14)
        c_canvas.setFillColorRGB(0.16, 0.5, 0.72)
        c_canvas.drawString(ancho/2 - 60, 30, "TECNOIMAGEN")
        c_canvas.setFont("Helvetica", 9)
        c_canvas.setFillColorRGB(0.5, 0.5, 0.5)
        c_canvas.drawString(ancho - 100, 30, f"Página {num_pagina}")
        return alto - 140

    def dibujar_caja_totales(titulo, datos_dict, pos_y):
        img_buf, total, colores = generar_grafico_dona(datos_dict)
        data_tabla = [["", "Equipo", "Cantidad", "%"]]
        filas = []
        for i, (eq, cant) in enumerate(datos_dict.items()):
            pct = f"{(cant / total * 100):.1f}%" if total > 0 else "0%"
            color_hex = colores[i] if i < len(colores) else '#bdc3c7'
            data_tabla.append(["", eq[:20], f"{cant:,}".replace(',', '.'), pct])
            filas.append(color_hex)
            
        alto_caja = max(200, 70 + (len(data_tabla) * 18))
        c.setStrokeColorRGB(0.8, 0.8, 0.8)
        c.setFillColorRGB(0.98, 0.98, 0.98)
        c.roundRect(40, pos_y - alto_caja, ancho - 80, alto_caja, 10, fill=1, stroke=1)
        c.setFillColorRGB(0.1, 0.1, 0.1)
        c.setFont("Helvetica-Bold", 12)
        c.drawString(55, pos_y - 25, titulo)
        c.drawImage(ImageReader(img_buf), 50, pos_y - alto_caja + (alto_caja/2) - 75, width=150, height=150, mask='auto')
        
        t = Table(data_tabla, colWidths=[20, 140, 70, 40])
        t.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#ecf0f1')),
            ('TEXTCOLOR', (0,0), (-1,0), colors.HexColor('#2c3e50')),
            ('ALIGN', (2,0), (-1,-1), 'RIGHT'),
            ('ALIGN', (0,0), (0,-1), 'CENTER'), 
            ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0,0), (-1,0), 6),
            ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#bdc3c7')),
            ('FONTSIZE', (0,0), (-1,-1), 9),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ]))
        t.wrapOn(c, 200, 200)
        pos_tabla_y = pos_y - 45 - (len(data_tabla)*18)
        t.drawOn(c, 230, pos_tabla_y)

        for i, color_hex in enumerate(filas):
            y_circulo = pos_tabla_y + ((len(data_tabla) - 2 - i) * 18) + 9 
            c.setFillColor(colors.HexColor(color_hex))
            c.setStrokeColor(colors.HexColor(color_hex))
            c.circle(240, y_circulo, 4, fill=1, stroke=0)
            
        return pos_y - alto_caja - 20

    # === DIBUJAR PÁGINA 1: TOTALES ===
    pos_y_actual = dibujar_encabezado_y_pie(c, "INFORME DE GESTIÓN DE EQUIPOS MÉDICOS", 1)
    
    if req.alcance in ['total', 'ris'] and datos_ris:
        pos_y_actual = dibujar_caja_totales("ÓRDENES RIS POR EQUIPO (Órdenes Creadas)", datos_ris, pos_y_actual)
    if req.alcance in ['total', 'pacs'] and datos_pacs:
        pos_y_actual = dibujar_caja_totales("ESTUDIOS PACS POR EQUIPO (Estudios Almacenados)", datos_pacs, pos_y_actual)

    # === DIBUJAR PÁGINAS 2+: EVOLUCIÓN TEMPORAL ===
    equipos_a_graficar = list(datos_temporales.keys())
    pagina_actual = 2
    
    if equipos_a_graficar:
        c.showPage()
        pos_y_actual = dibujar_encabezado_y_pie(c, "EVOLUCIÓN TEMPORAL POR EQUIPO", pagina_actual)
        
        for equipo in equipos_a_graficar:
            cols_activas = cols_activas_globales

            img_buf = generar_grafico_temporal(datos_temporales[equipo])
            if not img_buf: continue

            # Ajuste: Eliminamos la columna 'Total'
            headers = ['Período'] + [col.capitalize() for col in cols_activas]
            data_tabla = [headers]
            
            for tiempo in todos_los_tiempos:
                fila = [tiempo]
                for col in cols_activas:
                    val = datos_temporales[equipo][tiempo].get(col, 0)
                    fila.append(f"{val:,}".replace(',', '.'))
                data_tabla.append(fila)

            # Ajuste: Hacemos la caja más alta para separar el gráfico de la tabla
            alto_caja = 280 + (len(data_tabla) * 16)
            
            if (pos_y_actual - alto_caja) < 50:
                c.showPage()
                pagina_actual += 1
                pos_y_actual = dibujar_encabezado_y_pie(c, "EVOLUCIÓN TEMPORAL POR EQUIPO", pagina_actual)

            c.setStrokeColorRGB(0.8, 0.8, 0.8)
            c.setFillColorRGB(0.98, 0.98, 0.98)
            c.roundRect(40, pos_y_actual - alto_caja, ancho - 80, alto_caja, 10, fill=1, stroke=1)
            
            c.setFillColorRGB(0.1, 0.1, 0.1)
            c.setFont("Helvetica-Bold", 12)
            c.drawString(55, pos_y_actual - 25, f"EVOLUCIÓN COMBINADA - {equipo}")
            
            # Ajuste: El gráfico se dibuja con una altura de 200 en vez de 210, dejándole más respiro
            c.drawImage(ImageReader(img_buf), 40, pos_y_actual - 250, width=ancho-80, height=200, mask='auto')
            
            # Ajuste: Recalculamos los anchos sin la columna total
            ancho_col_base = (ancho - 120) / (len(cols_activas) + 1.5)
            anchos = [ancho_col_base * 1.5] + [ancho_col_base]*len(cols_activas)
            t = Table(data_tabla, colWidths=anchos)
            
            estilos = [
                ('ALIGN', (1,0), (-1,-1), 'CENTER'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0,0), (-1,0), 4),
                ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor('#bdc3c7')),
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ]
            
            for idx_col, col_name in enumerate(cols_activas):
                color_hex = ESTADOS_COLORS[col_name.capitalize()]
                estilos.append(('BACKGROUND', (idx_col+1, 0), (idx_col+1, 0), colors.HexColor(color_hex)))
                
            estilos.append(('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#ecf0f1')))

            t.setStyle(TableStyle(estilos))
            t.wrapOn(c, ancho-80, 200)
            
            # Ajuste: Dibujamos la tabla más abajo para que no toque las etiquetas del eje X
            t.drawOn(c, 50, pos_y_actual - alto_caja + 20)

            pos_y_actual -= (alto_caja + 20)

    c.save()
    
    # Preparamos el archivo y el nombre
    filename = f"Reporte_TM_{req.hospital_id}_{req.fecha_desde}.pdf"
    pdf_bytes = buffer_pdf.getvalue() # Obtenemos los bytes crudos
    
    # Intentamos subir a Asana
    asana_url = asana_conector.adjuntar_pdf_a_tarea(req.asana_task_id, pdf_bytes, filename)
    
    # NUEVO: Guardar en la Base de Datos
    nuevo_registro = HistorialReportes(
        hospital_id=req.hospital_id,
        tipo_reporte="PDF Completo",
        fecha_desde=req.fecha_desde,
        fecha_hasta=req.fecha_hasta,
        estado="Completado" if asana_url else "Descargado",
        asana_url=asana_url
    )
    db.add(nuevo_registro)
    db.commit()
    
    if asana_url:
        return {"status": "success", "asana_url": asana_url, "message": "Adjuntado a Asana correctamente"}
    else:
        buffer_pdf.seek(0)
        return StreamingResponse(
            buffer_pdf, 
            media_type="application/pdf", 
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

@app.get("/api/informes/historial")
def obtener_historial_reportes(db: Session = Depends(get_db)):
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



