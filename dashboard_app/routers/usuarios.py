"""
Perfil propio, cambio de contraseña, permisos del usuario logueado, y el
panel de administración de usuarios internos (Admin/Ingenieria/Comercial/Visor
-- el rol Cliente se gestiona en routers/clientes.py).

Sexto router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md.
"""
import re as _re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import database
import permissions
from core import ROLES_INTERNOS_VALIDOS, generar_password_temporal, get_db

router = APIRouter()

USERNAME_REGEX = _re.compile(r"^[a-z0-9](?:[a-z0-9._-]{1,30}[a-z0-9])?$")


# --- DTO para cambio de clave ---
class ChangePasswordRequest(BaseModel):
    email: str
    current_password: str | None = None
    new_password: str

# --- FUNCIÓN DE VALIDACIÓN DE CONTRASEÑAS ---
def validar_password(pw: str):
    if len(pw) < 10:
        raise ValueError("La contraseña debe tener al menos 10 caracteres.")
    if not _re.search(r"[A-Z]", pw):
        raise ValueError("La contraseña debe contener al menos una letra mayúscula.")
    if not _re.search(r"[0-9]", pw):
        raise ValueError("La contraseña debe contener al menos un número.")
    if not _re.search(r"[!@#$%^&*(),.?\":{}|<>]", pw):
        raise ValueError("La contraseña debe contener al menos un carácter especial.")

# --- ENDPOINT ACTUALIZADO ---
@router.post("/api/user/change-password")
def cambiar_contrasena(req: ChangePasswordRequest,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.get_current_user)):

    if current_user["email"].lower() != req.email.lower():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No podés modificar la contraseña de otro usuario."
        )

    user = db.query(database.UserModel).filter(database.UserModel.email == req.email.lower()).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    # Si NO es un primer cambio forzado, exigimos y verificamos la clave actual.
    # Si SÍ lo es, el usuario ya se autenticó con la clave temporal para llegar
    # hasta acá (tiene cookie de sesión válida), así que no hace falta repetirla.
    if not user.must_change_password:
        if not req.current_password:
            raise HTTPException(status_code=400, detail="Falta la contraseña actual")
        if not auth.verify_password(req.current_password, user.hashed_password):
            raise HTTPException(status_code=400, detail="La contraseña actual es incorrecta")

    try:
        validar_password(req.new_password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    user.hashed_password = auth.get_password_hash(req.new_password)
    user.must_change_password = False
    db.commit()

    return {"status": "ok", "message": "Contraseña actualizada correctamente"}


# ── PERFIL DE USUARIO ──────────────────────────────────────

class PerfilUpdateRequest(BaseModel):
    full_name: str

@router.get("/api/usuario/perfil")
def get_perfil(
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db)          # ← get_db local, no database.get_db
):
    user = db.query(database.UserModel).filter(
        database.UserModel.email == current_user["email"]
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")
    return {
        "email": user.email,
        "full_name": user.full_name or "",
        "role": user.role
    }

@router.put("/api/usuario/perfil")
def update_perfil(
    req: PerfilUpdateRequest,
    current_user: dict = Depends(auth.get_current_user),
    db: Session = Depends(get_db)          # ← get_db local, no database.get_db
):
    if not req.full_name.strip():
        raise HTTPException(status_code=400, detail="El nombre no puede estar vacío")

    user = db.query(database.UserModel).filter(
        database.UserModel.email == current_user["email"]
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Usuario no encontrado")

    user.full_name = req.full_name.strip()
    db.commit()

    return {
        "ok": True,
        "full_name": user.full_name,
        "email": user.email,
        "role": user.role
    }

@router.get("/api/me/permissions")
def get_my_permissions(db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.get_current_user)):
    base = permissions.permisos_de_usuario(current_user["role"])
    # Si el usuario tiene scope acotado por hospital (Cliente), sumamos su lista.
    if base["scope"] == permissions.SCOPE_HOSPITALES:
        base["hospitales"] = auth.hospitales_de_cliente(current_user["email"], db)
    return base


# ============================================================
# PANEL DE ADMINISTRACIÓN DE USUARIOS INTERNOS (solo Admin)
# ============================================================

class _CrearUsuarioDTO(BaseModel):
    email: str
    username: str
    full_name: str
    role: str
    asana_id: str | None = None

class _EditarUsuarioDTO(BaseModel):
    username: str
    full_name: str
    role: str
    asana_id: str | None = None

@router.get("/api/admin/usuarios")
def admin_listar_usuarios(db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_roles("Admin"))):
    out = []
    for u in db.query(database.UserModel).filter(database.UserModel.role != "Cliente").order_by(database.UserModel.role, database.UserModel.full_name).all():
        out.append({
            "id": u.id, "email": u.email, "username": u.username, "full_name": u.full_name,
            "role": u.role, "is_active": u.is_active,
            "must_change_password": bool(u.must_change_password),
            "asana_id": u.asana_id
        })
    return out

