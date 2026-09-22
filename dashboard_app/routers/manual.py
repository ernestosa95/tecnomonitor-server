"""
Sirve la documentación técnica del repo (docs/*.md) como HTML, autenticado
igual que los runbooks (mismo patrón, generalizado a un listado + una
página por doc). No usa `/docs` a propósito -- FastAPI ya sirve el Swagger
en esa ruta -- por eso esta va en `/manual`.

No incluye `docs/runbooks/` (tiene su propia ruta en routers/runbooks.py,
pensada para pegarse en un ticket de Asana) ni `README.md` (es el índice
del propio repo, no un documento para leer acá).
"""
import os
import re

import markdown
from fastapi import APIRouter, Depends, HTTPException, Request

import auth
from core import templates

router = APIRouter()

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DOCS_DIR = os.path.join(_REPO_ROOT, "docs")
_SLUG_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _listar_docs():
    """[{"slug", "titulo"}], en el orden en que aparecen los archivos (el
    prefijo numérico del nombre ya los deja en orden de lectura)."""
    items = []
    if not os.path.isdir(_DOCS_DIR):
        return items
    for nombre in sorted(os.listdir(_DOCS_DIR)):
        if not nombre.endswith(".md") or nombre.upper() == "README.MD":
            continue
        slug = nombre[:-3].lower()
        if not _SLUG_VALIDO.match(slug):
            continue
        titulo = slug
        try:
            with open(os.path.join(_DOCS_DIR, nombre), "r", encoding="utf-8") as f:
                for linea in f:
                    if linea.startswith("# "):
                        titulo = linea[2:].strip()
                        break
        except OSError:
            pass
        items.append({"slug": slug, "titulo": titulo})
    return items


@router.get("/manual")
def listar_manual(request: Request,
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    return templates.TemplateResponse("manual_index.html", {
        "request": request,
        "docs": _listar_docs(),
    })


@router.get("/manual/{slug}")
def ver_doc(slug: str, request: Request,
            current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    # Slug estricto (solo minúsculas/números/guiones): evita path traversal
    # y cualquier intento de leer un archivo fuera de docs/.
    if not _SLUG_VALIDO.match(slug):
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    ruta_md = os.path.join(_DOCS_DIR, f"{slug}.md")
    if not os.path.isfile(ruta_md):
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    with open(ruta_md, "r", encoding="utf-8") as f:
        contenido_md = f.read()

    contenido_html = markdown.markdown(contenido_md, extensions=["tables", "fenced_code"])

    return templates.TemplateResponse("doc.html", {
        "request": request,
        "contenido_html": contenido_html,
    })
