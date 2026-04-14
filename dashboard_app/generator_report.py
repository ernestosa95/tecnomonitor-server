import io
import json
import numpy as np
from datetime import datetime, timedelta
import matplotlib
matplotlib.use('Agg') # Crucial para servidores: dibuja sin abrir ventanas
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle
from reportlab.lib.utils import ImageReader
from sqlalchemy import text
from sqlalchemy.orm import Session
from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib import colors
import matplotlib.pyplot as plt
import io
import matplotlib.ticker as ticker

import database
from database import HospitalMetadata, HistorialReportes, AlertaModel
import asana_conector
import os
from datetime import datetime

import tempfile
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse
# Importamos el esquema (asegurate de haberlo agregado a schemas.py en la raiz)
from schemas import DatosRISAnalytics

# Paletas de colores oficiales extraídas del dashboard
ESTADOS_COLORS = {
    'Citados': '#cce5ff', 'Admitidos': '#99ccff', 'Ejecutados': '#66b2ff',
    'Asociados': '#3399ff', 'Borradores': '#0080ff', 'Definitivos': '#0066cc',
    'Suspendidos': '#004c99', 'Almacenados': '#1abc9c'
}

COLORS_CHART = ['#004c99', '#0066cc', '#0080ff', '#3399ff', '#66b2ff', '#99ccff', '#cce5ff']

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def crear_portada(c, ancho, alto, tipo, hospital_id, hospital_nombre, fecha_desde, fecha_hasta):
    """Genera una portada moderna con curvas y una imagen de fondo según el tipo de reporte"""
    
    # --- HELPER: Formatear fecha de YYYY-MM-DD a DD/MM/YYYY ---
    def formatear_fecha(fecha_str):
        try:
            return datetime.strptime(str(fecha_str)[:10], "%Y-%m-%d").strftime("%d/%m/%Y")
        except:
            return str(fecha_str) # Fallback por si viene en otro formato

    texto_periodo = f"{formatear_fecha(fecha_desde)} al {formatear_fecha(fecha_hasta)}"

    # 1. Configurar textos e imágenes según tipo
    if tipo == 'clinico':
        img_name = "imagen_informe_medica.png"
        titulo_principal = "REPORTE DE"
        titulo_secundario = "USO CLÍNICO"
    elif tipo == 'estadisticas':
        img_name = "imagen_informe_estadisticas.png"
        titulo_principal = "INFORME DE"
        titulo_secundario = "ESTADÍSTICAS"
    else:
        img_name = "imagen_informe_server.png"
        titulo_principal = "REPORTE DE"
        titulo_secundario = "INFRAESTRUCTURA IT"

    # Construimos la ruta ABSOLUTA uniendo el directorio del script con el nombre de la imagen
    img_path = os.path.join(BASE_DIR, img_name)

    # 2. Imagen de fondo (cubriendo el 80% inferior de la página)
    try:
        if os.path.exists(img_path):
            c.drawImage(img_path, 0, 0, width=ancho, height=alto * 0.8, preserveAspectRatio=False)
        else:
            print(f"⚠️ ATENCIÓN: No se encontró la imagen de portada en: {img_path}")
            c.setFillColorRGB(0.9, 0.9, 0.9)
            c.rect(0, 0, ancho, alto * 0.8, fill=1, stroke=0)
    except Exception as e:
        print(f"⚠️ Error al cargar la imagen de portada: {e}")
        c.setFillColorRGB(0.9, 0.9, 0.9)
        c.rect(0, 0, ancho, alto * 0.8, fill=1, stroke=0)

    # 3. Forma Blanca superior (Curva que baja en diagonal revelando la imagen)
    c.setFillColorRGB(1, 1, 1)
    path_blanco = c.beginPath()
    path_blanco.moveTo(0, alto)
    path_blanco.lineTo(ancho, alto)
    path_blanco.lineTo(ancho, alto * 0.65)
    path_blanco.curveTo(ancho * 0.6, alto * 0.35, ancho * 0.2, alto * 0.45, 0, alto * 0.5)
    path_blanco.close()
    c.drawPath(path_blanco, fill=1, stroke=0)

    # 4. Ondas Azules Inferiores (Estilo Tecnoimagen)
    c.setFillColorRGB(0.0, 0.45, 0.70) # Azul corporativo
    path_azul1 = c.beginPath()
    path_azul1.moveTo(ancho, 0)
    path_azul1.lineTo(0, 0)
    path_azul1.lineTo(0, alto * 0.15)
    path_azul1.curveTo(ancho * 0.4, alto * 0.05, ancho * 0.7, alto * 0.35, ancho, alto * 0.6)
    path_azul1.close()
    c.drawPath(path_azul1, fill=1, stroke=0)

    c.setFillColorRGB(0.0, 0.65, 0.85) # Azul claro
    path_azul2 = c.beginPath()
    path_azul2.moveTo(ancho, 0)
    path_azul2.lineTo(ancho * 0.4, 0)
    path_azul2.curveTo(ancho * 0.7, alto * 0.1, ancho * 0.85, alto * 0.25, ancho, alto * 0.45)
    path_azul2.close()
    c.drawPath(path_azul2, fill=1, stroke=0)

    # 5. Textos de la Portada
    # Logo
    c.setFillColorRGB(0.1, 0.1, 0.1)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(ancho - 140, alto - 50, "TECNOIMAGEN")

    # Títulos principales
    c.setFont("Helvetica", 28)
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(50, alto - 120, titulo_principal)
    
    c.setFont("Helvetica-Bold", 34)
    c.setFillColorRGB(0.0, 0.45, 0.70)
    c.drawString(50, alto - 155, titulo_secundario)
    
    # ---> FECHAS MÁS CHICAS Y SIN BOLD <---
    c.setFont("Helvetica", 16)               # Fuente normal y tamaño más chico
    c.setFillColorRGB(0.3, 0.3, 0.3)         # Gris un poco más suave
    c.drawString(50, alto - 185, texto_periodo) # Subimos un poquito la altura (a -185) para que quede bien agrupado

    # Información del Hospital
    c.setFont("Helvetica-Bold", 12)
    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.drawString(50, alto - 290, f"HOSPITAL: {hospital_id}")
    
    c.setFont("Helvetica", 10)
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.drawString(50, alto - 305, hospital_nombre[:60].upper())

    c.showPage()

