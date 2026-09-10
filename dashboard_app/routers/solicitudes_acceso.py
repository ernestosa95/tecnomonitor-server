"""
Dos flujos de alta que conviven en la app (no es un bug, son dos features
distintas -- ver docs/08-plan-refactor-dashboard.md §3):

1. Legacy (/api/user/request-access): dispara una notificación directa a
   Asana, sin persistir nada en la base. Formulario simple, sin cola de
   aprobación.
2. Nuevo (/api/access-requests + /api/admin/access-requests/...): persiste
   en AccessRequestModel, soporta alta "interno" y "cliente", y tiene cola
   de aprobación/rechazo por un Admin.

Séptimo router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md.
"""
import json
from datetime import datetime
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

import asana_conector
import auth
import database
from core import ROLES_INTERNOS_VALIDOS, generar_password_temporal, get_db
from routers.clientes import _AccesoDTO

router = APIRouter()


# --- DTO para Solicitud de Acceso (flujo legacy) ---
class UserAccessRequest(BaseModel):
    email: str
    nombre: str
    apellido: str
    motivo: str

@router.post("/api/user/request-access")
def solicitar_acceso(req: UserAccessRequest):
    # 1. Validación de dominio corporativo (Seguridad de Backend)
    email_clean = req.email.lower().strip()
    if not email_clean.endswith("@tecnoimagen.com.ar"):
        raise HTTPException(
            status_code=400,
            detail="Acceso denegado: Solo se permiten correos corporativos @tecnoimagen.com.ar"
        )

    # 2. Disparar tarea en Asana
    success = asana_conector.notificar_solicitud_acceso(
        email_clean, req.nombre, req.apellido, req.motivo
    )

    if success:
        return {"status": "ok", "message": "Solicitud enviada con éxito"}
    else:
        raise HTTPException(status_code=500, detail="Error al conectar con Asana")


# --- Flujo nuevo: persistido + cola de aprobación ---
class AccessRequestDTO(BaseModel):
    tipo: str                      # "interno" | "cliente"
    email: str
    nombre: str
    apellido: str | None = None
    full_name_cliente: str | None = None
    motivo: str | None = None
    hospital_ids: List[str] = []

@router.get("/api/hospitales-publico")
def listar_hospitales_publico(db: Session = Depends(get_db)):
    """Lista mínima (id + nombre) para el formulario de solicitud de acceso, sin auth."""
    hospitales = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True
    ).order_by(database.HospitalMetadata.nombre).all()
    return [{"hospital_id": h.hospital_id, "nombre": h.nombre} for h in hospitales]

@router.post("/api/access-requests")
def crear_solicitud_acceso(dto: AccessRequestDTO, db: Session = Depends(get_db)):
    email = dto.email.lower().strip()

    if dto.tipo == "interno":
        if not email.endswith("@tecnoimagen.com.ar"):
            raise HTTPException(400, "El correo debe ser @tecnoimagen.com.ar")
        if not dto.apellido:
            raise HTTPException(400, "Falta el apellido")
    elif dto.tipo == "cliente":
        if email.endswith("@tecnoimagen.com.ar"):
            raise HTTPException(400, "Usá un correo externo para solicitudes de cliente")
        if not dto.full_name_cliente:
            raise HTTPException(400, "Falta el nombre/referencia")
        if not dto.hospital_ids:
            raise HTTPException(400, "Seleccioná al menos un hospital")
    else:
        raise HTTPException(400, "Tipo de solicitud inválido")

    if db.query(database.UserModel).filter_by(email=email).first():
        raise HTTPException(400, "Ya existe una cuenta con ese correo.")
    if db.query(database.AccessRequestModel).filter_by(email=email, estado="pendiente").first():
        raise HTTPException(400, "Ya hay una solicitud pendiente con ese correo.")

    nueva = database.AccessRequestModel(
        tipo=dto.tipo, email=email, nombre=dto.nombre.strip(),
        apellido=(dto.apellido or "").strip() or None,
        full_name_cliente=(dto.full_name_cliente or "").strip() or None,
        motivo=(dto.motivo or "").strip() or None,
        hospitales_solicitados=json.dumps(dto.hospital_ids) if dto.hospital_ids else None,
        estado="pendiente"
    )
    db.add(nueva); db.commit()
    return {"status": "ok", "message": "Solicitud enviada correctamente"}

