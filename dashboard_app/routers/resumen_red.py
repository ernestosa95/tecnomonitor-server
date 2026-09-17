"""
Vistas agregadas de toda la red: resumen por hospital, resumen por
provincia/proyecto, datos del mapa nacional, y el listado de nodos.

Tercer router extraído de dashboard.py -- ver docs/08-plan-refactor-dashboard.md.

⚠️ Nota sobre /api/v1/nodos-hospitalarios: llama a `obtener_nodos_desde_db()`,
una función que no existe en ningún lado del repo (bug preexistente, no
introducido por este refactor -- confirmado antes de mover el código: el
endpoint devuelve 500 para cualquier usuario autenticado). Se mueve tal cual
estaba porque este refactor es solo reorganización, no corrección de bugs.
Ver docs/08-plan-refactor-dashboard.md.
"""
import json
import re
import time
import csv
from io import StringIO
from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

import auth
import database
import resumen_hospital
from core import get_db
from database import HospitalMetadata

router = APIRouter()

# ==========================================
# ⚡ CACHÉ EN MEMORIA PARA EL DASHBOARD
# ==========================================
_cache_resumen = {"data": None, "ts": 0}
CACHE_TTL_SEGUNDOS = 30

_cache_provincias = {"data": None, "ts": 0}
CACHE_TTL_PROVINCIAS = 60

# Centroides aprox. de provincias (lat, lng) para ubicar el marcador en /prov-analytics.
CENTROIDES_PROVINCIAS = {
    "Buenos Aires": [-36.5, -60.2], "CABA": [-34.61, -58.38], "Córdoba": [-32.0, -63.5],
    "Santa Fe": [-30.7, -60.9], "Tucumán": [-26.9, -65.2], "Chubut": [-43.8, -68.5],
    "Neuquén": [-38.9, -69.8], "Mendoza": [-34.9, -68.5], "Catamarca": [-27.3, -66.9],
    "Jujuy": [-23.3, -65.8], "Entre Ríos": [-32.0, -59.2], "Salta": [-24.3, -64.8],
    "San Juan": [-30.8, -68.9], "Formosa": [-24.9, -59.9], "La Rioja": [-29.7, -67.0],
    "Tierra del Fuego": [-53.8, -67.9], "Chaco": [-26.4, -60.5], "Corrientes": [-28.7, -57.8],
    "Río Negro": [-40.2, -67.2], "Santiago del Estero": [-27.8, -63.2], "Misiones": [-26.9, -54.5],
    "Santa Cruz": [-48.6, -70.0], "San Luis": [-33.7, -66.0], "La Pampa": [-37.2, -65.5],
}

_CANON_PROVINCIAS = {
    "caba": "CABA", "ciudad autonoma de buenos aires": "CABA", "capital federal": "CABA",
    "buenos aires": "Buenos Aires", "cordoba": "Córdoba", "santa fe": "Santa Fe",
    "tucuman": "Tucumán", "chubut": "Chubut", "neuquen": "Neuquén", "mendoza": "Mendoza",
    "catamarca": "Catamarca", "jujuy": "Jujuy", "entre rios": "Entre Ríos", "salta": "Salta",
    "san juan": "San Juan", "formosa": "Formosa", "la rioja": "La Rioja",
    "tierra del fuego": "Tierra del Fuego", "chaco": "Chaco", "corrientes": "Corrientes",
    "rio negro": "Río Negro", "santiago del estero": "Santiago del Estero",
    "misiones": "Misiones", "santa cruz": "Santa Cruz", "san luis": "San Luis",
    "la pampa": "La Pampa",
}

def _normalizar_provincia(p):
    s = (p or "").strip()
    if s == "":
        return "Sin provincia"
    return _CANON_PROVINCIAS.get(s.lower(), s.title())

# Clasificación por proyecto según el ID del hospital: BID = H01..H46 (sólo
# "H" + dígitos), PROSEPU = P01..P26 (sólo "P" + dígitos). IDs que no matchean
# ese patrón exacto (PAMI, privados, demo, etc. — ej. HEMDQ, PMHBI, GWNCORDOBA)
# caen en "Otros" en vez de forzarse a BID/PROSEPU.
_RE_BID = re.compile(r"^H\d+$", re.IGNORECASE)
_RE_PROSEPU = re.compile(r"^P\d+$", re.IGNORECASE)