def crear_encabezado(c, ancho, alto, titulo_hoja, hospital_id, hospital_nombre, num_pagina):
    """Genera una cabecera unificada y moderna para todas las páginas internas"""
    
    # 1. Ondas superiores (Fondo de cabecera)
    # Onda Azul Claro
    c.setFillColorRGB(0.0, 0.65, 0.85)
    path1 = c.beginPath()
    path1.moveTo(0, alto)
    path1.lineTo(ancho, alto)
    path1.lineTo(ancho, alto - 35)
    path1.curveTo(ancho * 0.6, alto - 45, ancho * 0.4, alto - 15, 0, alto - 25)
    path1.close()
    c.drawPath(path1, fill=1, stroke=0)

    # Onda Azul Oscuro Corporativo
    c.setFillColorRGB(0.0, 0.45, 0.70)
    path2 = c.beginPath()
    path2.moveTo(0, alto)
    path2.lineTo(ancho, alto)
    path2.lineTo(ancho, alto - 40)
    path2.curveTo(ancho * 0.7, alto - 15, ancho * 0.3, alto - 50, 0, alto - 40)
    path2.close()
    c.drawPath(path2, fill=1, stroke=0)

    # 2. Textos dentro de la franja (Blancos)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 12)
    # Etiqueta de Hospital
    c.drawString(40, alto - 22, f"HOSPITAL: {hospital_id}")
    # Logo Texto derecha
    c.drawString(ancho - 130, alto - 22, "TECNOIMAGEN")

    # 3. Textos debajo de la franja
    # Nombre del hospital
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.setFont("Helvetica", 10)
    c.drawString(40, alto - 60, hospital_nombre[:70].upper())

    # Título de la página
    c.setFillColorRGB(0.17, 0.24, 0.31) # Azul noche oscuro
    c.setFont("Helvetica-Bold", 16)
    c.drawString(40, alto - 80, titulo_hoja.upper())

    # 4. Línea separadora sutil
    c.setStrokeColorRGB(0.85, 0.85, 0.85)
    c.setLineWidth(1)
    c.line(40, alto - 95, ancho - 40, alto - 95)

    # 5. Pie de página unificado
    c.setFillColorRGB(0.5, 0.5, 0.5)
    c.setFont("Helvetica", 9)
    c.drawString(40, 30, "Monitoreo Inteligente de Infraestructura")
    c.drawString(ancho - 80, 30, f"Página {num_pagina}")

    # Retornamos la posición Y donde el resto del código debe empezar a dibujar contenido
    return alto - 125

def crear_hoja_cierre_clinico(c, ancho, alto):
    """Genera una página final explicativa con el glosario de términos para perfiles no técnicos."""
    c.showPage()  # Forzamos una página nueva
    
    # Fondo base blanco
    c.setFillColorRGB(1, 1, 1)
    c.rect(0, 0, ancho, alto, fill=1, stroke=0)
    
    # 1. Ondas Azules Inferiores (MÁS SUTILES PARA NO TAPAR TEXTO)
    c.setFillColorRGB(0.0, 0.45, 0.70) # Azul corporativo
    path_azul1 = c.beginPath()
    path_azul1.moveTo(ancho, 0)
    path_azul1.lineTo(0, 0)
    path_azul1.lineTo(0, alto * 0.05) # Arranca bien bajito a la izquierda
    # La curva sube solo hasta el 20% del alto total en la derecha
    path_azul1.curveTo(ancho * 0.4, alto * 0.02, ancho * 0.7, alto * 0.15, ancho, alto * 0.20)
    path_azul1.close()
    c.drawPath(path_azul1, fill=1, stroke=0)

    c.setFillColorRGB(0.0, 0.65, 0.85) # Azul claro
    path_azul2 = c.beginPath()
    path_azul2.moveTo(ancho, 0)
    path_azul2.lineTo(ancho * 0.4, 0)
    # Sube solo hasta el 15% del alto total en la derecha
    path_azul2.curveTo(ancho * 0.7, alto * 0.05, ancho * 0.85, alto * 0.10, ancho, alto * 0.15)
    path_azul2.close()
    c.drawPath(path_azul2, fill=1, stroke=0)

    # 2. Título y Encabezado de la página
    c.setFillColorRGB(0.17, 0.24, 0.31)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(50, alto - 80, "GUÍA DE LECTURA DEL REPORTE")
    
    c.setStrokeColorRGB(0.0, 0.45, 0.70)
    c.setLineWidth(2)
    c.line(50, alto - 95, 200, alto - 95)
    
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.setFont("Helvetica", 11)
    texto_intro = "Este reporte está diseñado para ofrecer una visión gerencial del flujo de trabajo del servicio."
    c.drawString(50, alto - 125, texto_intro)

    # 3. SECCIÓN 1: MÉTRICAS RIS
    pos_y = alto - 170
    c.setFillColorRGB(0.0, 0.45, 0.70)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, pos_y, "1. Flujo de Pacientes e Informes (RIS)")
    
    pos_y -= 30
    glosario_ris = [
        ("Citados", "Turnos otorgados y agendados en el sistema para el período."),
        ("Admitidos", "Pacientes que se presentaron en la recepción y fueron ingresados."),
        ("Ejecutados", "Estudios realizados físicamente por el técnico en la sala médica."),
        ("Asociados", "Imágenes vinculadas a la orden, listas para ser diagnosticadas por el médico."),
        ("Borradores", "Informes médicos iniciados o dictados, pero aún pendientes de firma."),
        ("Definitivos", "Informes médicos firmados digitalmente, cerrados y listos para entrega.")
    ]
    
    for termino, definicion in glosario_ris:
        c.setFillColorRGB(0.2, 0.2, 0.2)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(60, pos_y, f"• {termino}:")
        c.setFillColorRGB(0.4, 0.4, 0.4)
        c.setFont("Helvetica", 11)
        c.drawString(145, pos_y, definicion)
        pos_y -= 25

    # 4. SECCIÓN 2: MÉTRICAS PACS
    pos_y -= 15
    c.setFillColorRGB(0.0, 0.45, 0.70)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, pos_y, "2. Archivo Digital de Imágenes (PACS)")
    pos_y -= 30
    
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(60, pos_y, "• Almacenados:")
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.setFont("Helvetica", 11)
    c.drawString(160, pos_y, "Cantidad de estudios que llegaron exitosamente a los servidores.")
    
    # 5. SECCIÓN 3: INTERPRETACIÓN DE GRÁFICOS
    pos_y -= 50
    c.setFillColorRGB(0.0, 0.45, 0.70)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, pos_y, "3. Interpretación Visual")
    pos_y -= 30
    
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(60, pos_y, "• Gráficos de Anillo:")
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.setFont("Helvetica", 11)
    c.drawString(185, pos_y, "Muestran el volumen total y qué porcentaje de carga absorbe cada equipo.")
    pos_y -= 25
    
    c.setFillColorRGB(0.2, 0.2, 0.2)
    c.setFont("Helvetica-Bold", 11)
    c.drawString(60, pos_y, "• Gráficos de Barras:")
    c.setFillColorRGB(0.4, 0.4, 0.4)
    c.setFont("Helvetica", 11)
    c.drawString(185, pos_y, "Reflejan el comportamiento en el tiempo para detectar picos o caídas en el servicio.")

    # 6. Logo y Firma en la onda inferior
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 16)
    # También bajé la altura en Y a 30 para que el texto quede centrado dentro de la ola nueva
    c.drawString(ancho - 140, 30, "TECNOIMAGEN")
    
