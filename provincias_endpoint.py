# -*- coding: utf-8 -*-
"""
Endpoint de indicadores agrupados por provincia para la vista nueva de
index_beta (mapa + detalle por provincia).

Se apoya en la ULTIMA extraccion resumen por hospital. Para que la vista
sea liviana (no recalcular RIS/PACS/infra en cada request), lee de una
fuente ya materializada. Soporta dos modos:

  MODO_CSV   -> lee el csv que genera exportar_resumen_hospitales.py
  MODO_TABLA -> lee una tabla 'resumen_hospitales' (si la creaste)

Elegi uno con RESUMEN_SOURCE. Por defecto CSV.

Integracion (server.py / router principal):
    from provincias_endpoint import router as provincias_router
    app.include_router(provincias_router)

y en el front, la vista hace:  fetch('/api/provincias')
"""
from __future__ import annotations

import csv
import os
from collections import defaultdict

from fastapi import APIRouter

router = APIRouter()

# --- Configuracion ---
RESUMEN_SOURCE = "csv"                       # "csv" | "tabla"
CSV_PATH = os.getenv("RESUMEN_CSV", "resumen_hospitales.csv")
CSV_ENCODING = "utf-8-sig"                   # usar "latin-1" si tu csv viene asi

# Centroides aprox. de provincias (lat, lng) para ubicar el marcador.
CENTROIDES = {
    "Buenos Aires": [-36.5, -60.2], "CABA": [-34.61, -58.38], "Córdoba": [-32.0, -63.5],
    "Santa Fe": [-30.7, -60.9], "Tucumán": [-26.9, -65.2], "Chubut": [-43.8, -68.5],
    "Neuquén": [-38.9, -69.8], "Mendoza": [-34.9, -68.5], "Catamarca": [-27.3, -66.9],
    "Jujuy": [-23.3, -65.8], "Entre Ríos": [-32.0, -59.2], "Salta": [-24.3, -64.8],
    "San Juan": [-30.8, -68.9], "Formosa": [-24.9, -59.9], "La Rioja": [-29.7, -67.0],
    "Tierra del Fuego": [-53.8, -67.9], "Chaco": [-26.4, -60.5], "Corrientes": [-28.7, -57.8],
    "Río Negro": [-40.2, -67.2], "Santiago del Estero": [-27.8, -63.2], "Misiones": [-26.9, -54.5],
    "Santa Cruz": [-48.6, -70.0], "San Luis": [-33.7, -66.0], "La Pampa": [-37.2, -65.5],
}

# Normalizacion de nombres de provincia (las del origen no estan 100% limpias:
# aparece 'La Rioja' y 'La rioja', mayusculas mezcladas, etc.)
_CANON = {
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


def _norm_prov(p: str) -> str:
    s = (p or "").strip()
    if s == "":
        return "Sin provincia"
    return _CANON.get(s.lower(), s.title())


def _num(x):
    x = (str(x) if x is not None else "").strip().replace(",", ".")
    if x == "":
        return None
    try:
        return float(x)
    except ValueError:
        return None


def _int(x):
    v = _num(x)
    return int(v) if v is not None else 0


def _leer_filas():
    """Devuelve lista de dicts por hospital. Adaptar a tu fuente real."""
    if RESUMEN_SOURCE == "tabla":
        # from .db import SessionLocal  # ajustar import a tu proyecto
        # ...
        # SELECT hospital_id, hospital, provincia, go_live, ordenes_admitidas,
        #        ordenes_asociadas, ordenes_definitivas, estudios_almacenados,
        #        estudios_ia, equipos_almacenan, tb_almacenados, tb_disponibles,
        #        uso_ram_pct FROM resumen_hospitales
        raise NotImplementedError("Completar lectura de tabla resumen_hospitales")
    with open(CSV_PATH, encoding=CSV_ENCODING) as f:
        return list(csv.DictReader(f))


def _agrupar(filas):
    prov = defaultdict(lambda: {
        "hospitales": [], "estudios": 0, "admitidas": 0, "asociadas": 0,
        "definitivas": 0, "ia": 0, "equipos": 0,
        "tb_alm": 0.0, "tb_disp": 0.0, "ram_sum": 0.0, "ram_n": 0,
    })
    for r in filas:
        p = _norm_prov(r.get("provincia"))
        d = prov[p]
        est = _int(r.get("estudios_almacenados"))
        adm = _int(r.get("ordenes_admitidas"))
        aso = _int(r.get("ordenes_asociadas"))
        de = _int(r.get("ordenes_definitivas"))
        ia = _int(r.get("estudios_ia"))
        eq = _int(r.get("equipos_almacenan"))
        ta = _num(r.get("tb_almacenados"))
        td = _num(r.get("tb_disponibles"))
        ram = _num(r.get("uso_ram_pct"))
        d["estudios"] += est; d["admitidas"] += adm; d["asociadas"] += aso
        d["definitivas"] += de; d["ia"] += ia; d["equipos"] += eq
        if ta is not None:
            d["tb_alm"] += ta
        if td is not None:
            d["tb_disp"] += td
        if ram is not None:
            d["ram_sum"] += ram; d["ram_n"] += 1
        d["hospitales"].append({
            "id": r.get("hospital_id"), "nombre": r.get("hospital"),
            "go_live": (r.get("go_live") or "").strip(),
            "admitidas": adm, "asociadas": aso, "definitivas": de,
            "estudios": est, "ia": ia, "equipos": eq,
            "tb_alm": ta, "tb_disp": td, "ram": ram,
        })

    salida = []
    for p, d in prov.items():
        salida.append({
            "provincia": p,
            "centroide": CENTROIDES.get(p),
            "n_hospitales": len(d["hospitales"]),
            "estudios": d["estudios"], "admitidas": d["admitidas"],
            "asociadas": d["asociadas"], "definitivas": d["definitivas"],
            "ia": d["ia"], "equipos": d["equipos"],
            "tb_alm": round(d["tb_alm"], 2), "tb_disp": round(d["tb_disp"], 2),
            "ram": round(d["ram_sum"] / d["ram_n"], 1) if d["ram_n"] else None,
            "hospitales": sorted(d["hospitales"], key=lambda h: -h["estudios"]),
        })
    salida.sort(key=lambda x: -x["estudios"])
    return salida


@router.get("/api/provincias")
def provincias():
    try:
        filas = _leer_filas()
    except FileNotFoundError:
        return {"error": f"No se encontro la fuente ({CSV_PATH})", "provincias": []}
    return {"provincias": _agrupar(filas)}