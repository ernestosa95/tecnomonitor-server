from sqlalchemy import Column, Integer, String, Float, DateTime, Boolean, JSON
from sqlalchemy.sql import func
from database import Base

# --- TABLA PRINCIPAL DE REPORTES ---
class ReporteModel(Base):
    __tablename__ = "reportes_historicos"
    
    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True)
    timestamp = Column(DateTime, index=True)
    
    # Resumen rápido para SQL
    host_status = Column(String, default="Unknown")
    host_cpu_usage = Column(Float, default=0.0)
    host_ram_usage = Column(Float, default=0.0)
    power_watts = Column(Float, default=0.0)
    
    # Payload completo V3 (JSON)
    full_json_data = Column(JSON) 

# --- METADATA HOSPITALES ---
class HospitalMetadata(Base):
    __tablename__ = "hospital_metadata"
    
    hospital_id = Column(String, primary_key=True, index=True)
    nombre = Column(String)
    alerts_enabled = Column(Boolean, default=True)
    asana_project_id = Column(String, nullable=True)

# --- CONFIGURACIÓN ---
class ConfigModel(Base):
    __tablename__ = "configuracion"
    
    clave = Column(String, primary_key=True)
    valor = Column(String)
    descripcion = Column(String, nullable=True)

# --- ALERTAS ACTIVAS ---
class AlertaModel(Base):
    __tablename__ = "alertas_activas"
    
    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True)
    tipo = Column(String) 
    mensaje = Column(String)
    
    start_time = Column(DateTime, default=func.now())
    end_time = Column(DateTime, nullable=True)
    
    is_active = Column(Integer, default=1) # 1=Activa, 0=Resuelta
    asana_task_gid = Column(String, nullable=True)