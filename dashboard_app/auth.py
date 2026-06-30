import os
from datetime import datetime, timedelta
from jose import JWTError, jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from dotenv import load_dotenv
import database # Importamos tus modelos y conexión
from fastapi import Request
import permissions

load_dotenv()

# ==========================================
# PUNTO 1: CLAVE SEGURA Y SIN FALLBACKS
# ==========================================
# Leemos la clave del entorno. Si no existe, matamos la ejecución.
# Ya no hay "supersecret-tecno-2026" por defecto.
SECRET_KEY = os.getenv("JWT_SECRET")
if not SECRET_KEY:
    raise RuntimeError("CRÍTICO: La variable de entorno JWT_SECRET no está definida en el archivo .env")

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480 # 8 horas

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="api/login")

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def get_password_hash(password):
    return pwd_context.hash(password)

def create_access_token(data: dict, expires_delta: timedelta = None):
    to_encode = data.copy()
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    
    to_encode.update({"exp": expire})
    encoded_jwt = jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)
    return encoded_jwt

# Helper para la inyección de dependencias de la base de datos
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==========================================
# PUNTO 2: VALIDACIÓN ESTRICTA EN BASE DE DATOS
# ==========================================
def get_current_user(request: Request, db: Session = Depends(get_db)):
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="No se pudieron validar las credenciales",
    )
    
    # 🛡️ LEEMOS EL TOKEN DESDE LA COOKIE EN VEZ DEL HEADER
    token = request.cookies.get("tecnomonitor_token")
    if not token:
        raise credentials_exception
    
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        email: str = payload.get("sub")
        if email is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    user = db.query(database.UserModel).filter(database.UserModel.email == email.lower()).first()
    
    if user is None or not user.is_active:
        raise credentials_exception
        
    return {"email": user.email, "role": user.role}

def require_roles(*allowed_roles):
    """
    Fábrica de dependencias que usa get_current_user (el cual ya validó en BD).
    """
    def role_checker(current_user: dict = Depends(get_current_user)):
        if current_user["role"] not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos suficientes para realizar esta acción"
            )
        return current_user
    return role_checker

def require_view(vista: str):
    """
    Igual que require_roles, pero deriva los roles permitidos desde
    permissions.PERMISOS. Una sola fuente de verdad para toda la app.
    """
    def view_checker(current_user: dict = Depends(get_current_user)):
        if not permissions.puede_ver(current_user["role"], vista):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="No tienes permisos suficientes para realizar esta acción"
            )
        return current_user
    return view_checker

def hospitales_de_cliente(email: str, db) -> list:
    """
    Lista los hospitales asignados a un Cliente con las pestañas
    habilitadas en cada uno. Para roles internos devuelve [] (no aplica).
    """
    user = db.query(database.UserModel).filter(
        database.UserModel.email == email.lower()
    ).first()
    if not user:
        return []

    accesos = db.query(database.ClienteHospitalAccess).filter_by(user_id=user.id).all()
    metas = {m.hospital_id: m for m in db.query(database.HospitalMetadata).all()}

    out = []
    for a in accesos:
        meta = metas.get(a.hospital_id)
        out.append({
            "hospital_id": a.hospital_id,
            "nombre": meta.nombre if meta else a.hospital_id,
            "tabs": {
                "infra": a.ver_infra,
                "software": a.ver_software,
                "kpis": a.ver_kpis,
            },
        })
    return out

def require_hospital_access(tab: str = None):
    """
    Para endpoints de datos de UN hospital (ruta con {hospital_id}).
    - Roles internos (scope 'red'): pasan igual que antes. CERO cambio.
    - Cliente (scope 'hospitales'): solo hospitales asignados, y si se
      indica `tab` ('infra'|'software'|'kpis'), solo si está habilitada.
    """
    def checker(hospital_id: str,
                db: Session = Depends(get_db),
                current_user: dict = Depends(get_current_user)):
        if permissions.scope_de(current_user["role"]) != permissions.SCOPE_HOSPITALES:
            return current_user  # interno: sin restricción por hospital

        user = db.query(database.UserModel).filter(
            database.UserModel.email == current_user["email"].lower()
        ).first()
        acc = db.query(database.ClienteHospitalAccess).filter_by(
            user_id=user.id, hospital_id=hospital_id
        ).first() if user else None

        if not acc:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="No tenés acceso a este hospital")
        if tab:
            habilitada = {"infra": acc.ver_infra,
                          "software": acc.ver_software,
                          "kpis": acc.ver_kpis}.get(tab, False)
            if not habilitada:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                    detail="Esta sección no está habilitada para tu hospital")
        return current_user
    return checker


def bloquear_cliente():
    """Para endpoints de RED COMPLETA: un Cliente no debe verlos."""
    def checker(current_user: dict = Depends(get_current_user)):
        if permissions.scope_de(current_user["role"]) == permissions.SCOPE_HOSPITALES:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="Acceso no permitido para tu perfil")
        return current_user
    return checker