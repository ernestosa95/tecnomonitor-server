"""
Alertas activas/históricas, configuración global de umbrales, y el motor de
exclusiones (reglas de supresión de alertas conocidas/ruido).

Quinto router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md.
"""
import re
from datetime import datetime
from typing import Optional

import requests  # usado en _cerrar_alertas_por_regla() para el trigger del WebSocket
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import alerts_engine
import asana_conector
import auth
import database
from core import get_db

router = APIRouter()


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
    enable_network_latency: bool

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

    # --- Parametros Autoenrute DICOM ---
    # Con default: un cliente viejo (script.js sin actualizar) sigue pudiendo
    # guardar la configuración sin recibir un 422 por campos faltantes.
    dicom_alert_enabled: bool = False
    dicom_stall_warning_minutes: int = 45
    dicom_stall_critical_minutes: int = 120
    dicom_min_instances: int = 50
    dicom_drain_percent: int = 70
    dicom_responsible_email: str = ""

class ExclusionRequest(BaseModel):
    hospital_id: str = "*"
    patron: str
    modo_match: str = "prefix"      # exact | prefix | contains | regex
    accion: str = "total"           # total | silencioso
    nivel_max: str = "CRITICAL"     # NOTICE | WARNING | CRITICAL
    motivo: Optional[str] = None
    enabled: bool = True
    expires_at: Optional[str] = None    # "YYYY-MM-DD" o None
    cerrar_existentes: bool = True


@router.get("/api/alertas")
def obtener_alertas(db: Session = Depends(get_db),
                    # CORRECCIÓN: Solo roles autorizados pueden ver alertas
                    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    activas = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1).order_by(database.AlertaModel.start_time.desc()).all()
    historial = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 0).order_by(database.AlertaModel.end_time.desc()).limit(50).all()
    return {"activas": activas, "historial": historial}

# --- CONFIGURACIÓN ACTUALIZADA (Punto 1) ---
@router.get("/api/config")
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
        "enable_network_latency": g("enable_network_latency", True, is_bool=True),

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
        "mirth_responsible_email": g("mirth_responsible_email", ""),

        # --- AUTOENRUTE DICOM ---
        # ⚠️ Estos defaults DEBEN coincidir con los de alerts_engine.cargar_config().
        # Si divergen, el panel muestra un número y el motor usa otro.
        "dicom_alert_enabled": g("dicom_alert_enabled", False, is_bool=True),
        "dicom_stall_warning_minutes": g("dicom_stall_warning_minutes", 45),
        "dicom_stall_critical_minutes": g("dicom_stall_critical_minutes", 120),
        "dicom_min_instances": g("dicom_min_instances", 50),
        "dicom_drain_percent": g("dicom_drain_percent", 70),
        "dicom_responsible_email": g("dicom_responsible_email", "")
    }


@router.post("/api/config")
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
    s("enable_network_latency", cfg.enable_network_latency)
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

    # --- GUARDAR CONFIGURACIONES DE AUTOENRUTE DICOM ---
    s("dicom_alert_enabled", cfg.dicom_alert_enabled)
    s("dicom_stall_warning_minutes", cfg.dicom_stall_warning_minutes)
    s("dicom_stall_critical_minutes", cfg.dicom_stall_critical_minutes)
    s("dicom_min_instances", cfg.dicom_min_instances)
    s("dicom_drain_percent", cfg.dicom_drain_percent)
    s("dicom_responsible_email", cfg.dicom_responsible_email)

    db.commit()
    return {"status": "ok", "msg": "Configuración actualizada"}


_MODOS_MATCH = {"exact", "prefix", "contains", "regex"}
_ACCIONES = {"total", "silencioso"}
_NIVELES = {"NOTICE", "WARNING", "CRITICAL"}


def _validar_exclusion(req: ExclusionRequest):
    if not (req.patron or "").strip():
        raise HTTPException(status_code=400, detail="El patrón no puede estar vacío.")
    if req.modo_match not in _MODOS_MATCH:
        raise HTTPException(status_code=400, detail=f"modo_match inválido: {req.modo_match}")
    if req.accion not in _ACCIONES:
        raise HTTPException(status_code=400, detail=f"acción inválida: {req.accion}")
    if req.nivel_max not in _NIVELES:
        raise HTTPException(status_code=400, detail=f"nivel_max inválido: {req.nivel_max}")
    if req.modo_match == "regex":
        try:
            re.compile(req.patron)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"Regex inválida: {e}")
    # Un patrón vacío o de 1 carácter en modo prefix apagaría medio motor.
    if req.modo_match in ("prefix", "contains") and len(req.patron.strip()) < 3:
        raise HTTPException(
            status_code=400,
            detail="El patrón debe tener al menos 3 caracteres en modo prefijo/contiene."
        )


