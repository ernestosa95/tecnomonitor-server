"""
Generación de reportes/informes PDF (clínico + infraestructura), gráficos
matplotlib usados en esos PDFs, historial de reportes generados, el reporte
RIS Analytics corporativo, y el selector de usuarios responsables de alertas.

Octavo router extraído de dashboard.py -- el más pesado de todos, candidato a
que en un pase posterior se empuje más lógica hacia generator_report.py (que
ya existe como módulo separado) en vez de duplicar esa responsabilidad entre
dos archivos. Ver docs/08-plan-refactor-dashboard.md.
"""
import io
import os
import re
import tempfile
import time

import matplotlib.pyplot as plt
import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import database
import generator_report
from core import get_db, limiter
from database import HistorialReportes
from schemas import DatosRISAnalytics

router = APIRouter()

ESTADOS_COLORS = {
    'Citados': '#cce5ff',
    'Admitidos': '#99ccff',
    'Ejecutados': '#66b2ff',
    'Asociados': '#3399ff',
    'Borradores': '#0080ff',
    'Definitivos': '#0066cc',
    'Suspendidos': '#004c99',
    'Almacenados': '#1abc9c'  # Verde/Teal para PACS
}


class ReportePDFRequest(BaseModel):
    hospital_id: str
    fecha_desde: str
    fecha_hasta: str
    alcance: str
    asana_task_id: str
    tipo_reporte: str = "clinico"


# ==========================================
# --- MOTOR DE GENERACIÓN DE PDF ---
# ==========================================

# Paleta de colores oficial del frontend
COLORS_CHART = ['#004c99', '#0066cc', '#0080ff', '#3399ff', '#66b2ff', '#99ccff', '#cce5ff']

def generar_grafico_dona(datos: dict):
    """ Dibuja el gráfico en memoria y devuelve la imagen lista """
    labels = list(datos.keys())
    sizes = list(datos.values())
    total = sum(sizes)

    if total == 0:
        labels, sizes = ["Sin Datos"], [1]
        colores = ['#ecf0f1']
    else:
        colores = COLORS_CHART[:len(labels)]

    fig, ax = plt.subplots(figsize=(3, 3), subplot_kw=dict(aspect="equal"))

    # Dibujar Dona
    wedges, texts = ax.pie(sizes, colors=colores, wedgeprops=dict(width=0.3, edgecolor='white', linewidth=2))

    # Texto Central
    ax.text(0, 0.15, "TOTAL", ha='center', va='center', fontsize=10, color='#7f8c8d', fontweight='bold')
    ax.text(0, -0.15, f"{total:,}".replace(',', '.'), ha='center', va='center', fontsize=20, fontweight='bold', color='#2c3e50')

    # Guardar en memoria RAM
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf, total, colores

def generar_grafico_temporal(datos_equipo):
    """ Dibuja barras apiladas (RIS) vs barras simples (PACS) por tiempo """
    labels = sorted(list(datos_equipo.keys()))
    if not labels:
        return None

    x = np.arange(len(labels))
    width = 0.35  # Ancho de las barras

    # AJUSTE: Más margen inferior (bottom) para que entren las fechas largas
    fig, ax = plt.subplots(figsize=(8, 3.5))
    fig.subplots_adjust(bottom=0.45)

    bottom_ris = np.zeros(len(labels))

    # 1. Dibujar barras apiladas de RIS
    estados_ris = ['Citados', 'Admitidos', 'Ejecutados', 'Asociados', 'Borradores', 'Definitivos', 'Suspendidos']
    for estado in estados_ris:
        valores = [datos_equipo[l].get(estado.lower(), 0) for l in labels]
        if sum(valores) > 0:
            ax.bar(x - width/2, valores, width, bottom=bottom_ris, color=ESTADOS_COLORS[estado], label=estado)
            bottom_ris += np.array(valores)

    # 2. Dibujar barra de PACS
    valores_pacs = [datos_equipo[l].get('almacenados', 0) for l in labels]
    if sum(valores_pacs) > 0:
        ax.bar(x + width/2, valores_pacs, width, color=ESTADOS_COLORS['Almacenados'], label='Almacenados')

    # Estética del gráfico
    ax.set_xticks(x)
    # AJUSTE: Fuente más chica (6) y rotación de 60 grados
    ax.set_xticklabels(labels, rotation=60, ha='right', fontsize=6)
    ax.grid(axis='y', linestyle='--', alpha=0.7)

    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.25), ncol=4, fontsize=8, frameon=False)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf

@router.post("/api/informes/pdf")
@limiter.limit("5/minute")
def generar_reporte_pdf(request: Request,
                        req: ReportePDFRequest,
                        db: Session = Depends(get_db),
                        current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial"))):

    # 1. Delegar a la nueva lógica separada
    if req.tipo_reporte == "infra":
        result = generator_report.generar_pdf_infra(req, db)
    else:
        result = generator_report.generar_pdf_clinico(req, db)

    # 2. Manejo de errores controlados
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])

    # 3. Respuesta según resultado de Asana
    pdf_bytes = result["pdf_bytes"]
    filename = result["filename"]
    asana_url = result["asana_url"]

    if asana_url:
        return {
            "status": "success",
            "asana_url": asana_url,
            "message": "Adjuntado a Asana correctamente"
        }
    else:
        # Descarga forzada local en caso de error en Asana
        buffer_pdf = io.BytesIO(pdf_bytes)
        buffer_pdf.seek(0)
        return StreamingResponse(
            buffer_pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

@router.get("/api/informes/historial")
def obtener_historial_reportes(db: Session = Depends(get_db),
                               # CORRECCIÓN: Restringimos los roles explícitamente
                               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial"))):
    # Traemos los últimos 15 reportes generados
    historial = db.query(HistorialReportes).order_by(HistorialReportes.fecha_generacion.desc()).limit(15).all()

    resultados = []
    for h in historial:
        resultados.append({
            "id": h.id,
            "hospital_id": h.hospital_id,
            "tipo_reporte": h.tipo_reporte,
            "periodo": f"{h.fecha_desde} ➔ {h.fecha_hasta}",
            "fecha_generacion": h.fecha_generacion.strftime("%d/%m/%Y %H:%M"),
            "estado": h.estado,
            "asana_url": h.asana_url
        })
    return resultados


@router.post("/v1/generar-reporte-ris")
@limiter.limit("5/minute")
async def api_generar_reporte_ris(
    request: Request,
    datos: DatosRISAnalytics,
    background_tasks: BackgroundTasks,
    # Puedes descomentar la siguiente linea si quieres que solo usuarios logueados lo usen:
    # user: dict = Depends(auth.get_current_user)
):
    """
    Endpoint para recibir la estadística de solucion2.html (RIS Analytics)
    y devolver un PDF con el formato core de TecnoMonitor.
    """
    try:
        # 1. Crear nombre de archivo seguro
        nombre_limpio = re.sub(r'[^\w\s-]', '', datos.hospital_name).strip().replace(' ', '_')
        timestamp = int(time.time())
        filename = f"Reporte_RIS_{nombre_limpio}_{timestamp}.pdf"

        # 2. Crear archivo temporal
        temp_dir = tempfile.gettempdir()
        ruta_pdf = os.path.join(temp_dir, filename)

        # 3. Llamar a la nueva función del motor (debes agregarla a generator_report.py)
        # Usamos model_dump() para Pydantic v2
        generator_report.generar_reporte_ris_corporativo(datos.model_dump(), ruta_pdf)

        # 4. Programar borrado del temporal tras el envío
        background_tasks.add_task(os.remove, ruta_pdf)

        # 5. Retornar archivo
        return FileResponse(
            path=ruta_pdf,
            filename=filename,
            media_type='application/pdf'
        )

    except Exception as e:
        print(f"Error generando reporte RIS: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/api/users/responsables")
def listar_usuarios_responsables(db: Session = Depends(get_db),
                                 current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    """Devuelve la lista de usuarios activos para el selector de responsables de alertas."""
    usuarios = db.query(database.UserModel).filter(database.UserModel.is_active == True).all()

    resultados = []
    for u in usuarios:
        resultados.append({
            "email": u.email,
            "nombre": u.full_name or u.email,
            "tiene_asana": bool(u.asana_id) # Para mostrar un aviso si elegimos a alguien sin Asana ID
        })
    return resultados