@router.post("/api/admin/usuarios")
def admin_crear_usuario(dto: _CrearUsuarioDTO, db: Session = Depends(get_db),
                        current_user: dict = Depends(auth.require_roles("Admin"))):
    email = dto.email.lower().strip()
    username = dto.username.lower().strip()

    if not email.endswith("@tecnoimagen.com.ar"):
        raise HTTPException(400, "El correo debe ser @tecnoimagen.com.ar")
    if dto.role not in ROLES_INTERNOS_VALIDOS:
        raise HTTPException(400, f"Rol inválido. Debe ser uno de: {', '.join(ROLES_INTERNOS_VALIDOS)}")
    try:
        validar_username(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if db.query(database.UserModel).filter_by(email=email).first():
        raise HTTPException(400, "Ya existe un usuario con ese correo.")
    if db.query(database.UserModel).filter_by(username=username).first():
        raise HTTPException(400, "Ese username ya está en uso.")

    temp = generar_password_temporal()
    nuevo = database.UserModel(
        email=email, username=username, full_name=dto.full_name.strip(),
        hashed_password=auth.get_password_hash(temp),
        role=dto.role, is_active=True, must_change_password=True,
        asana_id=dto.asana_id or None
    )
    db.add(nuevo); db.commit(); db.refresh(nuevo)
    return {"status": "ok", "id": nuevo.id, "email": nuevo.email, "username": nuevo.username, "password_temporal": temp}

@router.put("/api/admin/usuarios/{user_id}")
def admin_editar_usuario(user_id: int, dto: _EditarUsuarioDTO, db: Session = Depends(get_db),
                         current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter(database.UserModel.id == user_id,
                                             database.UserModel.role != "Cliente").first()
    if not u: raise HTTPException(404, "Usuario no encontrado")
    if dto.role not in ROLES_INTERNOS_VALIDOS:
        raise HTTPException(400, f"Rol inválido. Debe ser uno de: {', '.join(ROLES_INTERNOS_VALIDOS)}")

    username = dto.username.lower().strip()
    try:
        validar_username(username)
    except ValueError as e:
        raise HTTPException(400, str(e))
    existente = db.query(database.UserModel).filter_by(username=username).first()
    if existente and existente.id != u.id:
        raise HTTPException(400, "Ese username ya está en uso por otro usuario.")

    u.full_name = dto.full_name.strip()
    u.role = dto.role
    u.username = username
    u.asana_id = dto.asana_id or None
    db.commit()
    return {"status": "ok"}

@router.patch("/api/admin/usuarios/{user_id}/toggle-active")
def admin_toggle_usuario(user_id: int, db: Session = Depends(get_db),
                         current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter(database.UserModel.id == user_id,
                                             database.UserModel.role != "Cliente").first()
    if not u: raise HTTPException(404, "Usuario no encontrado")
    # Protección: un Admin no puede desactivarse a sí mismo por error
    if u.email == current_user["email"] and u.is_active:
        raise HTTPException(400, "No podés desactivar tu propia cuenta.")
    u.is_active = not u.is_active; db.commit()
    return {"status": "ok", "is_active": u.is_active}

@router.post("/api/admin/usuarios/{user_id}/reset-password")
def admin_reset_password_usuario(user_id: int, db: Session = Depends(get_db),
                                 current_user: dict = Depends(auth.require_roles("Admin"))):
    u = db.query(database.UserModel).filter(database.UserModel.id == user_id,
                                             database.UserModel.role != "Cliente").first()
    if not u: raise HTTPException(404, "Usuario no encontrado")
    temp = generar_password_temporal()
    u.hashed_password = auth.get_password_hash(temp); u.must_change_password = True; db.commit()
    return {"status": "ok", "password_temporal": temp}

def validar_username(username: str):
    if not USERNAME_REGEX.match(username):
        raise ValueError(
            "El username debe tener 3-32 caracteres, minúsculas, números, "
            "puntos, guiones o guiones bajos, y no puede empezar ni terminar con símbolo."
        )
