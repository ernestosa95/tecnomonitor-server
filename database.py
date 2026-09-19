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
    reaperturas = Column(Integer, default=0)

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
    # Hospital sin agente de monitoreo: sus KPIs se cargan a mano
    # (ver HospitalManualKPI) en vez de calcularse en vivo desde reportes.
    datos_manuales = Column(Boolean, default=False)
    # Hash SHA-256 del token de ingesta (nunca el token en texto plano).
    # NULL = hospital todavía no migrado a schema_version 4.5. Ver
    # docs/11-plan-auth-ingesta-agente.md.
    ingest_token_hash = Column(String, nullable=True, unique=True, index=True)

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

    # Índice compuesto: cubre el patrón real de acceso (motor de alertas cada
    # 60s y /api/hospital/{id}/software), que filtra por hospital+app y ordena
    # por timestamp. Los índices individuales de arriba no alcanzan para eso.
    __table_args__ = (
        Index('idx_swmon_hosp_app_ts', 'hospital_id', 'app_name', 'timestamp'),
    )

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

class HospitalManualKPI(Base):
    """
    Snapshot único y editable de KPIs para hospitales sin agente de
    monitoreo (ej. algunos de Córdoba, relevados a mano). Se pisa cada
    vez que alguien vuelve a cargar los valores; no es serie temporal.
    """
    __tablename__ = "hospital_manual_kpi"

    hospital_id = Column(String, ForeignKey("hospitales_metadata.hospital_id", ondelete="CASCADE"),
                         primary_key=True)

    estudios = Column(Integer, default=0)
    admitidas = Column(Integer, default=0)
    asociadas = Column(Integer, default=0)
    definitivas = Column(Integer, default=0)
    ia = Column(Integer, default=0)
    equipos = Column(Integer, default=0)

    tb_alm = Column(Float, nullable=True)
    tb_disp = Column(Float, nullable=True)
    ram = Column(Float, nullable=True)
    go_live = Column(String, nullable=True)

    actualizado_en = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    actualizado_por = Column(String, nullable=True)  # email del usuario

class LoginAttempt(Base):
    __tablename__ = "login_attempts"
    ip = Column(String, primary_key=True)
    intentos = Column(Integer, default=0)
    bloqueado_hasta = Column(Float, default=0)

class LoginAttemptEmail(Base):
    """
    Igual que LoginAttempt pero por cuenta (email/username) en vez de por IP.
    Sin esto, un atacante con muchas IPs (proxies, botnet) puede probar
    fuerza bruta contra una cuenta puntual sin activar nunca el bloqueo por
    IP. Ver docs/04-seguridad.md#s4.
    """
    __tablename__ = "login_attempts_email"
    email = Column(String, primary_key=True)
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

class AlertExclusionModel(Base):
    """
    Reglas de supresión de alertas.

    Un hallazgo del motor puede ser técnicamente correcto (cumple el umbral)
    y aun así no merecer un ticket: discos de imágenes que viven al 92% por
    diseño, VMs de laboratorio, sensores rotos que reportan basura.
    Esta tabla es la lista de "sí, ya lo sé, no me avises".
    """
    __tablename__ = "alert_exclusions"

    id = Column(Integer, primary_key=True, index=True)

    # '*' = aplica a TODOS los hospitales. Si no, el hospital_id puntual.
    hospital_id = Column(String, index=True, nullable=False, default="*")

    # Patrón contra el campo `tipo` de AlertaModel (ej: "DISK_GMS-APP-PACS_E:")
    patron = Column(String, nullable=False)

    # exact | prefix | contains | regex
    modo_match = Column(String, nullable=False, default="prefix")

    # total = no se registra nada | silencioso = se guarda en DB, no va a Asana
    accion = Column(String, nullable=False, default="total")

    # Excluye hallazgos de severidad <= a este nivel.
    # CRITICAL (default) excluye todo. WARNING deja pasar los CRITICAL.
    nivel_max = Column(String, nullable=False, default="CRITICAL")

    motivo = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True, nullable=False)

    # Ventana de mantenimiento: NULL = permanente
    expires_at = Column(DateTime, nullable=True)

    created_by = Column(String, nullable=True)   # email del usuario
    created_at = Column(DateTime, default=datetime.now)

    # Telemetría de la propia regla: cuántas veces frenó algo y cuándo
    hits = Column(Integer, default=0, nullable=False)
    last_hit = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_excl_hospital_enabled", "hospital_id", "enabled"),
    )


# --- Mapa de integraciones Mirth (ver docs/13-contrato-topologia-mirth.md) ---

class MirthChannelTopology(Base):
    """
    Snapshot técnico de la definición de un canal de Mirth (conector de
    origen, conectores de destino, y si alguno es un "Channel Writer", a
    qué otro canal apunta) tal como lo manda el agente en
    `software_monitoring.mirth_topology`. NO es serie histórica -- upsert
    por (hospital_id, instancia, channel_id): la topología cambia rarísima
    vez, tenerla en una fila por ciclo (como `SoftwareMonitoring`)
    desperdiciaría espacio sin aportar nada. `channel_id` es el join
    estable con `SoftwareMonitoring.extra_data.channel_id` (component_id
    es frágil ante renames del canal).
    """
    __tablename__ = "mirth_channel_topology"

    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True)
    instancia = Column(String, default="Default")
    channel_id = Column(String, index=True)     # GUID de Mirth
    component_id = Column(String, index=True)   # "[alias] nombre" -- join legacy con software_monitoring
    nombre = Column(String)
    revision = Column(Integer, nullable=True)
    source_transport = Column(String, nullable=True)
    source_endpoint = Column(String, nullable=True)
    destinos = Column(JSON, default=list)        # lista de conectores destino tal cual la manda el agente
    topo_hash = Column(String)                   # sha1 del bloque distilado, para saber si cambió
    primera_vez = Column(DateTime, default=datetime.now)
    last_seen = Column(DateTime, index=True)      # último ciclo en que Mirth reportó este canal
    updated_at = Column(DateTime, default=datetime.now)

    __table_args__ = (
        UniqueConstraint("hospital_id", "instancia", "channel_id", name="uq_mirth_topo_canal"),
        Index("idx_mirth_topo_hosp", "hospital_id"),
    )


