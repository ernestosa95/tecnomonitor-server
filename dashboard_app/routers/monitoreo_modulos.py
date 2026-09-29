"""
Módulos de monitoreo dados de baja por hospital (REQ-03, docs/16): lista de "dados de baja" del
detalle del hospital, baja manual y reversión, y la vista previa global que se revisa antes de
prender `monitoreo_bajas_enabled`. La lógica vive en alerts_engine/modulos.py.
"""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import database
from alerts_engine import modulos
from core import get_db

router = APIRouter()

_ROLES_LECTURA = ("Admin", "Ingenieria", "Visor", "Comercial")
_ROLES_ESCRITURA = ("Admin", "Ingenieria")


class BajaManualRequest(BaseModel):
    motivo: Optional[str] = None


@router.get("/api/hospital/{hospital_id}/monitoreo-modulos")
def listar_modulos_hospital(hospital_id: str, db: Session = Depends(get_db),
                            current_user: dict = Depends(auth.require_roles(*_ROLES_LECTURA))):
    switch = modulos.switch_activo(db)
    filas = db.query(database.MonitoreoModulo).filter_by(hospital_id=hospital_id).order_by(
        database.MonitoreoModulo.modulo).all()
    return {
        "switch": switch,
        "modulos": [modulos.serializar(f, switch) for f in filas],
        # Para el selector de baja manual: los módulos que todavía no tienen fila.
        "disponibles": [{"modulo": m, "label": d["label"]} for m, d in modulos.MODULOS.items()
                        if m not in {f.modulo for f in filas}],
    }


@router.post("/api/hospital/{hospital_id}/monitoreo-modulos/{modulo}/baja")
def baja_manual(hospital_id: str, modulo: str, req: BajaManualRequest,
                db: Session = Depends(get_db),
                current_user: dict = Depends(auth.require_roles(*_ROLES_ESCRITURA))):
    if modulo not in modulos.MODULOS:
        raise HTTPException(status_code=400, detail="Módulo desconocido")
    if not db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first():
        raise HTTPException(status_code=404, detail="Hospital no encontrado")

    ahora = datetime.now()
    fila = db.query(database.MonitoreoModulo).filter_by(hospital_id=hospital_id, modulo=modulo).first()
    if fila and fila.origen == modulos.ORIGEN_MANUAL:
        raise HTTPException(status_code=409, detail="El módulo ya está dado de baja a mano")
    if fila is None:
        fila = database.MonitoreoModulo(hospital_id=hospital_id, modulo=modulo)
        db.add(fila)
    # Una baja del agente (pendiente o no) pasa a manual: sin gracia y efectiva ya.
    fila.estado = modulos.ESTADO_DESACTIVADO
    fila.origen = modulos.ORIGEN_MANUAL
    fila.desactivado_desde = ahora
    fila.acciones_aplicadas_en = None
    fila.motivo = (req.motivo or "").strip() or "Baja manual"
    fila.actualizado_por = current_user.get("email")

    cerradas = modulos.aplicar_baja(db, fila)
    db.commit()
    modulos.cargar_bajas(db)  # que el motor lo tome sin esperar al próximo tick
    if cerradas:
        modulos._avisar_ws()
    return {"status": "ok", "alertas_cerradas": cerradas}


@router.delete("/api/hospital/{hospital_id}/monitoreo-modulos/{modulo}")
def revertir_baja(hospital_id: str, modulo: str, db: Session = Depends(get_db),
                  current_user: dict = Depends(auth.require_roles(*_ROLES_ESCRITURA))):
    fila = db.query(database.MonitoreoModulo).filter_by(hospital_id=hospital_id, modulo=modulo).first()
    if not fila:
        raise HTTPException(status_code=404, detail="El módulo no está dado de baja")
    if fila.origen != modulos.ORIGEN_MANUAL:
        raise HTTPException(status_code=409,
                            detail="La baja la declaró el agente: se revierte reactivando el módulo en el agente")
    db.delete(fila)
    db.commit()
    modulos.cargar_bajas(db)
    return {"status": "ok"}


@router.get("/api/monitoreo-modulos/preview")
def preview_bajas(db: Session = Depends(get_db),
                  current_user: dict = Depends(auth.require_roles(*_ROLES_ESCRITURA))):
    return modulos.preview(db)
