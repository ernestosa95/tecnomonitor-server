"""
Panel de administración de usuarios rol Cliente (acceso externo por
hospital), la página /cliente, y el listado de casos/incidentes que ve un
Cliente para sus hospitales habilitados.

Sexto router extraído de dashboard.py (junto con routers/usuarios.py, comparten
el patrón de generar_password_temporal) -- ver docs/08-plan-refactor-dashboard.md.

Nota: _AccesoDTO también lo reutiliza routers/solicitudes_acceso.py (paso
futuro) para el flujo de aprobación de un alta tipo "cliente" -- se importa
desde acá cuando llegue ese paso.
"""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import asana_conector
import auth
import database
from core import generar_password_temporal, get_db

router = APIRouter()

# ============================================================
# PANEL DE ADMINISTRACIÓN DE CLIENTES  (solo Admin)
# ============================================================
class _AccesoDTO(BaseModel):
    hospital_id: str
    infra: bool = False
    software: bool = False
    kpis: bool = False

class _CrearClienteDTO(BaseModel):
    email: str
    full_name: str
    accesos: List[_AccesoDTO] = []

class _AccesosUpdateDTO(BaseModel):
    accesos: List[_AccesoDTO] = []

def _serializar_accesos(db, user):
    metas = {m.hospital_id: m for m in db.query(database.HospitalMetadata).all()}
    out = []
    for a in db.query(database.ClienteHospitalAccess).filter_by(user_id=user.id).all():
        m = metas.get(a.hospital_id)
        out.append({"hospital_id": a.hospital_id,
                    "nombre": m.nombre if m else a.hospital_id,
                    "infra": a.ver_infra, "software": a.ver_software, "kpis": a.ver_kpis})
    return out


@router.get("/api/admin/clientes")
def admin_listar_clientes(db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_roles("Admin"))):
    out = []
    for c in db.query(database.UserModel).filter_by(role="Cliente").all():
        out.append({"id": c.id, "email": c.email, "full_name": c.full_name,
                    "is_active": c.is_active,
                    "must_change_password": bool(c.must_change_password),
                    "cant_hospitales": db.query(database.ClienteHospitalAccess)
                                         .filter_by(user_id=c.id).count()})
    return out

@router.post("/api/admin/clientes")
def admin_crear_cliente(dto: _CrearClienteDTO, db: Session = Depends(get_db),
                        current_user: dict = Depends(auth.require_roles("Admin"))):
    email = dto.email.lower().strip()
    if email.endswith("@tecnoimagen.com.ar"):
        raise HTTPException(400, "Un correo interno no puede ser Cliente. Usá el correo del hospital.")
    if db.query(database.UserModel).filter_by(email=email).first():
        raise HTTPException(400, "Ya existe un usuario con ese correo.")

    temp = generar_password_temporal()
    nuevo = database.UserModel(email=email, full_name=dto.full_name.strip(),
                               hashed_password=auth.get_password_hash(temp),
                               role="Cliente", is_active=True, must_change_password=True)
    db.add(nuevo); db.commit(); db.refresh(nuevo)
    for a in dto.accesos:
        db.add(database.ClienteHospitalAccess(user_id=nuevo.id, hospital_id=a.hospital_id,
                                              ver_infra=a.infra, ver_software=a.software, ver_kpis=a.kpis))
    db.commit()
    # ⚠️ La contraseña temporal se devuelve UNA sola vez (en la base solo queda el hash).
    return {"status": "ok", "id": nuevo.id, "email": nuevo.email,
            "password_temporal": temp, "accesos": _serializar_accesos(db, nuevo)}

@router.get("/api/admin/clientes/{cliente_id}/accesos")
def admin_ver_accesos(cliente_id: int, db: Session = Depends(get_db),
                      current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter_by(id=cliente_id, role="Cliente").first()
    if not u: raise HTTPException(404, "Cliente no encontrado")
    return {"id": u.id, "email": u.email, "accesos": _serializar_accesos(db, u)}

@router.put("/api/admin/clientes/{cliente_id}/accesos")
def admin_actualizar_accesos(cliente_id: int, dto: _AccesosUpdateDTO,
                             db: Session = Depends(get_db),
                             current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter_by(id=cliente_id, role="Cliente").first()
    if not u: raise HTTPException(404, "Cliente no encontrado")
    deseados = {a.hospital_id: a for a in dto.accesos}
    existentes = {a.hospital_id: a for a in db.query(database.ClienteHospitalAccess)
                                              .filter_by(user_id=u.id).all()}
    for hid, a in deseados.items():
        if hid in existentes:
            r = existentes[hid]; r.ver_infra, r.ver_software, r.ver_kpis = a.infra, a.software, a.kpis
        else:
            db.add(database.ClienteHospitalAccess(user_id=u.id, hospital_id=hid,
                   ver_infra=a.infra, ver_software=a.software, ver_kpis=a.kpis))
    for hid, r in existentes.items():
        if hid not in deseados: db.delete(r)
    db.commit()
    return {"status": "ok", "accesos": _serializar_accesos(db, u)}

@router.patch("/api/admin/clientes/{cliente_id}/toggle-active")
def admin_toggle_active(cliente_id: int, db: Session = Depends(get_db),
                        current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter_by(id=cliente_id, role="Cliente").first()
    if not u: raise HTTPException(404, "Cliente no encontrado")
    u.is_active = not u.is_active; db.commit()
    return {"status": "ok", "is_active": u.is_active}

@router.post("/api/admin/clientes/{cliente_id}/reset-password")
def admin_reset_password(cliente_id: int, db: Session = Depends(get_db),
                         current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter_by(id=cliente_id, role="Cliente").first()
    if not u: raise HTTPException(404, "Cliente no encontrado")
    temp = generar_password_temporal()
    u.hashed_password = auth.get_password_hash(temp); u.must_change_password = True; db.commit()
    return {"status": "ok", "password_temporal": temp}

@router.get("/cliente")
def pagina_cliente(request: Request, db: Session = Depends(get_db)):
    try:
        user = auth.get_current_user(request, db)
    except Exception:
        return RedirectResponse("/")
    if user["role"] != "Cliente":
        return RedirectResponse("/beta")   # internos van a la app interna
    return FileResponse("dashboard_app/templates/cliente.html")


@router.get("/api/cliente/casos/{hospital_id}")
def listar_casos_cliente(hospital_id: str, db: Session = Depends(get_db),
                         current_user: dict = Depends(auth.get_current_user)):
    if current_user["role"] != "Cliente":
        raise HTTPException(403, "Solo disponible para clientes")

    hospitales_permitidos = auth.hospitales_de_cliente(current_user["email"], db)
    if not any(h["hospital_id"] == hospital_id for h in hospitales_permitidos):
        raise HTTPException(403, "No tenés acceso a este hospital")

    hosp = db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first()
    if not hosp or not hosp.asana_project_id:
        return []

    return asana_conector.listar_casos_abiertos(hosp.asana_project_id)