def _clasificar_proyecto(hospital_id):
    hid = (hospital_id or "").strip().upper()
    if _RE_BID.match(hid):
        return "BID"
    if _RE_PROSEPU.match(hid):
        return "PROSEPU"
    return "Otros"


@router.get("/api/resumen-hospitales")
def obtener_resumen(db: Session = Depends(get_db), current_user: dict = Depends(auth.bloquear_cliente())):
    global _cache_resumen
    ahora = time.time()

    # 1. Verificación del caché local
    if _cache_resumen["data"] is not None and (ahora - _cache_resumen.get("ts", 0)) < 30:
        return _cache_resumen["data"]

    hospitales_meta = db.query(database.HospitalMetadata).filter(
        database.HospitalMetadata.is_visible == True
    ).all()

    resultado_final = []

    for hosp in hospitales_meta:
        # --- SECCIÓN A: INFRAESTRUCTURA (Último reporte de estado) ---
        ultimo_reporte = db.query(database.ReporteModel).filter(
            database.ReporteModel.hospital_id == hosp.hospital_id
        ).order_by(database.ReporteModel.timestamp.desc()).first()

        fecha_reporte = "Sin datos"
        estado_texto = "Offline"
        elementos = []

        if ultimo_reporte:
            fecha_reporte = str(ultimo_reporte.timestamp)[:19]
            estado_texto = ultimo_reporte.host_status or "Offline"

            # 🛠️ FIX 1: Restaurada la lógica original para extraer los Nodos (VMs)
            try:
                if isinstance(ultimo_reporte.full_json_data, str):
                    data_json = json.loads(ultimo_reporte.full_json_data)
                else:
                    data_json = ultimo_reporte.full_json_data or {}

                virtual_layer = data_json.get("virtual_layer", [])

                if isinstance(virtual_layer, list):
                    for vm in virtual_layer:
                        estado_vm = vm.get("state", "unknown").lower()
                        color_state = "success" if estado_vm in ["running", "online"] else ("warning" if estado_vm == "warning" else "error")

                        elementos.append({
                            "label": vm.get("id", "VM"),
                            "state": color_state
                        })
            except Exception:
                pass

        # --- SECCIÓN B: MÉTRICAS HISTÓRICAS (PACS KPIs) ---
        todos_los_usos = db.query(database.ReporteUso).filter(
            database.ReporteUso.hospital_id == hosp.hospital_id
        ).all()

        estudios_pacs = 0
        estudios_ia = 0
        equipos_pacs = set()

        for uso in todos_los_usos:
            if uso.kpi_json_data:
                try:
                    kpis = json.loads(uso.kpi_json_data) if isinstance(uso.kpi_json_data, str) else uso.kpi_json_data
                    for item in kpis.get("pacs", []):
                        aet = item.get("aet", "").upper().strip()
                        mod = item.get("mod", "") or ""

                        # Mismo criterio que calcular_kpis_hospital (resumen_hospital.py) --
                        # antes este endpoint exigía "ENT_" con guion bajo exacto y se
                        # perdía AETs reales como ENTP_*/ENTB_*/ENTELAIENTB_* (confirmado
                        # con datos de producción), subcontando "Procesamientos IA" en el
                        # home. Reutilizamos las mismas constantes para no volver a divergir.
                        #
                        # EXCLUDED_MODS también importa acá: Entelai manda DOS items por
                        # cada estudio que procesa (uno "MG" -imagen marcada- y uno "DOC"
                        # -informe generado-). Sin excluir "DOC" se duplicaba el conteo de
                        # "Procesamientos IA" (confirmado con datos de producción).
                        if (aet and aet not in resumen_hospital.EXCLUDED_AETS
                                and mod not in resumen_hospital.EXCLUDED_MODS):
                            if aet.startswith(resumen_hospital.PREFIJO_IA):
                                estudios_ia += item.get("almacenados", 0)
                            else:
                                # Si no, es un estudio de equipo médico estándar
                                estudios_pacs += item.get("almacenados", 0)
                                equipos_pacs.add(aet)
                except Exception:
                    pass

        # --- SECCIÓN C: CONSTRUCCIÓN DEL OBJETO FINAL ---
        resultado_final.append({
            "raw_id": hosp.hospital_id,
            "id": hosp.hospital_id,
            "name": hosp.nombre,
            "timestamp": fecha_reporte,
            "status": estado_texto,
            "elements": elementos,
            "kpi_estudios": estudios_pacs,
            "kpi_equipos": list(equipos_pacs),
            "kpi_estudios_ia": estudios_ia
        })

    # Guardar en la caché global (Nota: usamos "ts" como espera script.js)
    _cache_resumen["data"] = resultado_final
    _cache_resumen["ts"] = ahora

    return resultado_final


