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
import json
import os
import re
import tempfile
import time
from datetime import datetime, timedelta

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Table, TableStyle
from sqlalchemy import text
from sqlalchemy.orm import Session

import asana_conector
import auth
import database
import generator_report
from core import get_db, limiter
from database import AlertaModel, HistorialReportes, HospitalMetadata
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

def generar_reporte_infra_pdf(req, db):
    hospital = db.query(HospitalMetadata).filter_by(hospital_id=req.hospital_id).first()
    nombre_hosp = hospital.nombre if hospital else "Hospital Desconocido"

    def parse_f(s): return datetime.strptime(s, "%Y-%m-%d")
    f_ini = parse_f(req.fecha_desde)
    f_fin = parse_f(req.fecha_hasta) + timedelta(days=1)

    query = text("""
        SELECT timestamp, host_cpu_usage, host_ram_usage, full_json_data
        FROM reportes_historicos
        WHERE hospital_id = :hid AND timestamp BETWEEN :f1 AND :f2
        ORDER BY timestamp ASC
    """)
    result = db.execute(query, {"hid": req.hospital_id, "f1": f_ini, "f2": f_fin}).fetchall()

    if not result:
        return {"error": "No hay datos para el periodo"}

    # --- PROCESAMIENTO DE KPIs ---
    metrics_host = {"cpu": [], "ram": []}
    for row in result:
        data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
        tele = (data.get("physical_layer") or {}).get("telemetry") or {}
        cpu_p = tele.get("cpu", {}).get("usage_percent")
        ram_p = tele.get("ram", {}).get("usage_percent")
        if cpu_p is not None: metrics_host["cpu"].append(cpu_p)
        if ram_p is not None: metrics_host["ram"].append(ram_p)

    ultimo_json = json.loads(result[-1].full_json_data) if isinstance(result[-1].full_json_data, str) else result[-1].full_json_data
    phy = ultimo_json.get("physical_layer") or {}
    vms_raw = ultimo_json.get("virtual_layer") or []

    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    ancho, alto = A4

    # --- HELPER LOCAL: Encabezado y pie de página ---
    def _encabezado(titulo_hoja, num_pagina):
        c.setStrokeColorRGB(0.6, 0.6, 0.6)
        c.setFillColorRGB(0.9, 0.9, 0.9)
        c.roundRect(40, alto - 60, 45, 25, 4, fill=1, stroke=1)
        c.setFillColorRGB(0.17, 0.24, 0.31)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(45, alto - 53, req.hospital_id)
        c.drawString(95, alto - 53, nombre_hosp[:40])
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, alto - 85, titulo_hoja)
        c.setFont("Helvetica-Bold", 14)
        c.setFillColorRGB(0.16, 0.5, 0.72)
        c.drawString(ancho / 2 - 60, 30, "TECNOIMAGEN")
        c.setFont("Helvetica", 9)
        c.setFillColorRGB(0.5, 0.5, 0.5)
        c.drawString(ancho - 100, 30, f"Página {num_pagina}")
        return alto - 130

    # --- PÁGINA 1 ---
    pos_y = _encabezado("REPORTE DE SALUD DE INFRAESTRUCTURA (IT)", 1)

    # Bloque KPIs
    c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setFillColorRGB(0.98, 0.98, 0.98)
    c.roundRect(40, pos_y - 70, ancho - 80, 70, 10, fill=1, stroke=1)

    def draw_kpi(label, val, x, y, color=(0,0,0)):
        c.setFillColorRGB(0.5, 0.5, 0.5); c.setFont("Helvetica", 8); c.drawString(x, y, label)
        c.setFillColorRGB(*color); c.setFont("Helvetica-Bold", 14); c.drawString(x, y - 18, val)

    uptime_pct = min(100.0, (len(result) / ((f_fin - f_ini).total_seconds() / 60)) * 100)
    draw_kpi("UPTIME ESTIMADO", f"{round(uptime_pct, 2)}%", 60, pos_y - 25, (0.15, 0.68, 0.37))
    draw_kpi("AVG CPU HOST", f"{round(np.mean(metrics_host['cpu']), 1) if metrics_host['cpu'] else 'N/A'}%", 210, pos_y - 25)
    draw_kpi("AVG RAM HOST", f"{round(np.mean(metrics_host['ram']), 1) if metrics_host['ram'] else 'N/A'}%", 360, pos_y - 25)
    pos_y -= 95

    # Gráfico Suavizado
    c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, pos_y, "ESTADO DE SENSORES Y EVOLUCIÓN TÉRMICA")
    pos_y -= 10

    img_temp = generar_grafico_temperaturas_infra(result)
    if img_temp:
        c.drawImage(ImageReader(img_temp), 35, pos_y - 210, width=ancho-70, height=210, mask='auto')
        pos_y -= 230

    # Tabla Sensores y RAID
    if phy:
        # Sensores
        temps = phy.get("sensors", {}).get("temperatures", [])
        if temps:
            data_t = [["Sensor", "Valor Actual", "Estado"]] + [[t.get("name")[:40], f"{t.get('value')} {t.get('unit')}", t.get("status")] for t in temps[:4]]
            t = Table(data_t, colWidths=[200, 80, 80])
            t.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),8), ('GRID',(0,0),(-1,-1),0.5,colors.grey), ('BACKGROUND',(0,0),(-1,0),colors.whitesmoke)]))
            tw, th = t.wrap(0,0); t.drawOn(c, 40, pos_y - th); pos_y -= (th + 20)

        # RAID (Añadido)
        vols = phy.get("storage_layer", {}).get("logical_volumes", [])
        if vols:
            c.setFont("Helvetica-Bold", 10); c.drawString(40, pos_y, "Almacenamiento Físico (RAID)")
            pos_y -= 12
            data_v = [["Volumen", "RAID", "Tamaño", "Estado"]] + [[v.get("name"), v.get("raid_level"), f"{v.get('size_gb')} GB", v.get("status")] for v in vols]
            t_v = Table(data_v, colWidths=[120, 100, 70, 70])
            t_v.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.5,colors.grey)]))
            tw, th = t_v.wrap(0,0); t_v.drawOn(c, 40, pos_y - th); pos_y -= (th + 25)

    # Referencias al pie
    pos_ref = 80
    c.setDash(1, 2); c.setStrokeColorRGB(0.7, 0.7, 0.7); c.line(40, pos_ref + 15, ancho - 40, pos_ref + 15); c.setDash()
    c.setFillColorRGB(0.4, 0.4, 0.4); c.setFont("Helvetica-BoldOblique", 8); c.drawString(40, pos_ref, "REFERENCIAS TÉCNICAS:")
    glosario = [
        ("• Uptime Estimado:", "Disponibilidad del agente basada en el conteo de reportes de telemetría recibidos."),
        ("• AVG CPU / RAM:", "Carga promedio de procesamiento y memoria del servidor físico durante el período."),
        ("• Sensores / RAID:", "Estado de salud del hardware capturado en el último reporte válido enviado.")
    ]
    gy = pos_ref - 12
    for tit, des in glosario:
        c.setFont("Helvetica-Bold", 7); c.drawString(40, gy, tit)
        c.setFont("Helvetica", 7); c.drawString(120, gy, des); gy -= 10

    # --- PÁGINA 2 ---
    c.showPage()
    pos_y = _encabezado("DETALLE DE CAPA VIRTUAL E INCIDENTES", 2)

    if vms_raw:
        c.setFont("Helvetica-Bold", 11); c.drawString(40, pos_y, "RECURSOS POR MÁQUINA VIRTUAL")
        pos_y -= 20
        for vm in vms_raw:
            if pos_y < 150: # Salto de página
                c.showPage(); pos_y = _encabezado("DETALLE CAPA VIRTUAL (CONT.)", 3)

            c.setFont("Helvetica-Bold", 9); c.setFillColorRGB(0.2, 0.4, 0.6)
            c.drawString(40, pos_y, f"■ {vm.get('id')} - Estado: {vm.get('state')}")
            pos_y -= 15

            # Discos
            discos = vm.get("storage", [])
            if discos:
                data_d = [["Disco", "Uso %", "Libre"]] + [[d.get("mount_point"), f"{d.get('usage_percent')}%", f"{d.get('free_gb')} GB"] for d in discos]
                t_d = Table(data_d, colWidths=[80, 50, 80])
                t_d.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.2,colors.grey)]))
                tw, th = t_d.wrap(0,0); t_d.drawOn(c, 60, pos_y - th); pos_y -= (th + 15)

    # Tabla Incidentes
    alertas = db.query(AlertaModel).filter(AlertaModel.hospital_id == req.hospital_id, AlertaModel.start_time >= f_ini).all()
    if alertas:
        c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(0.7, 0.1, 0.1)
        c.drawString(40, pos_y - 10, "HISTORIAL DE INCIDENTES RELEVANTES"); pos_y -= 30
        data_a = [["Inicio", "Tipo", "Mensaje", "Estado"]] + [[a.start_time.strftime("%d/%m %H:%M"), a.tipo[:15], a.mensaje[:65], "OK"] for a in alertas[:12]]
        t_a = Table(data_a, colWidths=[70, 110, 285, 50])
        t_a.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.5,colors.grey), ('VALIGN',(0,0),(-1,-1),'MIDDLE'), ('TEXTCOLOR',(0,1),(-1,-1),colors.darkred)]))
        tw, th = t_a.wrap(0,0); t_a.drawOn(c, 40, pos_y - th)

    c.save()

    # Manejo de Historial y Asana (Como lo tenías en el archivo original)
    filename = f"Infra_{req.hospital_id}_{req.fecha_desde}.pdf"
    pdf_bytes = buffer.getvalue()
    asana_url = asana_conector.adjuntar_pdf_a_tarea(req.asana_task_id, pdf_bytes, filename)

    nuevo_reg = HistorialReportes(hospital_id=req.hospital_id, tipo_reporte="Infraestructura IT", fecha_desde=req.fecha_desde, fecha_hasta=req.fecha_hasta,
                                  estado="Completado" if asana_url else "Descargado", asana_url=asana_url)
    db.add(nuevo_reg); db.commit()

    if asana_url: return {"status": "success", "asana_url": asana_url}
    buffer.seek(0)
    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": f"attachment; filename={filename}"})

