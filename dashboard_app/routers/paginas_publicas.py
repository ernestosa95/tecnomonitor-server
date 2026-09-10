"""
Páginas públicas de marketing/herramientas y el formulario de leads.
Sin autenticación, sin estado compartido con el resto del dashboard.

Primer router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md
(paso 1: bajo riesgo, sirve para validar el mecanismo de extracción).
"""
import csv
import os

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, JSONResponse

from core import limiter, templates

router = APIRouter()


@router.get("/herramientas")
async def get_herramientas(request: Request):
    return templates.TemplateResponse("herramientas.html", {"request": request})

@router.get("/ris-analytics")
async def get_ris_analytics(request: Request):
    return templates.TemplateResponse("solucion2.html", {"request": request})

@router.get("/prov-analytics")
async def prov_analytics(request: Request):
    return templates.TemplateResponse("vista_provincias_prototipo.html", {"request": request})

@router.get("/hl7-analytics")
async def get_hl7_analytics(request: Request):
    return templates.TemplateResponse("solucion1.html", {"request": request})

@router.get("/pacs-capacity")
async def get_pacs_capacity(request: Request):
    return templates.TemplateResponse("solucion3.html", {"request": request})

@router.get("/salta-project")
def landing_salta():
    return FileResponse("dashboard_app/templates/solucion4.html")

@router.get("/renovacion")
async def renovacion(request: Request):
    return templates.TemplateResponse("calculadora_local.html", {"request": request})

# --- Landing temporal para testeo de gerencia (acceso público, SIN login) ---
@router.get("/demo-pacs")
def landing_demo_pacs():
    return FileResponse("dashboard_app/templates/demo-pacs.html")

@router.get("/tecno-solution")
async def get_tecno_solutions(request: Request):
    return templates.TemplateResponse("links.html", {"request": request})


def _sanear_campo_csv(valor: str) -> str:
    """
    Antepone una comilla simple si el campo empieza con =, +, -, @, tab o CR,
    para que Excel/Sheets no lo interprete como fórmula al abrir el CSV
    (CSV/Formula Injection, CWE-1236). Ver docs/04-seguridad.md#s3.
    """
    if valor and valor[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + valor
    return valor


@router.post("/submit-lead")
@limiter.limit("10/minute")
async def handle_form(
    request: Request,
    nombre_apellido: str = Form(...),
    institucion: str = Form(...),
    cargo: str = Form(...),
    provincia: str = Form(...),
    volumen_estudios: str = Form(...),
    desafio_principal: str = Form(...),
    preferencia_contacto: str = Form(...),
    interes_poc: str = Form(...)
):
    file_path = "leads_evento_links.csv"
    file_exists = os.path.isfile(file_path)

    # Definimos los encabezados según tus requerimientos
    headers = [
        "Nombre y Apellido", "Institución", "Cargo", "Provincia",
        "Volumen Estudios", "Desafío Principal", "Preferencia Contacto", "Interés POC"
    ]

    try:
        with open(file_path, mode="a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            # Si el archivo es nuevo, escribimos la cabecera
            if not file_exists:
                writer.writerow(headers)

            # Escribimos los datos del usuario (saneados contra formula injection)
            writer.writerow([_sanear_campo_csv(v) for v in [
                nombre_apellido, institucion, cargo, provincia,
                volumen_estudios, desafio_principal, preferencia_contacto, interes_poc
            ]])

        return JSONResponse(content={"status": "success", "message": "Datos guardados correctamente"})

    except Exception as e:
        return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)
