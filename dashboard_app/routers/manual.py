"""
Sirve la documentación técnica del repo (docs/*.md) como HTML, autenticado
igual que los runbooks (mismo patrón, generalizado a un listado + una
página por doc). No usa `/docs` a propósito -- FastAPI ya sirve el Swagger
en esa ruta -- por eso esta va en `/manual`.

Dos carpetas, dos secciones en el listado: `docs/*.md` (documentación
técnica del server) y `docs/guias/*.md` (guías/tutoriales pensadas para
alguien nuevo, ver docs/17 y docs/18). No incluye `docs/runbooks/` (tiene
su propia ruta en routers/runbooks.py, pensada para pegarse en un ticket
de Asana) ni `README.md` (es el índice del propio repo, no un documento
para leer acá).
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
_GUIAS_DIR = os.path.join(_DOCS_DIR, "guias")
_SLUG_VALIDO = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _listar(directorio):
    """[{"slug", "titulo"}], en el orden en que aparecen los archivos (el
    prefijo numérico del nombre ya los deja en orden de lectura)."""
    items = []
    if not os.path.isdir(directorio):
        return items
    for nombre in sorted(os.listdir(directorio)):
        if not nombre.endswith(".md") or nombre.upper() == "README.MD":
            continue
        slug = nombre[:-3].lower()
        if not _SLUG_VALIDO.match(slug):
            continue
        titulo = slug
        try:
            with open(os.path.join(directorio, nombre), "r", encoding="utf-8") as f:
                for linea in f:
                    if linea.startswith("# "):
                        titulo = linea[2:].strip()
                        break
        except OSError:
            pass
        items.append({"slug": slug, "titulo": titulo})
    return items


def _ruta_md(slug):
    """Busca el slug en docs/ y, si no está, en docs/guias/. None si no existe en ninguna."""
    for directorio in (_DOCS_DIR, _GUIAS_DIR):
        ruta = os.path.join(directorio, f"{slug}.md")
        if os.path.isfile(ruta):
            return ruta
    return None


@router.get("/manual")
def listar_manual(request: Request,
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    return templates.TemplateResponse("manual_index.html", {
        "request": request,
        "docs_server": _listar(_DOCS_DIR),
        "guias": _listar(_GUIAS_DIR),
    })


@router.get("/manual/{full_path:path}")
def ver_doc(full_path: str, request: Request,
            current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    # Los docs se linkean entre sí con la ruta relativa real del archivo (ej.
    # "08-plan-refactor-dashboard.md", o "guias/18-....md" desde uno de afuera de esa
    # carpeta) -- así funcionan también mirando el repo crudo en GitHub/VS Code. Acá el
    # namespace es plano (un slug puede vivir en docs/ o en docs/guias/, ver _ruta_md),
    # así que basta con el nombre de archivo sin carpeta ni extensión: se admite
    # "/manual/08-plan-refactor-dashboard", "/manual/08-plan-refactor-dashboard.md" y
    # "/manual/guias/18-....md" por igual.
    slug = full_path.rsplit("/", 1)[-1]
    if slug.endswith(".md"):
        slug = slug[:-3]
    slug = slug.lower()

    # Slug estricto (solo minúsculas/números/guiones): evita path traversal
    # y cualquier intento de leer un archivo fuera de docs/.
    if not _SLUG_VALIDO.match(slug):
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    ruta_md = _ruta_md(slug)
    if not ruta_md:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    with open(ruta_md, "r", encoding="utf-8") as f:
        contenido_md = f.read()

    # "toc" no agrega tabla de contenidos visible (no ponemos [TOC] en ningún doc) --
    # lo único que aprovechamos es que le pone id="..." a cada h1-h6, así los links
    # con ancla (#seccion) entre docs funcionan también acá, no solo en GitHub.
    contenido_html = markdown.markdown(contenido_md, extensions=["tables", "fenced_code", "toc"])

    return templates.TemplateResponse("doc.html", {
        "request": request,
        "contenido_html": contenido_html,
    })