def generar_grafico_temperaturas_infra(result):
    if not result: return None

    # --- PASO 1: Recolección de datos en estructura plana ---
    # Usamos una lista de tuplas (timestamp, nombre_sensor, valor)
    # para evitar cualquier desincronización entre fechas y arrays de sensores.
    registros_planos = []

    for row in result:
        try:
            ts = row.timestamp
            if isinstance(ts, str):
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
                    try:
                        ts = datetime.strptime(ts, fmt)
                        break
                    except:
                        continue

            # Si después del parsing ts sigue siendo string, saltamos este registro
            if isinstance(ts, str):
                continue

            data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
            if not data:
                continue

            phy = data.get("physical_layer") or {}
            sensors = phy.get("sensors") or {}
            temps = sensors.get("temperatures") or []

            for t in temps:
                name = t.get("name") or "Desc"
                val = t.get("value")
                # Solo registramos si el valor es numérico válido
                if val is not None:
                    try:
                        registros_planos.append((ts, name, float(val)))
                    except (TypeError, ValueError):
                        continue
        except:
            continue

    if not registros_planos:
        return None

    # --- PASO 2: Pivot — construimos una serie temporal por sensor ---
    # Recolectamos todos los timestamps únicos (ordenados) y todos los sensores únicos.
    todos_ts = sorted(set(r[0] for r in registros_planos))

    if len(todos_ts) < 5:
        return None

    todos_sensores = sorted(set(r[1] for r in registros_planos))

    # Índice rápido: {(ts, sensor): valor}
    indice = {(r[0], r[1]): r[2] for r in registros_planos}

    # Para cada sensor construimos un array del mismo largo que todos_ts,
    # rellenando con NaN donde no hay dato. Esto garantiza len(fechas) == len(y).
    data_sensores = {}
    for sname in todos_sensores:
        data_sensores[sname] = [
            indice.get((ts, sname), float('nan')) for ts in todos_ts
        ]

    if not data_sensores:
        return None

    # --- PASO 3: Dibujo con suavizado ---
    window_size = min(12, max(1, len(todos_ts) // 20))  # Adaptativo según cantidad de puntos

    fig, ax = plt.subplots(figsize=(11, 4))

    for sname, valores in data_sensores.items():
        y = np.array(valores, dtype=float)

        # Interpolación de NaNs internos (no extrapolamos extremos)
        indices_validos = np.where(~np.isnan(y))[0]
        if len(indices_validos) < 2:
            continue  # Sensor con casi sin datos, no lo graficamos

        # Interpolamos solo los huecos internos
        y_interp = np.copy(y)
        y_interp[np.isnan(y_interp)] = np.interp(
            np.where(np.isnan(y_interp))[0],
            indices_validos,
            y[indices_validos]
        )

        # Suavizado con ventana adaptativa
        kernel = np.ones(window_size) / window_size
        y_smooth = np.convolve(y_interp, kernel, mode='same')

        # Verificación de seguridad: ambas dimensiones deben coincidir
        if len(todos_ts) != len(y_smooth):
            # Fallback: graficamos sin suavizado
            y_smooth = y_interp

        ax.plot(todos_ts, y_smooth, label=sname, linewidth=1.5, alpha=0.8)

    # Si ningún sensor pudo graficarse, cerramos y retornamos None
    if not ax.lines:
        plt.close(fig)
        return None

    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d/%m %H:%M'))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator())
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, -0.15), ncol=3, fontsize=8, frameon=False)
    ax.grid(True, linestyle='--', alpha=0.3)
    plt.xticks(rotation=15, fontsize=8)
    plt.tight_layout()

    img_buf = io.BytesIO()
    fig.savefig(img_buf, format='png', dpi=130)
    plt.close(fig)
    img_buf.seek(0)
    return img_buf

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
