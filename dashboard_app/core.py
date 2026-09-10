"""
Objetos compartidos por 2 o más routers de dashboard_app: la sesión de DB, el
motor de templates Jinja2, y el limiter de rate limiting (slowapi).

Todo lo que solo usa UN router (constantes, DTOs, helpers de un dominio
puntual) vive en ese router, no acá -- la idea es que este módulo se quede
chico. Ver docs/08-plan-refactor-dashboard.md.
"""
import os
import secrets
import string

from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

import database

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
STATIC_DIR = os.path.join(BASE_DIR, "static")

templates = Jinja2Templates(directory=TEMPLATES_DIR)

limiter = Limiter(key_func=get_remote_address)


async def custom_rate_limit_handler(request: Request, exc: RateLimitExceeded):
    return JSONResponse(
        status_code=429,
        content={"detail": "Estás generando reportes demasiado rápido. Esperá un minuto e intentá de nuevo."}
    )


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Roles internos válidos (no incluye "Cliente", que se gestiona aparte).
# Compartido por routers/usuarios.py y routers/solicitudes_acceso.py.
ROLES_INTERNOS_VALIDOS = ["Admin", "Ingenieria", "Comercial", "Visor"]


def generar_password_temporal(n: int = 14) -> str:
    """
    Contraseña temporal aleatoria (no una constante fija -- ver
    docs/04-seguridad.md#s1c). Compartida por routers/usuarios.py,
    routers/clientes.py y routers/solicitudes_acceso.py: los tres crean
    cuentas nuevas con el mismo criterio de password temporal + forzar
    cambio en el primer login.
    """
    especiales = "!@#$%&*?"
    pools = [string.ascii_uppercase, string.ascii_lowercase, string.digits, especiales]
    chars = [secrets.choice(p) for p in pools]  # garantiza 1 de cada tipo
    todos = string.ascii_letters + string.digits + especiales
    chars += [secrets.choice(todos) for _ in range(n - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)
