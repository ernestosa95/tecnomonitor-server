# -*- coding: utf-8 -*-
"""
Cálculo en vivo de los KPIs de un hospital (estudios, órdenes RIS,
almacenamiento, RAM) a partir de la DB.

Es la versión "en vivo" de la lógica que exportar_resumen_hospitales.py
corre offline sobre toda la historia vía sqlite3 crudo. Acá se calcula
por hospital con las tablas ya indexadas por hospital_id, para poder
llamarse en cada request de /api/provincias sin escanear reportes_historicos
completo (esa tabla puede tener millones de filas).

Diferencia deliberada con el script offline: tb_alm/tb_disp/ram se toman
del ÚLTIMO reporte de infraestructura del hospital, no de un promedio
sobre toda la historia — mismo criterio que ya usa /api/resumen-hospitales.
"""
import json
from collections import defaultdict

import database

EXCLUDED_AETS = {"CLIENT", "WADO", "PACS"}
EXCLUDED_MODS = {"DOC"}
PREFIJO_IA = "ENT"
CAPACIDAD_TB_TOTAL = 60.0
GB_POR_TB = 1024.0


def uso_disco_j_appv_tb(full_json):
    """Uso (GB->TB) del disco J en la VM cuyo id termina en APPV. None si no está."""
    try:
        data = json.loads(full_json) if isinstance(full_json, str) else (full_json or {})
    except (json.JSONDecodeError, TypeError):
        return None
    for vm in data.get("virtual_layer", []) or []:
        vid = str(vm.get("id", "")).upper()
        if not vid.endswith("APPV"):
            continue
        for disk in vm.get("storage", []) or []:
            mount = str(disk.get("mount_point", "")).strip().upper()
            if mount.startswith("J"):
                total = float(disk.get("total_gb") or 0)
                free = float(disk.get("free_gb") or 0)
                usado_gb = max(total - free, 0.0)
                return round(usado_gb / GB_POR_TB, 2)
    return None


def ram_pct(full_json):
    try:
        data = json.loads(full_json) if isinstance(full_json, str) else (full_json or {})
    except (json.JSONDecodeError, TypeError):
        return None
    tele = (data.get("physical_layer") or {}).get("telemetry") or {}
    val = (tele.get("ram") or {}).get("usage_percent")
    try:
        return float(val) if val is not None else None
    except (ValueError, TypeError):
        return None


def calcular_kpis_hospital(db, hospital_id: str) -> dict:
    """
    Devuelve estudios/admitidas/asociadas/definitivas/ia/equipos (acumulado
    histórico, sumando todos los reportes_uso del hospital) + tb_alm/tb_disp/
    ram (del último reporte de infraestructura) + go_live.
    """
    acc = {
        "estudios": 0, "admitidas": 0, "asociadas": 0, "definitivas": 0,
        "ia": 0, "equipos": 0,
    }
    aets = set()
    go_live = None

    usos = db.query(database.ReporteUso).filter(
        database.ReporteUso.hospital_id == hospital_id
    ).all()

    for uso in usos:
        if not uso.kpi_json_data:
            continue
        try:
            metrics = json.loads(uso.kpi_json_data) if isinstance(uso.kpi_json_data, str) else uso.kpi_json_data
        except (json.JSONDecodeError, TypeError):
            continue

        hay_actividad = False

        for item in metrics.get("ris", []) or []:
            aet = item.get("aet")
            eq = item.get("equipo")
            mod = item.get("mod", "") or ""
            nombre_ris = eq or aet or "Desc"
            if nombre_ris in EXCLUDED_AETS or aet in EXCLUDED_AETS or mod in EXCLUDED_MODS:
                continue
            adm = int(item.get("admitidos", 0) or 0)
            aso = int(item.get("con_imagen", 0) or 0)
            de = int(item.get("definitivos", 0) or 0)
            acc["admitidas"] += adm
            acc["asociadas"] += aso
            acc["definitivas"] += de
            if adm or aso or de or int(item.get("totales", 0) or 0):
                hay_actividad = True

        for item in metrics.get("pacs", []) or []:
            aet = (item.get("aet") or "Desc").upper().strip()
            mod = item.get("mod", "") or ""
            if aet in EXCLUDED_AETS or mod in EXCLUDED_MODS:
                continue
            val = int(item.get("almacenados", 0) or 0)
            if val <= 0:
                continue
            acc["estudios"] += val
            aets.add(aet)
            if aet.startswith(PREFIJO_IA):
                acc["ia"] += val
            hay_actividad = True

        if hay_actividad:
            fecha = uso.timestamp
            if fecha is not None and (go_live is None or fecha < go_live):
                go_live = fecha

    acc["equipos"] = len(aets)

    ultimo_reporte = db.query(database.ReporteModel).filter(
        database.ReporteModel.hospital_id == hospital_id
    ).order_by(database.ReporteModel.timestamp.desc()).first()

    tb_alm = ram = None
    if ultimo_reporte:
        tb_alm = uso_disco_j_appv_tb(ultimo_reporte.full_json_data)
        ram = ram_pct(ultimo_reporte.full_json_data)
    tb_disp = round(CAPACIDAD_TB_TOTAL - tb_alm, 2) if tb_alm is not None else None

    return {
        "estudios": acc["estudios"],
        "admitidas": acc["admitidas"],
        "asociadas": acc["asociadas"],
        "definitivas": acc["definitivas"],
        "ia": acc["ia"],
        "equipos": acc["equipos"],
        "tb_alm": tb_alm,
        "tb_disp": tb_disp,
        "ram": ram,
        "go_live": go_live.strftime("%m/%d/%Y") if go_live else "",
    }
