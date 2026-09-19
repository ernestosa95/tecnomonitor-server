from pydantic import BaseModel
from typing import List, Optional, Any, Dict
from datetime import datetime

# --- MODELOS PARA V4 (Software Metrics) ---
#
# No hay un equivalente V3 acá a propósito: el servidor solo valida contra
# AgentReportV4 (ver main.py), donde envelope/physical_layer/virtual_layer son
# Dict[str, Any] sin schema estricto. Hasta 2026-09, este archivo tuvo además
# un árbol completo de modelos tipados (AgentReportV3 + Envelope/PhysicalLayer/
# SensorLayer/VirtualResource/etc.) que nunca se instanciaba desde ningún lado
# del path de ingesta real — se borró para no sugerir que el servidor exige
# esa forma cuando en la práctica no valida nada de eso.

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