class MirthNodo(Base):
    """
    Nodo curado a mano desde el panel de admin: un sistema origen o destino
    del mapa de integraciones (ej. "HIS Hospital", "RIS SUITESTENSA"). Mirth
    no tiene ningún concepto de esto -- solo sabe host:puerto de un
    conector -- así que es configuración de negocio, no dato de monitoreo.
    Un mismo sistema puede ser origen en un canal y destino en otro (ej. el
    HIS: recibe en un puerto, responde en otro), por eso `tipo` discrimina
    en vez de usar dos tablas paralelas.
    """
    __tablename__ = "mirth_nodos"

    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, ForeignKey("hospitales_metadata.hospital_id", ondelete="CASCADE"),
                          nullable=False, index=True)
    tipo = Column(String, nullable=False)   # 'origen' | 'destino'
    clave = Column(String, nullable=False)  # slug estable, es el 'id' del JSON del mapa (ej. 'his', 'ris')
    label = Column(String, nullable=False)  # nombre técnico (modo "Operación")
    sub = Column(String, nullable=True)     # endpoint técnico mostrado (ej. "10.0.1.20:6661")
    humano = Column(String, nullable=True)  # nombre no técnico (modo "Estado")
    vm = Column(String, nullable=True)      # id dentro de virtual_layer, para mostrar CPU/RAM en el drawer
    orden = Column(Integer, default=0)
    activo = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        UniqueConstraint("hospital_id", "tipo", "clave", name="uq_mirth_nodo"),
    )


class DicomReglaBaseline(Base):
    """
    Piso habitual de cada regla de autoenrute DICOM (percentil 10 de sus
    últimos 7 días), calculado por alerts_engine/software/dicom_baseline.py.
    Una regla con un residuo constante (ej. 1164 pendientes inmóviles durante
    días) no debe alertar mientras esté en su piso. Ver docs/12 §3quater.
    """
    __tablename__ = "dicom_regla_baseline"

    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, index=True, nullable=False)
    component_id = Column(String, nullable=False)   # id de la regla, tal cual software_monitoring
    piso = Column(Integer, default=0)
    tolerancia = Column(Integer, default=300)
    # Solo las reglas con actividad demostrada (>=5 bajadas de >=300 en 7 días)
    # tienen piso vigente: una regla que nunca drenó no se autoperdona.
    activa = Column(Boolean, default=False)
    muestras = Column(Integer, default=0)
    calculado_en = Column(DateTime, default=datetime.now)
    piso_subido_en = Column(DateTime, nullable=True)  # límite de velocidad de subida del piso

    __table_args__ = (
        UniqueConstraint('hospital_id', 'component_id', name='uq_dicom_baseline_regla'),
    )


class MirthCanalMeta(Base):
    """
    Curación por canal: criticidad (define contra qué umbral de cola
    alerta, ver dashboard_app/alerts_engine/software/mirth.py), nombre
    humano, y a qué MirthNodo está asignado como origen/destino. Identidad
    por `channel_id` (GUID de Mirth), no por nombre. SQLite no enforcea
    FKs (ver database.py:24-29, solo setea WAL) -- borrar un MirthNodo
    referenciado acá tiene que nulear `nodo_origen_id`/`nodo_destino_id` a
    mano (lo hace el router de administración, no la base).
    """
    __tablename__ = "mirth_canales_meta"

    id = Column(Integer, primary_key=True, index=True)
    hospital_id = Column(String, ForeignKey("hospitales_metadata.hospital_id", ondelete="CASCADE"),
                          nullable=False, index=True)
    instancia = Column(String, default="Default")
    channel_id = Column(String, index=True)

    nombre_tecnico = Column(String, nullable=True)  # cache del último nombre visto, solo para mostrar
    hum = Column(String, nullable=True)              # descripción humana (modo "Estado")
    crit = Column(String, default="media")            # 'alta' | 'media' | 'baja'
    nodo_origen_id = Column(Integer, ForeignKey("mirth_nodos.id"), nullable=True)
    nodo_destino_id = Column(Integer, ForeignKey("mirth_nodos.id"), nullable=True)
    oculto = Column(Boolean, default=False)  # canales de prueba: no se muestran en el mapa
    notas = Column(Text, nullable=True)

    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    updated_by = Column(String, nullable=True)  # email del usuario que curó

    __table_args__ = (
        UniqueConstraint("hospital_id", "instancia", "channel_id", name="uq_mirth_canal_meta"),
    )


# --- FINAL DEL ARCHIVO: SE CREAN TODAS LAS TABLAS REGISTRADAS EN 'Base' ---
Base.metadata.create_all(bind=engine)