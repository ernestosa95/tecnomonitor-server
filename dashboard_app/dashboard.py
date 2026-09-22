import sys
import os
import json
from collections import defaultdict
import requests   # usado en _cerrar_alertas_por_regla() para el trigger del WebSocket
import database
import matplotlib.dates as mdates
from database import HospitalMetadata, HistorialReportes, AlertaModel, ReporteModel
from database import HistorialReportes
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Depends, HTTPException, status
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import text
from pydantic import BaseModel
import io
from typing import Optional
import matplotlib
matplotlib.use('Agg') # Crucial para servidores: dibuja sin abrir ventanas
import matplotlib.pyplot as plt
from fastapi.responses import StreamingResponse
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.platypus import Table, TableStyle
from reportlab.lib.utils import ImageReader
import numpy as np
import asana_conector
import auth
import time
from fastapi import WebSocket, WebSocketDisconnect
import asyncio
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import re
from fastapi import Response
from fastapi.middleware.gzip import GZipMiddleware
import generator_report
import resumen_hospital
from fastapi import FastAPI
from starlette.middleware.base import BaseHTTPMiddleware

import tempfile
from fastapi import BackgroundTasks
from fastapi.responses import FileResponse, RedirectResponse
from schemas import DatosRISAnalytics

import csv
import os
from fastapi import FastAPI, Request, Form, APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import permissions
from sqlalchemy import ForeignKey, UniqueConstraint 
import secrets, string
from typing import List
import re as _re

# USERNAME_REGEX ahora vive en routers/usuarios.py (único lugar que la usa).
# ESTADOS_COLORS ahora vive en routers/informes.py (único lugar que la usa).

# Ajuste de Path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database
from database import HospitalMetadata 
import alerts_engine 
from dotenv import load_dotenv
from fastapi.responses import JSONResponse

load_dotenv()

base_dir = os.path.dirname(os.path.abspath(__file__))
static_dir = os.path.join(base_dir, "static")

app = FastAPI(title="TecnoXaas Dashboard")
app.mount("/static", StaticFiles(directory=static_dir), name="static")

# get_db, templates y limiter viven en core.py -- compartidos por los routers
# en routers/. Ver docs/08-plan-refactor-dashboard.md.
from core import custom_rate_limit_handler, get_db, limiter, templates

# --- ROUTERS EXTRAÍDOS ---
from routers import (
    alertas_config, clientes, hospital_detalle, hospitales_metadata, informes,
    manual, mirth_mapa, mirth_topologia, paginas_publicas, resumen_red, runbooks,
    solicitudes_acceso, usuarios, websocket,
)
# Alias: dashboard.py ya tiene `import auth` (dashboard_app/auth.py, todavía
# usado en el resto del archivo) -- este es el router routers/auth.py, un
# módulo distinto pese al mismo nombre base. Ver nota en routers/auth.py.
from routers import auth as auth_router

app.include_router(paginas_publicas.router)
app.include_router(websocket.router)
app.include_router(resumen_red.router)
app.include_router(hospitales_metadata.router)
app.include_router(alertas_config.router)
app.include_router(usuarios.router)
app.include_router(clientes.router)
app.include_router(solicitudes_acceso.router)
app.include_router(informes.router)
app.include_router(hospital_detalle.router)
app.include_router(mirth_topologia.router)
app.include_router(mirth_mapa.router)
app.include_router(runbooks.router)
app.include_router(manual.router)
app.include_router(auth_router.router)

# ==========================================
# 🛡️ RATE LIMITING (Control de tráfico)
# ==========================================
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, custom_rate_limit_handler)

# ==========================================
# 🛡️ 1. CONFIGURACIÓN DE CORS
# ==========================================
# Aquí debes listar los dominios EXACTOS desde donde vas a entrar.
# Si lo vas a publicar, reemplaza el localhost por tu dominio real.
ORIGINES_PERMITIDOS = [
    "http://localhost",
    "http://localhost:8001",
    "http://127.0.0.1:8001",
    "https://tecnomonitor.tecnoimagen.com.ar/"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ORIGINES_PERMITIDOS,
    allow_credentials=True,
    allow_methods=["*"], # Permite todos los métodos (GET, POST, PUT, etc)
    allow_headers=["Authorization", "Content-Type"], # Solo permitimos estas cabeceras
)

# ==========================================
# 🚀 1.5 COMPRESIÓN GZIP (Nuevo)
# ==========================================
app.add_middleware(GZipMiddleware, minimum_size=1000)

# ==========================================
# 🛡️ 2. CABECERAS DE SEGURIDAD (Security Headers)
# ==========================================
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    
    # Evita que el sitio se incruste en un iframe malicioso (Clickjacking)
    response.headers["X-Frame-Options"] = "DENY"
    
    # Evita que el navegador adivine tipos de archivos maliciosos (MIME Sniffing)
    response.headers["X-Content-Type-Options"] = "nosniff"
    
    # Fuerza el uso de HTTPS por 1 año (HSTS) - Muy importante en producción
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    
    # Content-Security-Policy (CSP)
    # Adaptado específicamente para permitir Chart.js, Leaflet y los estilos inline de tu index.html
    # Content-Security-Policy (CSP)
    # Se agrega http://localhost:8501 a frame-src para permitir el embebido de Streamlit
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://netdna.bootstrapcdn.com https://unpkg.com; "
        "img-src 'self' data: https://a.basemaps.cartocdn.com https://b.basemaps.cartocdn.com https://c.basemaps.cartocdn.com https://d.basemaps.cartocdn.com "
        "https://a.tile.openstreetmap.org https://b.tile.openstreetmap.org https://c.tile.openstreetmap.org; "
        "connect-src 'self' https://cdn.jsdelivr.net; "
        "frame-src 'self' http://localhost:8501;"
    )
    response.headers["Content-Security-Policy"] = csp
    
    return response


from fastapi.responses import HTMLResponse

@app.get("/beta/simulador", response_class=HTMLResponse)
async def simulador_iframe(current_user: dict = Depends(auth.get_current_user)):
    return """
    <html>
        <head>
            <title>Simulador C-Level</title>
            <style>
                body, html { margin: 0; padding: 0; height: 100%; overflow: hidden; background: #fff; }
                iframe { width: 100%; height: 100%; border: none; }
            </style>
        </head>
        <body>
            <!-- Apunta al puerto donde corre Streamlit internamente en el servidor -->
            <iframe src="http://localhost:8501"></iframe>
        </body>
    </html>
    """

