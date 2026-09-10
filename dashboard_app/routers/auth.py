"""
Login/logout, páginas de entrada (login, /monitor, /beta), y el rate
limiting de intentos fallidos (por IP y por cuenta).

Noveno router extraído de dashboard.py -- se dejó para casi el final a
propósito: es el módulo de sesión/login, se tocó en la Fase 1 (rate limit por
email además de IP, ver docs/04-seguridad.md#s4) y convenía que ese código
decantara antes de reorganizarlo de lugar. Ver docs/08-plan-refactor-dashboard.md.

Nota de nombres: este archivo hace `import auth`, que resuelve a
dashboard_app/auth.py (el módulo de JWT/bcrypt/roles) -- es un módulo
distinto de este router pese al nombre compartido, Python los distingue por
ruta de import (`auth` vs `routers.auth`).
"""
import time

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import database
from core import get_db, templates

router = APIRouter()

# --- CONTROL DE SEGURIDAD EN MEMORIA ---
# Estructura: {"ip_cliente": {"intentos": int, "bloqueado_hasta": float}}

MAX_INTENTOS_LOGIN = 5
TIEMPO_BLOQUEO_SEG = 300  # 5 minutos

class LoginRequest(BaseModel):
    email: str
    password: str


# --- VISTAS ---
@router.get("/")
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@router.get("/monitor")
def dashboard_page(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@router.post("/api/login")
def verificar_login(request: Request, response: Response, login_data: LoginRequest, db: Session = Depends(get_db)):
    ip_cliente = request.client.host
    ahora = time.time()

    # --- Identificador: puede ser email o username ---
    identificador = login_data.email.lower().strip()

    # 1. Verificar si la IP está bloqueada temporalmente (persistido en DB)
    attempt = db.query(database.LoginAttempt).filter_by(ip=ip_cliente).first()
    if attempt:
        if attempt.bloqueado_hasta > ahora:
            tiempo_restante = int((attempt.bloqueado_hasta - ahora) / 60)
            return {"success": False, "message": f"Demasiados intentos fallidos. Cuenta bloqueada por {tiempo_restante or 1} minuto(s) por seguridad."}
        elif attempt.bloqueado_hasta <= ahora and attempt.intentos >= MAX_INTENTOS_LOGIN:
            # El tiempo de castigo ya pasó, reseteamos el contador
            attempt.intentos = 0
            attempt.bloqueado_hasta = 0
            db.commit()

    # 1b. Verificar si la CUENTA (email/username) está bloqueada, más allá de
    # la IP -- evita fuerza bruta distribuida contra una cuenta puntual.
    attempt_email = db.query(database.LoginAttemptEmail).filter_by(email=identificador).first()
    if attempt_email:
        if attempt_email.bloqueado_hasta > ahora:
            tiempo_restante = int((attempt_email.bloqueado_hasta - ahora) / 60)
            return {"success": False, "message": f"Demasiados intentos fallidos. Cuenta bloqueada por {tiempo_restante or 1} minuto(s) por seguridad."}
        elif attempt_email.bloqueado_hasta <= ahora and attempt_email.intentos >= MAX_INTENTOS_LOGIN:
            attempt_email.intentos = 0
            attempt_email.bloqueado_hasta = 0
            db.commit()

    # Buscamos al usuario por email O por username
    user = db.query(database.UserModel).filter(
        (database.UserModel.email == identificador) | (database.UserModel.username == identificador)
    ).first()

    # --- Política de dominio ---
    # Si encontramos al usuario, usamos SU email real (no el identificador que
    # pudo haber sido un username) para decidir si es interno.
    # Interno (@tecnoimagen.com.ar): siempre habilitado.
    # Externo: SOLO si es una cuenta Cliente provisionada por un Admin.
    email_real = user.email if user else identificador
    es_interno = email_real.endswith("@tecnoimagen.com.ar")
    es_cliente = bool(user and user.role == "Cliente")

    if not es_interno and not es_cliente:
        _registrar_intento_fallido(db, ip_cliente, ahora, identificador)
        return {"success": False, "message": "Credenciales inválidas"}

    # Validación de clave
    if not user or not auth.verify_password(login_data.password, user.hashed_password):
        bloqueado = _registrar_intento_fallido(db, ip_cliente, ahora, identificador)
        msg = "Demasiados intentos. Cuenta bloqueada." if bloqueado else "Credenciales inválidas"
        return {"success": False, "message": msg}

    if not user.is_active:
        return {"success": False, "message": "Usuario inactivo"}

    # 4. ¡Login Exitoso! Limpiar el registro de intentos de esa IP y de la cuenta
    if attempt:
        db.delete(attempt)
    if attempt_email:
        db.delete(attempt_email)
    db.commit()

    # Generar token con el Rol incluido
    token = auth.create_access_token(data={"sub": user.email, "role": user.role})

    response.set_cookie(
        key="tecnomonitor_token",
        value=token,
        httponly=True,     # JS no puede leerla (Previene XSS)
        secure=True,        # Ponelo en True si ya estás usando HTTPS en producción
        samesite="Lax",     # Protege contra ataques CSRF
        max_age=28800       # Expira en 8 horas (en segundos)
    )

    # El token viaja solo en la cookie httpOnly (arriba). No lo devolvemos acá
    # también: exponerlo en el body anula parcialmente la protección de
    # httpOnly contra robo vía XSS. El frontend ya opera vía cookie.
    return {
        "success": True,
        "user": {
            "name": user.full_name,
            "email": user.email,
            "role": user.role,
            "must_change_password": bool(user.must_change_password)
        }
    }

def _registrar_intento_fallido(db: Session, ip_cliente: str, ahora: float, identificador: str) -> bool:
    """
    Suma un intento fallido por IP Y por cuenta (email/username), y bloquea
    cualquiera de los dos que corresponda. Devuelve True si alguno se bloqueó.
    """
    attempt = db.query(database.LoginAttempt).filter_by(ip=ip_cliente).first()
    if not attempt:
        attempt = database.LoginAttempt(ip=ip_cliente, intentos=0, bloqueado_hasta=0)
        db.add(attempt)

    attempt.intentos += 1
    bloqueado = False
    if attempt.intentos >= MAX_INTENTOS_LOGIN:
        attempt.bloqueado_hasta = ahora + TIEMPO_BLOQUEO_SEG
        bloqueado = True

    attempt_email = db.query(database.LoginAttemptEmail).filter_by(email=identificador).first()
    if not attempt_email:
        attempt_email = database.LoginAttemptEmail(email=identificador, intentos=0, bloqueado_hasta=0)
        db.add(attempt_email)

    attempt_email.intentos += 1
    if attempt_email.intentos >= MAX_INTENTOS_LOGIN:
        attempt_email.bloqueado_hasta = ahora + TIEMPO_BLOQUEO_SEG
        bloqueado = True

    db.commit()
    return bloqueado

@router.post("/api/logout")
def logout_usuario(response: Response):
    # Le decimos al navegador que borre la cookie
    response.delete_cookie("tecnomonitor_token", httponly=True, samesite="Lax")
    return {"success": True}

@router.get("/beta")
async def beta_dashboard(request: Request):
    return templates.TemplateResponse("index_beta.html", {"request": request})