@router.get("/api/admin/access-requests")
def admin_listar_solicitudes(estado: str = "pendiente", db: Session = Depends(get_db),
                             current_user: dict = Depends(auth.require_roles("Admin"))):
    q = db.query(database.AccessRequestModel)
    if estado != "todas":
        q = q.filter_by(estado=estado)
    out = []
    for r in q.order_by(database.AccessRequestModel.creado_en.desc()).all():
        out.append({
            "id": r.id, "tipo": r.tipo, "email": r.email,
            "nombre": r.nombre, "apellido": r.apellido,
            "full_name_cliente": r.full_name_cliente, "motivo": r.motivo,
            "hospitales_solicitados": json.loads(r.hospitales_solicitados) if r.hospitales_solicitados else [],
            "estado": r.estado, "creado_en": r.creado_en.strftime("%d/%m/%Y %H:%M"),
            "revisado_por": r.revisado_por
        })
    return out

class _AprobarInternoDTO(BaseModel):
    role: str
    asana_id: str | None = None

class _AprobarClienteDTO(BaseModel):
    accesos: List[_AccesoDTO] = []   # reutiliza el DTO ya definido para Clientes

@router.post("/api/admin/access-requests/{req_id}/aprobar-interno")
def aprobar_solicitud_interna(req_id: int, dto: _AprobarInternoDTO, db: Session = Depends(get_db),
                              current_user: dict = Depends(auth.require_roles("Admin"))):
    r = db.query(database.AccessRequestModel).filter_by(id=req_id, tipo="interno", estado="pendiente").first()
    if not r: raise HTTPException(404, "Solicitud no encontrada o ya procesada")
    if dto.role not in ROLES_INTERNOS_VALIDOS:
        raise HTTPException(400, f"Rol inválido. Debe ser uno de: {', '.join(ROLES_INTERNOS_VALIDOS)}")

    temp = generar_password_temporal()
    nuevo = database.UserModel(
        email=r.email, full_name=f"{r.nombre} {r.apellido or ''}".strip(),
        hashed_password=auth.get_password_hash(temp),
        role=dto.role, is_active=True, must_change_password=True,
        asana_id=dto.asana_id or None
    )
    db.add(nuevo)
    r.estado = "aprobado"; r.revisado_por = current_user["email"]; r.revisado_en = datetime.now()
    db.commit()
    return {"status": "ok", "email": nuevo.email, "password_temporal": temp}

@router.post("/api/admin/access-requests/{req_id}/aprobar-cliente")
def aprobar_solicitud_cliente(req_id: int, dto: _AprobarClienteDTO, db: Session = Depends(get_db),
                              current_user: dict = Depends(auth.require_roles("Admin"))):
    r = db.query(database.AccessRequestModel).filter_by(id=req_id, tipo="cliente", estado="pendiente").first()
    if not r: raise HTTPException(404, "Solicitud no encontrada o ya procesada")

    temp = generar_password_temporal()
    nuevo = database.UserModel(
        email=r.email, full_name=r.full_name_cliente,
        hashed_password=auth.get_password_hash(temp),
        role="Cliente", is_active=True, must_change_password=True
    )
    db.add(nuevo); db.commit(); db.refresh(nuevo)
    for a in dto.accesos:
        db.add(database.ClienteHospitalAccess(user_id=nuevo.id, hospital_id=a.hospital_id,
                                              ver_infra=a.infra, ver_software=a.software, ver_kpis=a.kpis))
    r.estado = "aprobado"; r.revisado_por = current_user["email"]; r.revisado_en = datetime.now()
    db.commit()
    return {"status": "ok", "email": nuevo.email, "password_temporal": temp}

@router.post("/api/admin/access-requests/{req_id}/rechazar")
def rechazar_solicitud(req_id: int, db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.require_roles("Admin"))):
    r = db.query(database.AccessRequestModel).filter_by(id=req_id, estado="pendiente").first()
    if not r: raise HTTPException(404, "Solicitud no encontrada o ya procesada")
    r.estado = "rechazado"; r.revisado_por = current_user["email"]; r.revisado_en = datetime.now()
    db.commit()
    return {"status": "ok"}