def _parse_expira(valor):
    if not valor:
        return None
    try:
        return datetime.strptime(valor, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        raise HTTPException(status_code=400, detail="Fecha de vencimiento inválida (usar YYYY-MM-DD).")


@router.get("/api/exclusiones")
def listar_exclusiones(db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    filas = db.query(database.AlertExclusionModel).order_by(
        database.AlertExclusionModel.created_at.desc()
    ).all()
    ahora = datetime.now()
    return [{
        "id": f.id,
        "hospital_id": f.hospital_id,
        "patron": f.patron,
        "modo_match": f.modo_match,
        "accion": f.accion,
        "nivel_max": f.nivel_max,
        "motivo": f.motivo,
        "enabled": bool(f.enabled),
        "expires_at": f.expires_at.strftime("%Y-%m-%d") if f.expires_at else None,
        "vencida": bool(f.expires_at and f.expires_at <= ahora),
        "created_by": f.created_by,
        "created_at": f.created_at.strftime("%Y-%m-%d %H:%M") if f.created_at else None,
        "hits": f.hits or 0,
        "last_hit": f.last_hit.strftime("%Y-%m-%d %H:%M") if f.last_hit else None,
    } for f in filas]


@router.post("/api/exclusiones/preview")
def preview_exclusion(req: ExclusionRequest, db: Session = Depends(get_db),
                      current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    """
    Antes de guardar: qué alertas ACTIVAS quedarían suprimidas por esta regla.
    Sirve para no descubrir después que apagaste medio hospital.
    """
    _validar_exclusion(req)

    q = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1)
    if req.hospital_id != "*":
        q = q.filter(database.AlertaModel.hospital_id == req.hospital_id)

    regla = {
        "patron_lower": req.patron.strip().lower(),
        "modo_match": req.modo_match,
        "rx": re.compile(req.patron, re.IGNORECASE) if req.modo_match == "regex" else None,
    }

    afectadas = [
        {"hospital_id": a.hospital_id, "tipo": a.tipo, "mensaje": a.mensaje}
        for a in q.all()
        if alerts_engine._match_patron(a.tipo, regla)
    ]
    return {"total": len(afectadas), "alertas": afectadas[:50]}


@router.post("/api/exclusiones")
def crear_exclusion(req: ExclusionRequest, db: Session = Depends(get_db),
                    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    _validar_exclusion(req)

    nueva = database.AlertExclusionModel(
        hospital_id=(req.hospital_id or "*").strip(),
        patron=req.patron.strip(),
        modo_match=req.modo_match,
        accion=req.accion,
        nivel_max=req.nivel_max,
        motivo=(req.motivo or "").strip() or None,
        enabled=req.enabled,
        expires_at=_parse_expira(req.expires_at),
        created_by=current_user.get("email"),
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)

    cerradas = 0
    if req.cerrar_existentes:
        cerradas = _cerrar_alertas_por_regla(db, nueva)

    # El motor releva las reglas en el próximo tick; forzamos para no esperar 60s
    try:
        alerts_engine.cargar_exclusiones(db)
    except Exception:
        pass

    return {"status": "ok", "id": nueva.id, "alertas_cerradas": cerradas}


@router.put("/api/exclusiones/{excl_id}")
def actualizar_exclusion(excl_id: int, req: ExclusionRequest, db: Session = Depends(get_db),
                         current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    _validar_exclusion(req)
    f = db.query(database.AlertExclusionModel).filter_by(id=excl_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Regla no encontrada")

    f.hospital_id = (req.hospital_id or "*").strip()
    f.patron = req.patron.strip()
    f.modo_match = req.modo_match
    f.accion = req.accion
    f.nivel_max = req.nivel_max
    f.motivo = (req.motivo or "").strip() or None
    f.enabled = req.enabled
    f.expires_at = _parse_expira(req.expires_at)
    db.commit()

    try:
        alerts_engine.cargar_exclusiones(db)
    except Exception:
        pass
    return {"status": "ok"}


@router.delete("/api/exclusiones/{excl_id}")
def borrar_exclusion(excl_id: int, db: Session = Depends(get_db),
                     current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    f = db.query(database.AlertExclusionModel).filter_by(id=excl_id).first()
    if not f:
        raise HTTPException(status_code=404, detail="Regla no encontrada")
    db.delete(f)
    db.commit()

    try:
        alerts_engine.cargar_exclusiones(db)
    except Exception:
        pass
    # Ojo: al borrar la regla, las alertas vuelven a levantarse en el próximo tick.
    return {"status": "ok"}


def _cerrar_alertas_por_regla(db, fila):
    """Cierra (y cierra en Asana) las alertas activas que matchean la regla recién creada."""
    q = db.query(database.AlertaModel).filter(database.AlertaModel.is_active == 1)
    if fila.hospital_id != "*":
        q = q.filter(database.AlertaModel.hospital_id == fila.hospital_id)

    regla = {
        "patron_lower": fila.patron.lower(),
        "modo_match": fila.modo_match,
        "rx": re.compile(fila.patron, re.IGNORECASE) if fila.modo_match == "regex" else None,
    }

    ahora = datetime.now()
    n = 0
    for a in q.all():
        if not alerts_engine._match_patron(a.tipo, regla):
            continue
        if a.asana_task_gid:
            try:
                asana_conector.cerrar_tarea_asana(a.asana_task_gid, a.hospital_id, a.tipo, ahora)
            except Exception as e:
                print(f"⚠️ [Exclusiones] No se pudo cerrar en Asana {a.asana_task_gid}: {e}")
        a.is_active = 0
        a.end_time = ahora
        a.mensaje = f"[EXCLUIDA] Regla #{fila.id}: {a.mensaje}"
        n += 1
    db.commit()

    if n:
        try:
            requests.post("http://127.0.0.1:8001/api/internal/trigger-ws", timeout=1)
        except Exception:
            pass

    return n
