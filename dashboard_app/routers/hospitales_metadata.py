"""
ABM de la metadata de hospitales: alta/edición/baja, toggles de
visibilidad/alertas/RIS/datos-manuales, y los KPIs manuales para hospitales
sin agente de monitoreo instalado.

Cuarto router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import database
from core import generar_ingest_token, get_db, hash_ingest_token
from database import HospitalMetadata

router = APIRouter()


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
    datos_manuales: bool = False


class ManualKPIDTO(BaseModel):
    estudios: int = 0
    admitidas: int = 0
    asociadas: int = 0
    definitivas: int = 0
    ia: int = 0
    equipos: int = 0
    tb_alm: Optional[float] = None
    tb_disp: Optional[float] = None
    ram: Optional[float] = None
    go_live: Optional[str] = None


# --- METADATA HOSPITALES (Punto 2) ---
@router.get("/api/hospitales-metadata")
def listar_hospitales_metadata(db: Session = Depends(get_db),
                               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    return db.query(HospitalMetadata).all()

@router.post("/api/hospitales-metadata")
def crear_hospital_metadata(dto: HospitalDTO,
                            db: Session = Depends(get_db),
                            current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    existe = db.query(HospitalMetadata).filter_by(hospital_id=dto.hospital_id).first()
    if existe: raise HTTPException(status_code=400, detail="El ID existe")

    # Todo hospital nuevo nace con su token de ingesta (schema_version 4.5+).
    # Ver docs/11-plan-auth-ingesta-agente.md.
    token = generar_ingest_token()
    nuevo = HospitalMetadata(**dto.dict(), ingest_token_hash=hash_ingest_token(token))
    db.add(nuevo); db.commit()
    return {"status": "ok", "msg": "Creado", "ingest_token": token}

@router.put("/api/hospitales-metadata/{hid}")
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
    h.datos_manuales = dto.datos_manuales

    db.commit()
    return {"status": "ok", "msg": "Actualizado"}

@router.patch("/api/hospitales-metadata/{hid}/toggle")
def toggle_visibilidad(hid: str,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.is_visible = not h.is_visible
    db.commit()
    return {"status": "ok", "new_state": h.is_visible}

# Nueva ruta para togglear alertas (Punto 2)
@router.patch("/api/hospitales-metadata/{hid}/toggle-alerts")
def toggle_alertas(hid: str,
                   db: Session = Depends(get_db),
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.alerts_enabled = not h.alerts_enabled
    db.commit()
    return {"status": "ok", "alerts_enabled": h.alerts_enabled}

@router.patch("/api/hospitales-metadata/{hid}/toggle-ris")
def toggle_ris(hid: str,
               db: Session = Depends(get_db),
               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")

    # Invertimos el valor actual (asume False si es None)
    h.has_ris = not getattr(h, 'has_ris', False)
    db.commit()
    return {"status": "ok", "has_ris": h.has_ris}

@router.post("/api/hospitales-metadata/{hid}/regenerar-token")
def regenerar_ingest_token(hid: str,
                           db: Session = Depends(get_db),
                           current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    """
    Genera un token de ingesta nuevo para el hospital y pisa el hash
    guardado -- el token anterior queda invalidado. Se usa tanto para
    migrar hospitales existentes a schema_version 4.5 como para rotar un
    token filtrado. El valor en texto plano solo se devuelve acá, una vez.
    """
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")

    token = generar_ingest_token()
    h.ingest_token_hash = hash_ingest_token(token)
    db.commit()
    return {"status": "ok", "ingest_token": token}

@router.delete("/api/hospitales-metadata/{hid}")
def eliminar_hospital_metadata(hid: str,
                               db: Session = Depends(get_db),
                               current_user: dict = Depends(auth.require_roles("Admin"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    db.delete(h); db.commit()
    return {"status": "ok", "msg": "Eliminado"}

@router.patch("/api/hospitales-metadata/{hid}/toggle-manual")
def toggle_datos_manuales(hid: str,
                          db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    h = db.query(HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h: raise HTTPException(status_code=404, detail="No encontrado")
    h.datos_manuales = not getattr(h, 'datos_manuales', False)
    db.commit()
    return {"status": "ok", "datos_manuales": h.datos_manuales}

@router.get("/api/hospitales-metadata/{hid}/manual-kpi")
def get_manual_kpi(hid: str,
                   db: Session = Depends(get_db),
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    fila = db.query(database.HospitalManualKPI).filter_by(hospital_id=hid).first()
    if not fila:
        return {"hospital_id": hid, "existe": False, **ManualKPIDTO().dict()}
    return {
        "hospital_id": hid,
        "existe": True,
        "estudios": fila.estudios, "admitidas": fila.admitidas,
        "asociadas": fila.asociadas, "definitivas": fila.definitivas,
        "ia": fila.ia, "equipos": fila.equipos,
        "tb_alm": fila.tb_alm, "tb_disp": fila.tb_disp, "ram": fila.ram,
        "go_live": fila.go_live,
        "actualizado_en": fila.actualizado_en.isoformat() if fila.actualizado_en else None,
        "actualizado_por": fila.actualizado_por,
    }

@router.put("/api/hospitales-metadata/{hid}/manual-kpi")
def set_manual_kpi(hid: str, dto: ManualKPIDTO,
                   db: Session = Depends(get_db),
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    if not db.query(HospitalMetadata).filter_by(hospital_id=hid).first():
        raise HTTPException(status_code=404, detail="Hospital no encontrado")

    fila = db.query(database.HospitalManualKPI).filter_by(hospital_id=hid).first()
    if not fila:
        fila = database.HospitalManualKPI(hospital_id=hid)
        db.add(fila)

    for campo, valor in dto.dict().items():
        setattr(fila, campo, valor)
    fila.actualizado_por = current_user["email"]

    db.commit()
    return {"status": "ok", "msg": "Datos manuales guardados"}
