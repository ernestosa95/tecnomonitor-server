from pydantic import BaseModel, Field
from typing import List, Optional, Any, Dict
from datetime import datetime

# --- BLOQUES COMUNES ---

class SensorReading(BaseModel):
    name: str
    value: float
    unit: str
    status: str = "OK"

class CpuTelemetry(BaseModel):
    usage_percent: float = 0.0

class RamTelemetry(BaseModel):
    total_gb: float = 0.0
    used_gb: float = 0.0
    usage_percent: float = 0.0

class Telemetry(BaseModel):
    cpu: Optional[CpuTelemetry] = None
    ram: Optional[RamTelemetry] = None
    uptime_seconds: Optional[int] = None 

# --- STORAGE & APP ---

class DiskPerformance(BaseModel):
    latency_ms: float = 0.0
    status: str = "OK"

class StorageVolume(BaseModel):
    mount_point: str
    total_gb: float = 0.0
    free_gb: float = 0.0
    usage_percent: float = 0.0
    performance: Optional[DiskPerformance] = None

class VitalSigns(BaseModel):
    pid: int
    health: str = "OK"
    cpu_percent: float = 0.0
    ram_mb: float = 0.0
    threads: Optional[int] = 0
    handles: Optional[int] = 0

class Service(BaseModel):
    name: str
    display_name: Optional[str] = None
    state: str
    vital_signs: Optional[VitalSigns] = None

class ApplicationLayer(BaseModel):
    services: List[Service] = []

# --- CAPA VIRTUAL ---

class VirtualResource(BaseModel):
    id: str
    type: str = "vm"
    state: str
    telemetry: Optional[Telemetry] = None
    storage: List[StorageVolume] = []
    application_layer: Optional[ApplicationLayer] = None

# --- CAPA FÍSICA (AQUÍ ESTÁ LA CORRECCIÓN) ---

class HostInfo(BaseModel):
    hostname: str = "Unknown"
    type: str = "Unknown"
    model: str = "Unknown"
    uptime_seconds: int = 0

class PowerSupply(BaseModel):
    name: str
    watts: float = 0.0
    status: str = "Unknown"

class PowerInfo(BaseModel):
    watts_current: float = 0.0
    supplies: List[PowerSupply] = []

class SensorLayer(BaseModel):
    status: str = "Unknown"
    temperatures: List[SensorReading] = []
    fans: List[SensorReading] = []
    power: Optional[PowerInfo] = None

class NetworkHealth(BaseModel):
    status: str = "Unknown"
    upload_usage_mbps: float = 0.0
    download_usage_mbps: float = 0.0
    cloud_latency_ms: float = 0.0
    cloud_status: str = "Unknown"
    last_check: Optional[datetime] = None

class PhysicalLayer(BaseModel):
    # Todos estos campos ahora son OPCIONALES con valor por defecto None
    host_info: Optional[HostInfo] = None
    telemetry: Optional[Telemetry] = None
    sensors: Optional[SensorLayer] = None
    
    # Campo extra para RAID u otros datos futuros
    storage_layer: Optional[Dict[str, Any]] = None 
    network_health: Optional[NetworkHealth] = None

    class Config:
        extra = "allow" 

# --- ENVELOPE ---

class Envelope(BaseModel):
    schema_version: str
    agent_version: str
    hospital_id: str
    timestamp: datetime

# --- ROOT ---

class AgentReportV3(BaseModel):
    envelope: Envelope
    # PhysicalLayer también opcional, por si ni siquiera viene la llave
    physical_layer: Optional[PhysicalLayer] = Field(default_factory=PhysicalLayer)
    virtual_layer: List[VirtualResource] = []
    
    class Config:
        extra = "allow"

# --- NUEVOS MODELOS PARA V4 (Software Metrics) ---

class RISMetric(BaseModel):
    equipo: str
    aet: str
    mod: str
    totales: int
    citados: int
    admitidos: int
    ejecutados: int
    con_imagen: int
    borradores: int
    definitivos: int
    suspendidos: int

class PACSMetric(BaseModel):
    aet: str
    mod: str
    almacenados: int

class UserMetric(BaseModel):
    rol: str
    usuarios_unicos: int
    inicios_sesion: int

class ApplicationMetricsContent(BaseModel):
    extraction_interval_hours: Optional[float] = None
    start_time_extraction: Optional[datetime] = None  # <-- NUEVO
    end_time_extraction: Optional[datetime] = None    # <-- NUEVO
    ris: List[RISMetric] = []
    pacs: List[PACSMetric] = []
    users: List[UserMetric] = []

# --- REPORTE MAESTRO V4 ---

class AgentReportV4(BaseModel):
    envelope: Dict[str, Any]
    collection_meta: Optional[Dict[str, Any]] = None
    software_monitoring: Optional[Dict[str, Any]] = None
    physical_layer: Dict[str, Any]
    virtual_layer: List[Dict[str, Any]]
    # Hacemos que este campo sea opcional para mantener compatibilidad
    application_metrics: Optional[ApplicationMetricsContent] = None

class DatosRISAnalytics(BaseModel):
    hospital_name: str
    hospital_id: str = "S/D"
    fecha_desde: str = ""
    fecha_hasta: str = ""
    kpi_total_registros: int = 0
    kpi_total_estudios: int = 0
    kpi_origenes_distintos: int = 0
    kpi_pacientes: int
    kpi_edad_promedio: float
    kpi_equipos_activos: int
    datos_equipos: Dict[str, int] = {}
    datos_origen: Dict[str, int] = {}
    datos_tipo: Dict[str, int] = {}
    datos_edad: Dict[str, int] = {}
    datos_sexo: Dict[str, int] = {}
    datos_piramide: Dict[str, Any] = {}
    datos_sexo_tipo: Dict[str, Any] = {}
    datos_mapa_calor: Dict[str, Any] = {}