# ==========================================
# MOTORES GRÁFICOS (Matplotlib)
# ==========================================

def generar_grafico_dona(datos: dict):
    labels = list(datos.keys())
    sizes = list(datos.values())
    total = sum(sizes)
    
    if total == 0:
        labels, sizes = ["Sin Datos"], [1]
        colores = ['#ecf0f1']
    else:
        colores = COLORS_CHART[:len(labels)]

    fig, ax = plt.subplots(figsize=(3, 3), subplot_kw=dict(aspect="equal"))
    wedges, texts = ax.pie(sizes, colors=colores, wedgeprops=dict(width=0.3, edgecolor='white', linewidth=2))
    
    ax.text(0, 0.15, "TOTAL", ha='center', va='center', fontsize=10, color='#7f8c8d', fontweight='bold')
    ax.text(0, -0.15, f"{total:,}".replace(',', '.'), ha='center', va='center', fontsize=20, fontweight='bold', color='#2c3e50')
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf, total, colores

def generar_grafico_temporal(datos_equipo):
    labels = sorted(list(datos_equipo.keys()))
    if not labels:
        return None
        
    x = np.arange(len(labels))
    width = 0.35

    fig, ax = plt.subplots(figsize=(8, 3.5))
    fig.subplots_adjust(bottom=0.45) 
    
    bottom_ris = np.zeros(len(labels))
    
    estados_ris = ['Citados', 'Admitidos', 'Ejecutados', 'Asociados', 'Borradores', 'Definitivos', 'Suspendidos']
    for estado in estados_ris:
        valores = [datos_equipo[l].get(estado.lower(), 0) for l in labels]
        if sum(valores) > 0:
            ax.bar(x - width/2, valores, width, bottom=bottom_ris, color=ESTADOS_COLORS[estado], label=estado)
            bottom_ris += np.array(valores)

    valores_pacs = [datos_equipo[l].get('almacenados', 0) for l in labels]
    if sum(valores_pacs) > 0:
        ax.bar(x + width/2, valores_pacs, width, color=ESTADOS_COLORS['Almacenados'], label='Almacenados')

    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=60, ha='right', fontsize=6) 
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.25), ncol=4, fontsize=8, frameon=False)

    buf = io.BytesIO()
    plt.savefig(buf, format='png', bbox_inches='tight', transparent=True, dpi=300)
    buf.seek(0)
    plt.close(fig)
    return buf

