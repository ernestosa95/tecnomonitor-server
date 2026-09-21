"""
Páginas públicas de marketing/herramientas y el formulario de leads.
Sin autenticación, sin estado compartido con el resto del dashboard.

Primer router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md
(paso 1: bajo riesgo, sirve para validar el mecanismo de extracción).
"""
import csv
import logging
import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

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

@router.get("/proyectos-provincias")
async def proyectos_provincias(request: Request):
    return templates.TemplateResponse("proyectos_provincias.html", {"request": request})

@router.get("/salta-project")
def landing_salta():
    return FileResponse("dashboard_app/templates/solucion4.html")

@router.get("/mendoza-project")
def landing_mendoza():
    return FileResponse("dashboard_app/templates/mendoza_project.html")

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


# --- Leads de la landing /demo-pacs (un CSV propio, una fila por formulario enviado) ---
logger = logging.getLogger(__name__)

LEADS_DEMO_PACS_CSV = "leads_demo_pacs.csv"
_LEADS_DEMO_PACS_HEADERS = [
    "Fecha", "Nombre y Apellido", "Institución", "Cargo", "Volumen Estudios",
    "Email", "Teléfono", "Plan de Interés", "Origen",
]
# Tope de largo por campo: el endpoint es público y el CSV crece sin cota (docs/04-seguridad.md#s3).
_LEADS_DEMO_PACS_MAX = {
    "nombre_apellido": 120, "institucion": 150, "cargo": 60, "volumen_estudios": 60,
    "email": 254, "telefono": 30, "plan_interes": 120, "origen": 20,
}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_TELEFONO_RE = re.compile(r"^[+(]?[0-9][0-9\s().-]{5,}$")  # admite "(011) 4123-4567"
_TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")


@router.post("/submit-lead-demo-pacs")
@limiter.limit("10/minute")
async def submit_lead_demo_pacs(
    request: Request,
    nombre_apellido: str = Form(...),
    institucion: str = Form(...),
    cargo: str = Form(...),
    email: str = Form(...),
    telefono: str = Form(...),
    volumen_estudios: str = Form(""),
    plan_interes: str = Form(""),
    origen: str = Form("formulario"),
):
    campos = {
        "nombre_apellido": nombre_apellido, "institucion": institucion, "cargo": cargo,
        "volumen_estudios": volumen_estudios, "email": email, "telefono": telefono,
        "plan_interes": plan_interes, "origen": origen,
    }
    campos = {k: " ".join(v.split()) for k, v in campos.items()}

    for nombre in ("nombre_apellido", "institucion", "cargo", "email", "telefono"):
        if not campos[nombre]:
            return JSONResponse(
                content={"status": "error", "message": "Complete todos los campos obligatorios."},
                status_code=400,
            )
    if any(len(v) > _LEADS_DEMO_PACS_MAX[k] for k, v in campos.items()):
        return JSONResponse(
            content={"status": "error", "message": "Alguno de los datos ingresados es demasiado largo."},
            status_code=400,
        )
    if not _EMAIL_RE.match(campos["email"]):
        return JSONResponse(
            content={"status": "error", "message": "Ingrese un correo electrónico válido."},
            status_code=400,
        )
    if not _TELEFONO_RE.match(campos["telefono"]):
        return JSONResponse(
            content={"status": "error", "message": "Ingrese un teléfono válido (solo números, +, espacios, guiones o paréntesis)."},
            status_code=400,
        )

    # El teléfono se guarda solo con dígitos (sin +, espacios ni guiones): queda uniforme para
    # Excel y no dispara el saneado antifórmula. 15 dígitos es el máximo de un número internacional
    # (E.164) y lo que Excel conserva sin perder precisión.
    solo_digitos = re.sub(r"\D", "", campos["telefono"])
    if not 6 <= len(solo_digitos) <= 15:
        return JSONResponse(
            content={"status": "error", "message": "Ingrese un teléfono válido (entre 6 y 15 dígitos; puede usar +, espacios, guiones o paréntesis)."},
            status_code=400,
        )
    campos["telefono"] = solo_digitos

    fila = [datetime.now(_TZ_AR).strftime("%Y-%m-%d %H:%M:%S")] + [
        campos[k] for k in (
            "nombre_apellido", "institucion", "cargo", "volumen_estudios",
            "email", "telefono", "plan_interes", "origen",
        )
    ]

    try:
        # Sin await entre el chequeo del archivo y la escritura: en el event loop de un
        # único proceso uvicorn dos requests no se pueden intercalar acá.
        file_exists = os.path.isfile(LEADS_DEMO_PACS_CSV)
        # utf-8-sig: el BOM hace que Excel abra bien las tildes (Python no lo repite en modo append).
        # Separador ";" porque el Excel en español (es-AR) lo usa como separador de lista: con "," abriría
        # el archivo con todo en una sola columna.
        with open(LEADS_DEMO_PACS_CSV, mode="a", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f, delimiter=";")
            if not file_exists:
                writer.writerow(_LEADS_DEMO_PACS_HEADERS)
            writer.writerow([_sanear_campo_csv(v) for v in fila])
    except Exception:
        logger.exception("No se pudo guardar el lead de /demo-pacs")
        return JSONResponse(
            content={"status": "error", "message": "No pudimos guardar sus datos. Intente nuevamente en unos minutos."},
            status_code=500,
        )

    return JSONResponse(content={"status": "success", "message": "Datos guardados correctamente"})
