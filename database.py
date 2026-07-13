# database.py
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, JSON, Boolean, Text, Index, ForeignKey, UniqueConstraint
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

    # Índice compuesto
    __table_args__ = (
        Index('idx_hospital_timestamp', 'hospital_id', 'timestamp'),
    )

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
    has_ris = Column(Boolean, default=False)
    kpi_settings = Column(JSON, default=dict)

class ReporteUso(Base):
    __tablename__ = "reportes_uso"

    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String(50), index=True)  # Indexado para buscar rápido por hospital
    timestamp = Column(DateTime, index=True)      # Indexado para filtrar rápido por fechas
    kpi_json_data = Column(Text)                  # Aquí guardaremos el JSON de application_metrics

    # Índice compuesto
    __table_args__ = (
        Index('idx_uso_hospital_timestamp', 'hospital_id', 'timestamp'),
    )

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

class UserModel(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    # email: Único e indexado para búsquedas rápidas durante el login
    email = Column(String, unique=True, index=True, nullable=False)
    username = Column(String, unique=True, index=True, nullable=True)
    # hashed_password: Nunca guardaremos la clave en texto plano
    hashed_password = Column(String, nullable=False)
    full_name = Column(String)
    # role: Aquí definiremos 'Admin', 'Ingenieria', 'Comercial' o 'Visor'
    role = Column(String, default="Visor") 
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    asana_id = Column(String(255), nullable=True)
    must_change_password = Column(Boolean, default=False)

class ClienteHospitalAccess(Base):
    """
    Acceso de un usuario rol 'Cliente' a un hospital puntual, con las
    pestañas habilitadas para ESE usuario en ESE hospital.
    Un Cliente puede tener varias filas (varios hospitales).
    """
    __tablename__ = "cliente_hospital_access"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"),
                     nullable=False, index=True)
    hospital_id = Column(String, ForeignKey("hospitales_metadata.hospital_id", ondelete="CASCADE"),
                         nullable=False, index=True)

    # Pestañas habilitadas (por usuario+hospital)
    ver_infra    = Column(Boolean, default=False, nullable=False)
    ver_software = Column(Boolean, default=False, nullable=False)
    ver_kpis     = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("user_id", "hospital_id", name="uq_cliente_hospital"),
    )

class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    ip = Column(String, primary_key=True)
    intentos = Column(Integer, default=0)
    bloqueado_hasta = Column(Float, default=0)

class AccessRequestModel(Base):
    __tablename__ = "access_requests"

    id = Column(Integer, primary_key=True, index=True)
    tipo = Column(String, nullable=False)          # "interno" | "cliente"
    email = Column(String, nullable=False)
    nombre = Column(String, nullable=False)
    apellido = Column(String, nullable=True)        # solo interno
    full_name_cliente = Column(String, nullable=True)  # solo cliente ("Resp. Sistemas · Hospital X")
    motivo = Column(Text, nullable=True)
    hospitales_solicitados = Column(Text, nullable=True)  # JSON: ["H01","H02"] — solo cliente
    estado = Column(String, default="pendiente")    # "pendiente" | "aprobado" | "rechazado"
    creado_en = Column(DateTime, default=datetime.now)
    revisado_por = Column(String, nullable=True)
    revisado_en = Column(DateTime, nullable=True)

Base.metadata.create_all(bind=engine)