def generar_grafico_temperaturas_infra(result):
    if not result: return None
    
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
            if isinstance(ts, str):
                continue

            data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
            if not data: continue
                
            phy = data.get("physical_layer") or {}
            sensors = phy.get("sensors") or {}
            temps = sensors.get("temperatures") or []
            
            for t in temps:
                name = t.get("name") or "Desc"
                val = t.get("value")
                if val is not None:
                    try: registros_planos.append((ts, name, float(val)))
                    except (TypeError, ValueError): continue
        except:
            continue

    if not registros_planos: return None

    todos_ts = sorted(set(r[0] for r in registros_planos))
    if len(todos_ts) < 5: return None

    todos_sensores = sorted(set(r[1] for r in registros_planos))
    indice = {(r[0], r[1]): r[2] for r in registros_planos}
    
    data_sensores = {}
    for sname in todos_sensores:
        data_sensores[sname] = [indice.get((ts, sname), float('nan')) for ts in todos_ts]

    if not data_sensores: return None

    window_size = min(12, max(1, len(todos_ts) // 20))
    fig, ax = plt.subplots(figsize=(11, 4))
    
    for sname, valores in data_sensores.items():
        y = np.array(valores, dtype=float)
        indices_validos = np.where(~np.isnan(y))[0]
        if len(indices_validos) < 2: continue
        
        y_interp = np.copy(y)
        y_interp[np.isnan(y_interp)] = np.interp(np.where(np.isnan(y_interp))[0], indices_validos, y[indices_validos])
        
        # Suavizado con ventana adaptativa (corregido para bordes)
        kernel = np.ones(window_size) / window_size
        
        # Calculamos cuánto padding necesita cada lado según la ventana
        pad_size = window_size // 2
        
        # Rellenamos los extremos repitiendo el primer/último valor (mode='edge')
        y_padded = np.pad(y_interp, (pad_size, window_size - 1 - pad_size), mode='edge')
        
        # Hacemos la convolución en modo 'valid' para que recorte el relleno extra
        y_smooth = np.convolve(y_padded, kernel, mode='valid')
        
        if len(todos_ts) != len(y_smooth): y_smooth = y_interp
        ax.plot(todos_ts, y_smooth, label=sname, linewidth=1.5, alpha=0.8)
    
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

# ==========================================
# GENERADORES DE REPORTES PDF
# ==========================================

def generar_pdf_clinico(req, db: Session):
    """Genera el reporte clínico de RIS/PACS"""
    hospital = db.query(HospitalMetadata).filter_by(hospital_id=req.hospital_id).first()
    nombre_hosp = hospital.nombre if hospital else "Hospital Desconocido"

    def parsear_fecha(fecha_str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"):
            try: return datetime.strptime(fecha_str, fmt)
            except ValueError: continue
        raise ValueError("Formato desconocido")

    try:
        f_desde = parsear_fecha(req.fecha_desde)
        f_hasta = parsear_fecha(req.fecha_hasta) + timedelta(days=1)
    except Exception as e:
        return {"error": f"Formato de fecha inválido. Recibimos: {req.fecha_desde}"}

    f_desde_sql = f_desde - timedelta(days=3)
    query = text("SELECT timestamp, kpi_json_data FROM reportes_uso WHERE hospital_id = :hid AND timestamp >= :f1")
    result = db.execute(query, {"hid": req.hospital_id, "f1": f_desde_sql}).fetchall()

    datos_ris, datos_pacs, datos_temporales = {}, {}, {}
    EXCLUDED_AETS = ['CLIENT', 'WADO', 'PACS']
    EXCLUDED_MODS = ['DOC']
    diccionario_aet = {}
    agrupar_por_mes = (f_hasta - f_desde).days > 45

    for row in result:
        metrics = json.loads(row.kpi_json_data) if row.kpi_json_data else {}
        fecha_extraccion_str = metrics.get("start_time_extraction")
        try:
            if fecha_extraccion_str: fecha_evento = datetime.fromisoformat(fecha_extraccion_str).replace(tzinfo=None)
            else: fecha_evento = (datetime.strptime(str(row.timestamp)[:19], "%Y-%m-%d %H:%M:%S") if isinstance(row.timestamp, str) else row.timestamp).replace(tzinfo=None)
        except: continue

        if f_desde <= fecha_evento < f_hasta:
            k_tiempo = fecha_evento.strftime("%Y-%m") if agrupar_por_mes else fecha_evento.strftime("%Y-%m-%d")
            
            for item in metrics.get("ris", []):
                eq = item.get("equipo"); aet = item.get("aet"); mod = item.get("mod", "")
                if aet and eq: diccionario_aet[aet] = eq
                nombre_final_ris = eq or aet or "Desc"
                
                if nombre_final_ris not in EXCLUDED_AETS and aet not in EXCLUDED_AETS and mod not in EXCLUDED_MODS:
                    if nombre_final_ris not in datos_temporales: datos_temporales[nombre_final_ris] = {}
                    if k_tiempo not in datos_temporales[nombre_final_ris]: datos_temporales[nombre_final_ris][k_tiempo] = {}
                    
                    val = item.get("totales", 0)
                    if val == 0: val = sum([item.get(k, 0) for k in ["citados", "admitidos", "ejecutados", "con_imagen", "borradores", "definitivos", "suspendidos"]])
                    
                    if val > 0: datos_ris[nombre_final_ris] = datos_ris.get(nombre_final_ris, 0) + val
                        
                    for st in ["citados", "admitidos", "ejecutados", "con_imagen", "borradores", "definitivos", "suspendidos"]:
                        val_st = item.get(st, 0)
                        if val_st > 0:
                            key_st = 'asociados' if st == 'con_imagen' else st
                            datos_temporales[nombre_final_ris][k_tiempo][key_st] = datos_temporales[nombre_final_ris][k_tiempo].get(key_st, 0) + val_st
            
            for item in metrics.get("pacs", []):
                aet = item.get("aet") or "Desc"; mod = item.get("mod", "")
                nombre_final_pacs = diccionario_aet.get(aet, aet)
                
                if aet not in EXCLUDED_AETS and nombre_final_pacs not in EXCLUDED_AETS and mod not in EXCLUDED_MODS:
                    if nombre_final_pacs not in datos_temporales: datos_temporales[nombre_final_pacs] = {}
                    if k_tiempo not in datos_temporales[nombre_final_pacs]: datos_temporales[nombre_final_pacs][k_tiempo] = {}
                    
                    val = item.get("almacenados", 0)
                    if val > 0:
                        datos_pacs[nombre_final_pacs] = datos_pacs.get(nombre_final_pacs, 0) + val
                        datos_temporales[nombre_final_pacs][k_tiempo]['almacenados'] = datos_temporales[nombre_final_pacs][k_tiempo].get('almacenados', 0) + val

    datos_ris = dict(sorted(datos_ris.items(), key=lambda x: x[1], reverse=True))
    datos_pacs = dict(sorted(datos_pacs.items(), key=lambda x: x[1], reverse=True))

    todos_los_tiempos = set()
    for eq in datos_temporales: todos_los_tiempos.update(datos_temporales[eq].keys())
    todos_los_tiempos = sorted(list(todos_los_tiempos))
    
    columnas_posibles = ['citados', 'admitidos', 'ejecutados', 'asociados', 'borradores', 'definitivos', 'suspendidos', 'almacenados']
    cols_activas_globales = []
    for col in columnas_posibles:
        if any(datos_temporales[eq].get(t, {}).get(col, 0) > 0 for eq in datos_temporales for t in datos_temporales[eq]):
            cols_activas_globales.append(col)
            
    if not cols_activas_globales: cols_activas_globales = ['citados', 'ejecutados', 'almacenados']
            
    for eq in datos_temporales:
        for t in todos_los_tiempos:
            if t not in datos_temporales[eq]: datos_temporales[eq][t] = {col: 0 for col in cols_activas_globales}
            else:
                for col in cols_activas_globales:
                    if col not in datos_temporales[eq][t]: datos_temporales[eq][t][col] = 0

    # ---> CÁLCULO DE EVOLUCIÓN TOTAL <---
    datos_temporales_total = {}
    for t in todos_los_tiempos:
        datos_temporales_total[t] = {col: 0 for col in cols_activas_globales}
        for eq in datos_temporales:
            for col in cols_activas_globales:
                datos_temporales_total[t][col] += datos_temporales[eq][t].get(col, 0)

    buffer_pdf = io.BytesIO()
    c = canvas.Canvas(buffer_pdf, pagesize=A4)
    ancho, alto = A4

    def _encabezado(titulo_hoja, num_pagina):
        # Redirigimos la lógica a nuestro nuevo creador de cabeceras global
        return crear_encabezado(c, ancho, alto, titulo_hoja, req.hospital_id, nombre_hosp, num_pagina)

    def _caja_totales(titulo, datos_dict, pos_y):
        if not datos_dict: return pos_y
        img_buf, total, colores = generar_grafico_dona(datos_dict)
        data_tabla = [["", "Equipo", "Cantidad", "%"]]
        filas = []
        for i, (eq, cant) in enumerate(datos_dict.items()):
            pct = f"{(cant / total * 100):.1f}%" if total > 0 else "0%"
            color_hex = colores[i] if i < len(colores) else '#bdc3c7'
            data_tabla.append(["", eq[:22], f"{cant:,}".replace(',', '.'), pct])
            filas.append(color_hex)

        tbl = Table(data_tabla, colWidths=[15, 145, 55, 35])
        tbl.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#ecf0f1')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.HexColor('#2c3e50')),
            ('ALIGN', (2, 0), (-1, -1), 'RIGHT'), ('ALIGN', (0, 0), (0, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')),
            ('FONTSIZE', (0, 0), (-1, -1), 6), ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ]))
        tw, th = tbl.wrap(0, 0)
        alto_caja = max(200, th + 60)

        c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setFillColorRGB(0.98, 0.98, 0.98)
        c.roundRect(40, pos_y - alto_caja, ancho - 80, alto_caja, 10, fill=1, stroke=1)
        c.setFillColorRGB(0.1, 0.1, 0.1); c.setFont("Helvetica-Bold", 11)
        c.drawString(55, pos_y - 25, titulo)
        c.drawImage(ImageReader(img_buf), 50, pos_y - alto_caja + (alto_caja / 2) - 75, width=150, height=150, mask='auto')
        pos_tabla_y = pos_y - 45 - th
        tbl.drawOn(c, 230, pos_tabla_y)
        if len(data_tabla) > 1:
            alto_fila = th / len(data_tabla)
            for i, color_hex in enumerate(filas):
                y_circulo = pos_tabla_y + th - (alto_fila * (1.5 + i))
                c.setFillColor(colors.HexColor(color_hex)); c.setStrokeColor(colors.HexColor(color_hex))
                c.circle(240, y_circulo, 3, fill=1, stroke=0)
        return pos_y - alto_caja - 20

    # ----------------------------------------------------
    # DIBUJO DEL PDF - ORDEN DE PÁGINAS
    # ----------------------------------------------------
    
    # 1. Página de Portada
    crear_portada(c, ancho, alto, 'clinico', req.hospital_id, nombre_hosp, req.fecha_desde, req.fecha_hasta)

    # 2. Inicia el contenido (Página 2 en adelante)
    pagina_actual = 2
    pos_y_actual = _encabezado("INFORME DE GESTIÓN DE EQUIPOS MÉDICOS", pagina_actual)

    if req.alcance in ['total', 'ris'] and datos_ris: pos_y_actual = _caja_totales("ÓRDENES RIS POR EQUIPO (Órdenes Creadas)", datos_ris, pos_y_actual)
    if req.alcance in ['total', 'pacs'] and datos_pacs: pos_y_actual = _caja_totales("ESTUDIOS PACS POR EQUIPO (Estudios Almacenados)", datos_pacs, pos_y_actual)

    # ---> NUEVO: DIBUJAR GRÁFICO Y TABLA DEL TOTAL <---
    if datos_temporales_total:
        img_buf_total = generar_grafico_temporal(datos_temporales_total)
        if img_buf_total:
            headers = ['Período'] + [col.capitalize() for col in cols_activas_globales]
            data_tabla = [headers]
            for tiempo in todos_los_tiempos:
                fila = [tiempo]
                for col in cols_activas_globales:
                    val = datos_temporales_total[tiempo].get(col, 0)
                    fila.append(f"{val:,}".replace(',', '.'))
                data_tabla.append(fila)

            # Agregar fila de TOTAL
            fila_total = ['TOTAL']
            for col in cols_activas_globales:
                suma_col = sum(datos_temporales_total[t].get(col, 0) for t in todos_los_tiempos)
                fila_total.append(f"{suma_col:,}".replace(',', '.'))
            data_tabla.append(fila_total)

            ancho_col_base = (ancho - 120) / (len(cols_activas_globales) + 1.5)
            anchos = [ancho_col_base * 1.5] + [ancho_col_base] * len(cols_activas_globales)
            tbl = Table(data_tabla, colWidths=anchos)

            estilos = [
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')), ('FONTSIZE', (0, 0), (-1, -1), 6),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]
            for idx_col, col_name in enumerate(cols_activas_globales):
                color_hex = ESTADOS_COLORS.get(col_name.capitalize(), '#cccccc')
                estilos.append(('BACKGROUND', (idx_col + 1, 0), (idx_col + 1, 0), colors.HexColor(color_hex)))
            
            estilos.append(('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#ecf0f1')))
            estilos.append(('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'))
            estilos.append(('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f2f4f4')))
            
            tbl.setStyle(TableStyle(estilos))

            tw, th = tbl.wrap(ancho - 80, 200)
            alto_caja = 190 + th + 60

            if (pos_y_actual - alto_caja) < 50:
                c.showPage()
                pagina_actual += 1
                pos_y_actual = _encabezado("INFORME DE GESTIÓN DE EQUIPOS MÉDICOS", pagina_actual)

            c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setFillColorRGB(0.98, 0.98, 0.98)
            c.roundRect(40, pos_y_actual - alto_caja, ancho - 80, alto_caja, 10, fill=1, stroke=1)
            c.setFillColorRGB(0.1, 0.1, 0.1); c.setFont("Helvetica-Bold", 12)
            c.drawString(55, pos_y_actual - 25, "EVOLUCIÓN COMBINADA - RESUMEN GENERAL")
            c.drawImage(ImageReader(img_buf_total), 40, pos_y_actual - 225, width=ancho - 80, height=190, mask='auto')
            tbl.drawOn(c, 50, pos_y_actual - alto_caja + 15)
            pos_y_actual -= (alto_caja + 20)

    equipos_a_graficar = list(datos_temporales.keys())

    if equipos_a_graficar:
        # Forzamos salto de página para empezar el desglose por equipos limpio
        c.showPage()
        pagina_actual += 1
        pos_y_actual = _encabezado("EVOLUCIÓN TEMPORAL POR EQUIPO", pagina_actual)

        for equipo in equipos_a_graficar:
            cols_activas = cols_activas_globales
            img_buf = generar_grafico_temporal(datos_temporales[equipo])
            if not img_buf: continue

            headers = ['Período'] + [col.capitalize() for col in cols_activas]
            data_tabla = [headers]
            for tiempo in todos_los_tiempos:
                fila = [tiempo]
                for col in cols_activas:
                    val = datos_temporales[equipo][tiempo].get(col, 0)
                    fila.append(f"{val:,}".replace(',', '.'))
                data_tabla.append(fila)

            # Agregar fila de TOTAL
            fila_total = ['TOTAL']
            for col in cols_activas:
                suma_col = sum(datos_temporales[equipo][t].get(col, 0) for t in todos_los_tiempos)
                fila_total.append(f"{suma_col:,}".replace(',', '.'))
            data_tabla.append(fila_total)

            ancho_col_base = (ancho - 120) / (len(cols_activas) + 1.5)
            anchos = [ancho_col_base * 1.5] + [ancho_col_base] * len(cols_activas)
            tbl = Table(data_tabla, colWidths=anchos)

            estilos = [
                ('ALIGN', (1, 0), (-1, -1), 'CENTER'), ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
                ('TOPPADDING', (0, 0), (-1, -1), 1), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
                ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#bdc3c7')), ('FONTSIZE', (0, 0), (-1, -1), 6),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]
            for idx_col, col_name in enumerate(cols_activas):
                color_hex = ESTADOS_COLORS.get(col_name.capitalize(), '#cccccc')
                estilos.append(('BACKGROUND', (idx_col + 1, 0), (idx_col + 1, 0), colors.HexColor(color_hex)))
            
            estilos.append(('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#ecf0f1')))
            estilos.append(('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'))
            estilos.append(('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#f2f4f4')))
            
            tbl.setStyle(TableStyle(estilos))

            tw, th = tbl.wrap(ancho - 80, 200)
            alto_caja = 190 + th + 60

            if (pos_y_actual - alto_caja) < 50:
                c.showPage()
                pagina_actual += 1
                pos_y_actual = _encabezado("EVOLUCIÓN TEMPORAL POR EQUIPO", pagina_actual)

            c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setFillColorRGB(0.98, 0.98, 0.98)
            c.roundRect(40, pos_y_actual - alto_caja, ancho - 80, alto_caja, 10, fill=1, stroke=1)
            c.setFillColorRGB(0.1, 0.1, 0.1); c.setFont("Helvetica-Bold", 12)
            c.drawString(55, pos_y_actual - 25, f"EVOLUCIÓN COMBINADA - {equipo}")
            c.drawImage(ImageReader(img_buf), 40, pos_y_actual - 225, width=ancho - 80, height=190, mask='auto')
            tbl.drawOn(c, 50, pos_y_actual - alto_caja + 15)
            pos_y_actual -= (alto_caja + 20)
    
    crear_hoja_cierre_clinico(c, ancho, alto)

    c.save()

    filename = f"Reporte_TM_{req.hospital_id}_{req.fecha_desde}.pdf"
    pdf_bytes = buffer_pdf.getvalue()

    # Procesar lógica de Asana e Historial
    asana_url = asana_conector.adjuntar_pdf_a_tarea(req.asana_task_id, pdf_bytes, filename)
    nuevo_registro = HistorialReportes(
        hospital_id=req.hospital_id, tipo_reporte="PDF Completo",
        fecha_desde=req.fecha_desde, fecha_hasta=req.fecha_hasta,
        estado="Completado" if asana_url else "Descargado", asana_url=asana_url
    )
    db.add(nuevo_registro)
    db.commit()

    return {"pdf_bytes": pdf_bytes, "filename": filename, "asana_url": asana_url}