@router.get("/api/provincias")
def obtener_resumen_provincias(db: Session = Depends(get_db),
                               current_user: dict = Depends(auth.bloquear_cliente())):
    """
    Resumen de KPIs agrupado por provincia para /prov-analytics.
    Hospitales normales: calculados en vivo (resumen_hospital.py).
    Hospitales marcados con datos_manuales: leídos de HospitalManualKPI
    (cargados a mano desde el panel de Hospitales, para los que no tienen
    agente de monitoreo instalado).
    """
    global _cache_provincias
    ahora = time.time()

    if _cache_provincias["data"] is not None and (ahora - _cache_provincias.get("ts", 0)) < CACHE_TTL_PROVINCIAS:
        return _cache_provincias["data"]

    hospitales_meta = db.query(HospitalMetadata).filter(
        HospitalMetadata.is_visible == True
    ).all()

    manuales_por_id = {
        fila.hospital_id: fila
        for fila in db.query(database.HospitalManualKPI).all()
    }

    def _bucket():
        return {
            "hospitales": [], "estudios": 0, "admitidas": 0, "asociadas": 0,
            "definitivas": 0, "ia": 0, "equipos": 0,
            "tb_alm": 0.0, "tb_disp": 0.0, "ram_sum": 0.0, "ram_n": 0,
        }

    por_provincia = defaultdict(_bucket)
    por_proyecto = defaultdict(_bucket)

    # Resumen ejecutivo BID+PROSEPU (estudios PACS vs. vía IA), para la
    # tarjeta de /prov-analytics. A pedido explícito: solo hospitales con
    # agente real instalado -- los de carga manual solo guardan un total de
    # "ia" (HospitalManualKPI), así que se excluyen del todo en vez de
    # mezclar datos reales con datos incompletos.
    #
    # Nota: se evaluó desglosar por modalidad (Rx/MG), pero se descartó -- el
    # campo `mod` que manda el agente para los AETs de IA (prefijo ENT_) no
    # es la modalidad clínica de origen, es el tipo de artefacto de salida
    # del motor de IA (imagen marcada = "MG", informe generado = "DOC"),
    # confirmado con datos reales de producción. No hay forma de recuperar
    # la modalidad real del estudio con lo que se ingesta hoy.
    resumen_bid_prosepu = {"estudios": 0, "ia": 0}

    for hosp in hospitales_meta:
        if getattr(hosp, "datos_manuales", False):
            fila = manuales_por_id.get(hosp.hospital_id)
            if fila:
                kpis = {
                    "estudios": fila.estudios, "admitidas": fila.admitidas,
                    "asociadas": fila.asociadas, "definitivas": fila.definitivas,
                    "ia": fila.ia, "equipos": fila.equipos,
                    # Sin serie temporal real (es una carga manual puntual): no se
                    # puede calcular ventana de 12 meses ni extrapolar.
                    "estudios_pacs_anual": None, "estudios_pacs_anual_estimado": None,
                    "estudios_pacs_anual_meses_base": None,
                    "tb_alm": fila.tb_alm, "tb_disp": fila.tb_disp, "ram": fila.ram,
                    "go_live": fila.go_live or "",
                }
                pendiente = False
            else:
                kpis = {"estudios": 0, "admitidas": 0, "asociadas": 0, "definitivas": 0,
                       "ia": 0, "equipos": 0,
                       "estudios_pacs_anual": None, "estudios_pacs_anual_estimado": None,
                       "estudios_pacs_anual_meses_base": None,
                       "tb_alm": None, "tb_disp": None,
                       "ram": None, "go_live": ""}
                pendiente = True
        else:
            kpis = resumen_hospital.calcular_kpis_hospital(db, hosp.hospital_id)
            pendiente = False

        hosp_out = {
            "id": hosp.hospital_id, "nombre": hosp.nombre,
            "go_live": kpis["go_live"],
            "estudios": kpis["estudios"], "admitidas": kpis["admitidas"],
            "asociadas": kpis["asociadas"], "definitivas": kpis["definitivas"],
            "ia": kpis["ia"], "equipos": kpis["equipos"],
            "estudios_pacs_anual": kpis["estudios_pacs_anual"],
            "estudios_pacs_anual_estimado": kpis["estudios_pacs_anual_estimado"],
            "estudios_pacs_anual_meses_base": kpis["estudios_pacs_anual_meses_base"],
            "tb_alm": kpis["tb_alm"], "tb_disp": kpis["tb_disp"], "ram": kpis["ram"],
            "pendiente_carga": pendiente,
        }

        clave_proyecto = _clasificar_proyecto(hosp.hospital_id)
        if not getattr(hosp, "datos_manuales", False) and clave_proyecto in ("BID", "PROSEPU"):
            resumen_bid_prosepu["estudios"] += kpis["estudios"]
            resumen_bid_prosepu["ia"] += kpis["ia"]

        for grupo, clave in ((por_provincia, _normalizar_provincia(hosp.provincia)),
                             (por_proyecto, clave_proyecto)):
            d = grupo[clave]
            d["estudios"] += kpis["estudios"]; d["admitidas"] += kpis["admitidas"]
            d["asociadas"] += kpis["asociadas"]; d["definitivas"] += kpis["definitivas"]
            d["ia"] += kpis["ia"]; d["equipos"] += kpis["equipos"]
            if kpis["tb_alm"] is not None:
                d["tb_alm"] += kpis["tb_alm"]
            if kpis["tb_disp"] is not None:
                d["tb_disp"] += kpis["tb_disp"]
            if kpis["ram"] is not None:
                d["ram_sum"] += kpis["ram"]; d["ram_n"] += 1
            d["hospitales"].append(hosp_out)

    def _armar_salida(grupo_dict, campo_clave, con_centroide=False):
        salida = []
        for clave, d in grupo_dict.items():
            item = {
                campo_clave: clave,
                "n_hospitales": len(d["hospitales"]),
                "estudios": d["estudios"], "admitidas": d["admitidas"],
                "asociadas": d["asociadas"], "definitivas": d["definitivas"],
                "ia": d["ia"], "equipos": d["equipos"],
                "tb_alm": round(d["tb_alm"], 2), "tb_disp": round(d["tb_disp"], 2),
                "ram": round(d["ram_sum"] / d["ram_n"], 1) if d["ram_n"] else None,
                "hospitales": sorted(d["hospitales"], key=lambda h: -h["estudios"]),
            }
            if con_centroide:
                item["centroide"] = CENTROIDES_PROVINCIAS.get(clave)
            salida.append(item)
        salida.sort(key=lambda x: -x["estudios"])
        return salida

    resultado = {
        "provincias": _armar_salida(por_provincia, "provincia", con_centroide=True),
        "proyectos": _armar_salida(por_proyecto, "proyecto"),
        "resumen_bid_prosepu": resumen_bid_prosepu,
    }
    _cache_provincias["data"] = resultado
    _cache_provincias["ts"] = ahora
    return resultado


