# database.py
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON, Boolean, Text
from sqlalchemy import event  # <-- 1. NUEVO: Importamos event para configurar SQLite
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "monitor_hospitales.db")

SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# --- 2. MODIFICADO: Agregamos el timeout de 15 segundos ---
engine = create_engine(
    SQLALCHEMY_DATABASE_URL, 
    connect_args={
        "check_same_thread": False,
        "timeout": 15  # Le da a SQLite un margen de 15s para esperar si está ocupada
    }
)

# --- 3. NUEVO: Activamos el modo WAL (Write-Ahead Logging) ---
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()
# -----------------------------------------------------------

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

class ReporteModel(Base):
    __tablename__ = "reportes_historicos"
    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True)
    timestamp = Column(DateTime, index=True)
    host_status = Column(String)
    host_cpu_usage = Column(Float)
    host_ram_usage = Column(Float)
    power_watts = Column(Integer)
    ambient_temp = Column(Integer, nullable=True)
    full_json_data = Column(JSON) 

class AlertaModel(Base):
    __tablename__ = "alertas"
    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True)
    tipo = Column(String) 
    mensaje = Column(String)
    start_time = Column(DateTime, default=datetime.now)
    end_time = Column(DateTime, nullable=True)
    is_active = Column(Integer, default=1)
    asana_task_gid = Column(String, nullable=True)

class ConfigModel(Base):
    __tablename__ = "configuracion"
    clave = Column(String, primary_key=True, index=True)
    valor = Column(String)

class HospitalMetadata(Base):
    __tablename__ = "hospitales_metadata"
    hospital_id = Column(String, primary_key=True, index=True)
    nombre = Column(String, nullable=False)
    provincia = Column(String, nullable=True)
    latitud = Column(String, nullable=True)
    longitud = Column(String, nullable=True)
    asana_project_id = Column(String, nullable=True)
    is_visible = Column(Boolean, default=True)
    alerts_enabled = Column(Boolean, default=True)

class ReporteUso(Base):
    __tablename__ = "reportes_uso"

    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String(50), index=True)  # Indexado para buscar rápido por hospital
    timestamp = Column(DateTime, index=True)      # Indexado para filtrar rápido por fechas
    kpi_json_data = Column(Text)                  # Aquí guardaremos el JSON de application_metrics

class SoftwareMonitoring(Base):
    __tablename__ = "software_monitoring"
    
    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True)
    app_name = Column(String, index=True)      # 'mirth' o 'suitestensa'
    component_id = Column(String, index=True)  # El canal (HL7_ADMISSION) o el ID del evento (INT-FALSE-01)
    status_value = Column(String, nullable=True) # Ej: 'Started', 'Error'
    metric_value = Column(Integer, default=0)    # Ej: 150 (queued) o 19 (ocurrencias 'c')
    extra_data = Column(JSON, nullable=True)     # Guardamos subsistemas o errores extra aquí por si acaso
    timestamp = Column(DateTime, index=True)     # Fecha clínica real (ts o scan_ts)
    created_at = Column(DateTime, default=datetime.now) # Cuándo llegó al servidor

class LogDictionary(Base):
    __tablename__ = "log_dictionary"

    id = Column(Integer, primary_key=True, index=True)
    app_name = Column(String, index=True)      # Ej: 'suitestensa' o 'mirth'
    event_id = Column(String, index=True)      # Ej: 'CRIT-MQ-01'
    title = Column(String)                     # Ej: 'RabbitMQ Saturado'
    description = Column(Text)                 # Ej: 'Agotamiento crítico de canales...'
    action = Column(Text)                      # Ej: 'URGENTE: Reiniciar el servicio...'
    severity = Column(String)                  # Ej: 'CRITICAL', 'WARNING', 'ERROR'

class HistorialReportes(Base):
    __tablename__ = "historial_reportes"
    
    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String(50), index=True)
    tipo_reporte = Column(String(50)) 
    fecha_desde = Column(String(20))
    fecha_hasta = Column(String(20))
    fecha_generacion = Column(DateTime, default=datetime.now)
    estado = Column(String(50)) # "Completado", "Descargado", "Error"
    asana_url = Column(String(255), nullable=True)

Base.metadata.create_all(bind=engine)