def generar_pdf_infra(req, db: Session):
    """Genera el reporte de Infraestructura IT"""
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

    def _encabezado(titulo_hoja, num_pagina):
        # Redirigimos la lógica a nuestro nuevo creador de cabeceras global
        return crear_encabezado(c, ancho, alto, titulo_hoja, req.hospital_id, nombre_hosp, num_pagina)

    # ----------------------------------------------------
    # DIBUJO DEL PDF - ORDEN DE PÁGINAS
    # ----------------------------------------------------
    
    # 1. Página de Portada
    crear_portada(c, ancho, alto, 'infra', req.hospital_id, nombre_hosp, req.fecha_desde, req.fecha_hasta)

    # 2. Inicia el contenido (Página 2)
    pagina_actual = 2
    pos_y = _encabezado("REPORTE DE SALUD DE INFRAESTRUCTURA (IT)", pagina_actual)
    c.setStrokeColorRGB(0.8, 0.8, 0.8); c.setFillColorRGB(0.98, 0.98, 0.98)
    c.roundRect(40, pos_y - 70, ancho - 80, 70, 10, fill=1, stroke=1)
    
    def draw_kpi(label, val, x, y, color=(0,0,0)):
        c.setFillColorRGB(0.5, 0.5, 0.5); c.setFont("Helvetica", 8); c.drawString(x, y, label)
        c.setFillColorRGB(*color); c.setFont("Helvetica-Bold", 14); c.drawString(x, y - 18, val)

    segundos_totales = (f_fin - f_ini).total_seconds()
    reportes_esperados = segundos_totales / 600 # Se espera minimo un reporte cada 10 minutos
    uptime_pct = min(100.0, (len(result) / reportes_esperados * 100))

    draw_kpi("UPTIME ESTIMADO", f"{round(uptime_pct, 2)}%", 60, pos_y - 25, (0.15, 0.68, 0.37))
    draw_kpi("AVG CPU HOST", f"{round(np.mean(metrics_host['cpu']), 1) if metrics_host['cpu'] else 'N/A'}%", 210, pos_y - 25)
    draw_kpi("AVG RAM HOST", f"{round(np.mean(metrics_host['ram']), 1) if metrics_host['ram'] else 'N/A'}%", 360, pos_y - 25)
    pos_y -= 95

    c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(0.1, 0.1, 0.1)
    c.drawString(40, pos_y, "ESTADO DE SENSORES Y EVOLUCIÓN TÉRMICA")
    pos_y -= 10
    
    img_temp = generar_grafico_temperaturas_infra(result)
    if img_temp:
        c.drawImage(ImageReader(img_temp), 35, pos_y - 210, width=ancho-70, height=210, mask='auto')
        pos_y -= 230

    # --- CÁLCULO HISTÓRICO DE SENSORES TÉRMICOS ---
    temp_stats = {}
    for row in result:
        row_data = json.loads(row.full_json_data) if isinstance(row.full_json_data, str) else row.full_json_data
        if not row_data: continue
        
        temps_list = row_data.get("physical_layer", {}).get("sensors", {}).get("temperatures", [])
        for t in temps_list:
            name = t.get("name", "Desc")
            val = t.get("value")
            unit = t.get("unit", "C")
            
            if val is not None:
                try:
                    val_float = float(val)
                    if name not in temp_stats:
                        temp_stats[name] = {"values": [], "unit": unit}
                    temp_stats[name]["values"].append(val_float)
                except (TypeError, ValueError):
                    continue

    if temp_stats:
        data_t = [["Sensor", "Temperatura Promedio", "Temperatura Máxima"]]
        
        for name, stats in temp_stats.items():
            vals = stats["values"]
            if vals:
                avg_val = sum(vals) / len(vals)
                max_val = max(vals)
                unit = stats["unit"].strip()
                data_t.append([
                    name[:40], 
                    f"{avg_val:.1f} {unit}", 
                    f"{max_val:.1f} {unit}"
                ])
        
        if len(data_t) > 1:
            # Calculamos el ancho total respetando los márgenes (40 izq + 40 der = 80)
            total_width = ancho - 80
            # Distribuimos el ancho: 50% para el nombre, 25% y 25% para los valores
            col_widths = [total_width * 0.5, total_width * 0.25, total_width * 0.25]
            
            t = Table(data_t, colWidths=col_widths)
            t.setStyle(TableStyle([
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'), # Negrita para la cabecera
                ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
                ('BACKGROUND', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (1,0), (-1,-1), 'CENTER'),           # Centramos los números
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE')
            ]))
            
            tw, th = t.wrap(0,0)
            t.drawOn(c, 40, pos_y - th)
            pos_y -= (th + 20)

        vols = phy.get("storage_layer", {}).get("logical_volumes", [])
        if vols:
            c.setFont("Helvetica-Bold", 10)
            c.drawString(40, pos_y, "Almacenamiento Físico (RAID)")
            pos_y -= 15
            
            data_v = [["Volumen", "RAID", "Tamaño", "Estado"]] + [[v.get("name"), v.get("raid_level"), f"{v.get('size_gb')} GB", v.get("status")] for v in vols]
            
            # Repartimos el ancho disponible (ancho - 80) en proporciones lógicas
            ancho_disponible = ancho - 80
            anchos_raid = [ancho_disponible * 0.40, ancho_disponible * 0.25, ancho_disponible * 0.20, ancho_disponible * 0.15]
            
            t_v = Table(data_v, colWidths=anchos_raid)
            t_v.setStyle(TableStyle([
                ('FONTSIZE', (0,0), (-1,-1), 8),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),    # Cabecera en negrita
                ('BACKGROUND', (0,0), (-1,0), colors.whitesmoke), # Fondo gris claro para cabecera
                ('GRID', (0,0), (-1,-1), 0.5, colors.grey),
                ('ALIGN', (2,0), (-1,-1), 'CENTER'),              # Centramos el tamaño y el estado
                ('VALIGN', (0,0), (-1,-1), 'MIDDLE')
            ]))
            
            tw, th = t_v.wrap(0,0)
            t_v.drawOn(c, 40, pos_y - th)
            pos_y -= (th + 25)

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

    # 3. Siguiente página (Página 3)
    c.showPage()
    pagina_actual += 1
    pos_y = _encabezado("DETALLE DE CAPA VIRTUAL E INCIDENTES", pagina_actual)

    if vms_raw:
        c.setFont("Helvetica-Bold", 11); c.drawString(40, pos_y, "RECURSOS POR MÁQUINA VIRTUAL")
        pos_y -= 20
        for vm in vms_raw:
            if pos_y < 150: 
                c.showPage()
                pagina_actual += 1
                pos_y = _encabezado("DETALLE CAPA VIRTUAL (CONT.)", pagina_actual)
            
            c.setFont("Helvetica-Bold", 9); c.setFillColorRGB(0.2, 0.4, 0.6)
            c.drawString(40, pos_y, f"■ {vm.get('id')} - Estado: {vm.get('state')}")
            pos_y -= 15
            
            discos = vm.get("storage", [])
            if discos:
                data_d = [["Disco", "Uso %", "Libre"]] + [[d.get("mount_point"), f"{d.get('usage_percent')}%", f"{d.get('free_gb')} GB"] for d in discos]
                t_d = Table(data_d, colWidths=[80, 50, 80])
                t_d.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.2,colors.grey)]))
                tw, th = t_d.wrap(0,0); t_d.drawOn(c, 60, pos_y - th); pos_y -= (th + 15)

    alertas = db.query(AlertaModel).filter(AlertaModel.hospital_id == req.hospital_id, AlertaModel.start_time >= f_ini).all()
    if alertas:
        c.setFont("Helvetica-Bold", 11); c.setFillColorRGB(0.7, 0.1, 0.1)
        c.drawString(40, pos_y - 10, "HISTORIAL DE INCIDENTES RELEVANTES"); pos_y -= 30
        data_a = [["Inicio", "Tipo", "Mensaje", "Estado"]] + [[a.start_time.strftime("%d/%m %H:%M"), a.tipo[:15], a.mensaje[:65], "OK"] for a in alertas[:12]]
        t_a = Table(data_a, colWidths=[70, 110, 285, 50])
        t_a.setStyle(TableStyle([('FONTSIZE',(0,0),(-1,-1),7), ('GRID',(0,0),(-1,-1),0.5,colors.grey), ('VALIGN',(0,0),(-1,-1),'MIDDLE'), ('TEXTCOLOR',(0,1),(-1,-1),colors.darkred)]))
        tw, th = t_a.wrap(0,0); t_a.drawOn(c, 40, pos_y - th)

    c.save()
    
    filename = f"Infra_{req.hospital_id}_{req.fecha_desde}.pdf"
    pdf_bytes = buffer.getvalue()
    asana_url = asana_conector.adjuntar_pdf_a_tarea(req.asana_task_id, pdf_bytes, filename)
    
    nuevo_reg = HistorialReportes(hospital_id=req.hospital_id, tipo_reporte="Infraestructura IT", fecha_desde=req.fecha_desde, fecha_hasta=req.fecha_hasta, estado="Completado" if asana_url else "Descargado", asana_url=asana_url)
    db.add(nuevo_reg); db.commit()

    return {"pdf_bytes": pdf_bytes, "filename": filename, "asana_url": asana_url}


# Agregado informe de RIS
def generar_grafico_barras_memoria(datos_dict, titulo, color_barra='#2c3e50'):
    """Genera un gráfico Matplotlib en buffer de memoria (para no guardar imagen en disco)"""
    plt.figure(figsize=(6, 3))
    nombres = list(datos_dict.keys())
    valores = list(datos_dict.values())
    
    plt.bar(nombres, valores, color=color_barra)
    plt.title(titulo)
    plt.ylabel("Cantidad")
    plt.xticks(rotation=15, ha='right')
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300)
    buf.seek(0)
    plt.close()
    return buf

def generar_grafico_torta_sexo(datos_dict):
    """Genera un gráfico tipo dona/torta para el sexo"""
    if not datos_dict: return None
    plt.figure(figsize=(4, 3))
    
    labels = list(datos_dict.keys())
    sizes = list(datos_dict.values())
    
    # Asignar colores (Azul para M, Rosa para F, Gris para Otros)
    colores = []
    for l in labels:
        if str(l).upper().startswith('M'): colores.append('#3399ff')
        elif str(l).upper().startswith('F'): colores.append('#ff66b2')
        else: colores.append('#bdc3c7')

    plt.pie(sizes, labels=labels, colors=colores, autopct='%1.1f%%', startangle=90, wedgeprops=dict(width=0.4, edgecolor='white'))
    plt.title("Distribución por Sexo")
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300)
    buf.seek(0)
    plt.close()
    return buf

def generar_grafico_piramide_memoria(datos_piramide):
    """Genera una pirámide poblacional enfrentando M y F"""
    labels = datos_piramide.get('labels', [])
    M = datos_piramide.get('M', [])
    F = datos_piramide.get('F', [])

    if not labels or (sum(M) == 0 and sum(F) == 0):
        return None

    y = np.arange(len(labels))
    plt.figure(figsize=(6, 3.5))

    # Hombres a la izquierda (negativo), Mujeres a la derecha (positivo)
    plt.barh(y, [-m for m in M], color='#3399ff', label='Masculino', edgecolor='white')
    plt.barh(y, F, color='#ff66b2', label='Femenino', edgecolor='white')

    plt.yticks(y, labels)
    plt.xlabel("Cantidad de Estudios/Pacientes")
    plt.title("Pirámide Poblacional")
    plt.legend(loc='upper right', frameon=False, fontsize=8)

    # Formatear el eje X para que NO muestre números negativos en el lado izquierdo
    formatter = ticker.FuncFormatter(lambda x, pos: f"{int(abs(x)):,}".replace(',', '.'))
    plt.gca().xaxis.set_major_formatter(formatter)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300)
    buf.seek(0)
    plt.close()
    return buf

def generar_grafico_apilado_sexo_tipo(datos_dict):
    """Genera un gráfico de columnas apiladas cruzando Tipo de Estudio y Sexo"""
    if not datos_dict: return None
    
    labels = list(datos_dict.keys())
    # Extraemos las listas de valores
    M = [datos_dict[l].get('M', 0) for l in labels]
    F = [datos_dict[l].get('F', 0) for l in labels]

    if sum(M) == 0 and sum(F) == 0: return None

    x = np.arange(len(labels))
    width = 0.6

    plt.figure(figsize=(8, 4))
    
    # Dibujamos las barras apiladas (M abajo, F arriba sumado al bottom)
    plt.bar(x, M, width, label='Masculino', color='#3399ff', edgecolor='white')
    plt.bar(x, F, width, bottom=M, label='Femenino', color='#ff66b2', edgecolor='white')

    plt.xticks(x, labels, rotation=45, ha="right", fontsize=8)
    plt.ylabel("Cantidad de Pacientes")
    plt.title("Distribución de Sexo por Modalidad")
    plt.legend(loc="upper center", bbox_to_anchor=(0.5, 1.15), ncol=2, frameon=False)
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300)
    buf.seek(0)
    plt.close()
    return buf

def generar_mapa_calor(datos_dict):
    """Genera un mapa de calor cruzando Origen (Y) vs Tipo de Estudio (X)"""
    if not datos_dict: return None
    
    origenes = list(datos_dict.keys())
    tipos = set()
    for o in origenes:
        tipos.update(datos_dict[o].keys())
    tipos = sorted(list(tipos))

    if not origenes or not tipos: return None

    # Construimos la matriz 2D
    matrix = np.zeros((len(origenes), len(tipos)))
    for i, o in enumerate(origenes):
        for j, t in enumerate(tipos):
            matrix[i, j] = datos_dict[o].get(t, 0)

    plt.figure(figsize=(8, 5))
    ax = plt.gca()
    
    # Mapa de calor usando una paleta azul (Blues)
    im = ax.imshow(matrix, cmap="Blues", aspect="auto")

    # Configuramos los ejes
    ax.set_xticks(np.arange(len(tipos)))
    ax.set_yticks(np.arange(len(origenes)))
    ax.set_xticklabels(tipos, rotation=45, ha="right", fontsize=8)
    ax.set_yticklabels(origenes, fontsize=8)

    # Imprimimos los números dentro de los cuadros del mapa
    threshold = matrix.max() / 2.0
    for i in range(len(origenes)):
        for j in range(len(tipos)):
            val = int(matrix[i, j])
            if val > 0: # Solo mostramos donde hay datos
                color_texto = "white" if val > threshold else "black"
                ax.text(j, i, str(val), ha="center", va="center", color=color_texto, fontsize=8, fontweight='bold')

    plt.title("Mapa de Calor: Origen vs Modalidad", pad=20)
    plt.tight_layout()
    
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=300)
    buf.seek(0)
    plt.close()
    return buf

def generar_reporte_ris_corporativo(datos: dict, ruta_salida: str):
    """Genera el informe PDF completo de RIS Analytics con diseño corporativo"""
    c = canvas.Canvas(ruta_salida, pagesize=A4)
    ancho, alto = A4

    h_nombre = datos.get('hospital_name', 'Institución')
    h_id = datos.get('hospital_id', 'S/D')
    f_desde, f_hasta = datos.get('fecha_desde', ''), datos.get('fecha_hasta', '')

    # PÁGINA 1: PORTADA
    crear_portada(c, ancho, alto, 'estadisticas', h_id, h_nombre, f_desde, f_hasta)

    # PÁGINA 2: COMIENZO DE ANÁLISIS
    pagina_actual = 2
    pos_y = crear_encabezado(c, ancho, alto, "ANÁLISIS DE DEMANDA RIS", h_id, h_nombre, pagina_actual)
    
    # Texto Auditoría
    pos_y -= 25
    c.setFont("Helvetica", 10); c.setFillColorRGB(0.4, 0.4, 0.4)
    c.drawString(40, pos_y, f"Informe generado en base a la auditoría de {datos.get('kpi_total_registros', 0):,} registros analizados.")

    # TABLERO DE KPIs (4 Cajas)
    pos_y -= 85
    kpis = [("TOTAL ESTUDIOS", f"{datos.get('kpi_total_estudios', 0):,}"), 
            ("PACIENTES ÚNICOS", f"{datos.get('kpi_pacientes', 0):,}"),
            ("EDAD PROMEDIO", f"{datos.get('kpi_edad_promedio', 0):.1f}"),
            ("ORÍGENES", str(datos.get('kpi_origenes_distintos', 0)))]

    for i, (label, val) in enumerate(kpis):
        x = 45 + i * 130
        c.setFillColorRGB(0.9, 0.9, 0.9); c.roundRect(x+2, pos_y-2, 120, 65, 6, fill=1, stroke=0)
        c.setFillColor(colors.HexColor('#004c99')); c.roundRect(x, pos_y, 120, 65, 6, fill=1, stroke=0)
        c.setFillColorRGB(1, 1, 1); c.setFont("Helvetica-Bold", 18); c.drawCentredString(x+60, pos_y+32, str(val).replace(',', '.'))
        c.setFillColorRGB(0.8, 0.9, 1); c.setFont("Helvetica-Bold", 7); c.drawCentredString(x+60, pos_y+12, label)

    pos_y -= 50

    # Lógica de Paginación Dinámica
    def check_p(py, pa):
        if py < 250:
            c.showPage()
            pa += 1
            return crear_encabezado(c, ancho, alto, "ANÁLISIS DE DEMANDA (CONT.)", h_id, h_nombre, pa) - 30, pa
        return py, pa

    # RENDERIZADO DE GRÁFICOS
    secciones = [
        ('datos_tipo', "ESTUDIOS POR TIPO (MODALIDAD)", "#1abc9c", generar_grafico_barras_memoria),
        ('datos_sexo', "DISTRIBUCIÓN POR SEXO", None, generar_grafico_torta_sexo),
        ('datos_piramide', "PIRÁMIDE POBLACIONAL (EDAD Y SEXO)", None, generar_grafico_piramide_memoria),
        ('datos_sexo_tipo', "SEXO POR TIPO DE ESTUDIO", None, generar_grafico_apilado_sexo_tipo),
        ('datos_mapa_calor', "MAPA DE CALOR: ORIGEN VS MODALIDAD", None, generar_mapa_calor),
        ('datos_edad', "DISTRIBUCIÓN POR RANGO DE EDAD", "#d35400", generar_grafico_barras_memoria),
        ('datos_equipos', "VOLUMEN DE ESTUDIOS POR EQUIPO", "#3399ff", generar_grafico_barras_memoria),
        ('datos_origen', "DISTRIBUCIÓN POR ORIGEN", "#004c99", generar_grafico_barras_memoria)
    ]

    for key, title, color, func in secciones:
        if datos.get(key):
            pos_y, pagina_actual = check_p(pos_y, pagina_actual)
            c.setFont("Helvetica-Bold", 12); c.setFillColorRGB(0.17, 0.24, 0.31)
            c.drawString(40, pos_y, title); pos_y -= 10
            img = func(datos[key], "", color) if color else func(datos[key])
            if img:
                h_img = 250 if key == 'datos_mapa_calor' else 200
                c.drawImage(ImageReader(img), 40, pos_y - h_img, width=ancho-80, height=h_img, mask='auto', preserveAspectRatio=True)
                pos_y -= (h_img + 40)

    c.save()