@router.get("/api/mapa-data")
def obtener_datos_mapa(db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.bloquear_cliente())):
    conf = db.query(database.ConfigModel).filter_by(clave="offline_minutes").first()
    limit_min = int(conf.valor) if (conf and conf.valor) else 10
    limit_delta = timedelta(minutes=limit_min)
    ahora = datetime.now()

    hospitales = db.query(HospitalMetadata).filter(
        HospitalMetadata.is_visible == True,
        HospitalMetadata.latitud != None,
        HospitalMetadata.latitud != "",
        HospitalMetadata.longitud != None,
        HospitalMetadata.longitud != ""
    ).all()

    mapa_data = []

    for h in hospitales:
        last_report = db.execute(
            text("SELECT timestamp FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1"),
            {"hid": h.hospital_id}
        ).fetchone()

        status = "Offline"
        if last_report:
            last_seen = last_report.timestamp
            if isinstance(last_seen, str):
                try: last_seen = datetime.strptime(last_seen, "%Y-%m-%d %H:%M:%S.%f")
                except: pass
            if (ahora - last_seen) <= limit_delta:
                status = "Online"

        try:
            mapa_data.append({
                "id": h.hospital_id,
                "nombre": h.nombre,
                "status": status,
                "lat": float(h.latitud),
                "lng": float(h.longitud)
            })
        except ValueError: continue

    return mapa_data


