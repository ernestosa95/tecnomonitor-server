"""
Sirve los protocolos de atención (docs/runbooks/<slug>.md) como HTML,
autenticado igual que el resto del dashboard. El link se arma en
alerts_engine/runbooks.py y se adjunta a la tarea de Asana cuando se abre
una alerta -- así el colaborador que la recibe no necesita acceso al
repositorio para ver el protocolo.

Ver conversación sobre última milla de alertas / protocolos de atención.
"""
import os
import re

import markdown
from fastapi import APIRouter, Depends, HTTPException, Request

import auth
from core import templates

router = APIRouter()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_RUNBOOKS_DIR = os.path.join(_REPO_ROOT, "docs", "runbooks")
_SLUG_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@router.get("/runbooks/{slug}")
def ver_runbook(slug: str, request: Request,
                current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    # Slug estricto (solo minúsculas/números/guiones): evita path traversal
    # y cualquier intento de leer un archivo fuera de docs/runbooks/.
    if not _SLUG_VALIDO.match(slug):
        raise HTTPException(status_code=404, detail="Protocolo no encontrado")

    ruta_md = os.path.join(_RUNBOOKS_DIR, f"{slug}.md")
    if not os.path.isfile(ruta_md):
        raise HTTPException(status_code=404, detail="Protocolo no encontrado")

    with open(ruta_md, "r", encoding="utf-8") as f:
        contenido_md = f.read()

    contenido_html = markdown.markdown(contenido_md, extensions=["tables", "fenced_code"])

    return templates.TemplateResponse("runbook.html", {
        "request": request,
        "contenido_html": contenido_html,
    })