@router.get("/api/v1/nodos-hospitalarios")
async def get_nodos(current_user: dict = Depends(auth.get_current_user)):
    # Reutiliza tu lógica de conexión a DB existente (ej. Accesorios/actualizar_db.py)
    return obtener_nodos_desde_db()


# ==========================================
# ⚡ EXPORTACIÓN A CSV (Aprovechando Caché)
# ==========================================
@router.get("/api/provincias/exportar/csv")
def exportar_provincias_csv(
    modo: str = "provincia",
    clave: str = None,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.bloquear_cliente())
):
    """
    Exporta la tabla de hospitales a formato CSV.
    Si modo='provincia', exporta los hospitales de esa provincia.
    Si modo='proyecto', exporta los hospitales de ese proyecto.
    Si no se pasa clave, exporta la red completa.
    """
    # 1. Obtener la data (aprovecha la caché existente de CACHE_TTL_PROVINCIAS para máxima eficiencia)
    datos = obtener_resumen_provincias(db, current_user)

    hospitales_a_exportar = []

    # 2. Filtrar según lo que el usuario esté viendo en el frontend
    if clave:
        if modo == "provincia":
            grupo = next((p for p in datos["provincias"] if p["provincia"] == clave), None)
            if grupo:
                hospitales_a_exportar = grupo["hospitales"]
        elif modo == "proyecto":
            grupo = next((p for p in datos["proyectos"] if p["proyecto"] == clave), None)
            if grupo:
                hospitales_a_exportar = grupo["hospitales"]
    else:
        # Si requieren un general de todo, iteramos las provincias para obtener todos los hospitales
        for prov in datos["provincias"]:
             hospitales_a_exportar.extend(prov["hospitales"])

    # Evitar duplicados si se exporta todo
    hospitales_unicos = {h["id"]: h for h in hospitales_a_exportar}.values()

    # 3. Preparar el buffer de texto para el CSV
    output = StringIO()
    writer = csv.writer(output, delimiter=';') # Usamos punto y coma para no romper cifras con coma decimal

    # Escribir Cabeceras
    writer.writerow([
        "Hospital", "Código", "Modo Agrupación", "Grupo",
        "Estudios (PACS)", "PACS / Año (Proyectado)", "Órdenes Admitidas (RIS)",
        "Estudios vía IA", "Equipos Conectados", "TB Almacenados", "RAM Promedio (%)", "Go-Live"
    ])

    # 4. Escribir filas
    for h in hospitales_unicos:
        est_anual = h.get("estudios_pacs_anual")
        est_anual_str = str(est_anual) if est_anual is not None else "-"
        if h.get("estudios_pacs_anual_estimado"):
            est_anual_str = "~" + est_anual_str

        writer.writerow([
            h.get("nombre", ""),
            h.get("id", ""),
            modo.capitalize(),
            clave if clave else "Todos",
            h.get("estudios", 0),
            est_anual_str,
            h.get("admitidas", 0),
            h.get("ia", 0),
            h.get("equipos", 0),
            h.get("tb_alm") if h.get("tb_alm") is not None else "-",
            h.get("ram") if h.get("ram") is not None else "-",
            h.get("go_live", "")
        ])

    output.seek(0)

    # Nombre del archivo dinámico
    nombre_limpio = str(clave).replace(" ", "_").lower() if clave else "nacional"
    filename = f"tecnomonitor_{modo}_{nombre_limpio}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )
