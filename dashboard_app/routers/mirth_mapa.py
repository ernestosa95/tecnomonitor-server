"""
Endpoint de lectura del mapa de integraciones Mirth: arma
orígenes/destinos/canales/línea de tiempo a partir de la topología curada
(mirth_nodos/mirth_canales_meta), el snapshot técnico
(mirth_channel_topology) y el histórico de software_monitoring.

Shape del response pensado 1:1 contra el modelo JS del prototipo
(ORIGENES/DESTINOS/CANALES/TL/UMBRALES) -- ver docs/13-contrato-topologia-mirth.md.
"""
import hashlib
import json
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

import auth
import database
from alerts_engine.config import cargar_config
from alerts_engine.software.mirth import _umbrales
from core import get_db
from routers.mirth_topologia import _armar_inventario_canales

router = APIRouter()

_PASOS_MAX = 288  # tope defensivo (24h a paso=5min) contra un query param abusivo


def _parsear_ts(v):
    if isinstance(v, datetime):
        return v
    if not v:
        return None
    s = str(v)[:26].replace("T", " ")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _extra(row):
    try:
        return json.loads(row.extra_data) if row.extra_data else {}
    except (TypeError, ValueError):
        return {}


def _identidad_de(row, component_a_channel):
    extra = _extra(row)
    cid = extra.get("channel_id")
    if cid:
        return cid, extra
    cid = component_a_channel.get(row.component_id)
    if cid:
        return cid, extra
    return "cid_" + hashlib.sha1(row.component_id.encode("utf-8")).hexdigest()[:8], extra


def _mapa_padres(topo_por_channel):
    """target_channel_id -> [channel_id que le escribe vía Channel Writer, ...]"""
    padres = {}
    for cid, t in topo_por_channel.items():
        for d in (t.destinos or []):
            target = d.get("target_channel_id")
            if target:
                padres.setdefault(target, []).append(cid)
    return padres


def _bucketizar(filas_por_canal, t0, paso_min, pasos):
    paso_seg = paso_min * 60
    resultado = {}
    for ident, filas in filas_por_canal.items():
        por_bucket = {}
        for row, extra in filas:
            ts = _parsear_ts(row.timestamp)
            if ts is None:
                continue
            idx = int((ts - t0).total_seconds() // paso_seg)
            if 0 <= idx < pasos:
                por_bucket[idx] = (row, extra)  # último en el bucket gana (filas vienen ASC)

        serie = []
        prev_r = prev_s = prev_err = None
        estado_actual, cola_actual = "UNKNOWN", 0
        for idx in range(pasos):
            entrada = por_bucket.get(idx)
            if entrada is not None:
                row, extra = entrada
                r, s = extra.get("recibidos", 0), extra.get("enviados", 0)
                err = extra.get("errored", 0)
                delta_r = (r - prev_r) if prev_r is not None and r >= prev_r else 0
                delta_s = (s - prev_s) if prev_s is not None and s >= prev_s else 0
                delta_err = (err - prev_err) if prev_err is not None and err >= prev_err else 0
                prev_r, prev_s, prev_err = r, s, err
                estado_actual = (row.status_value or "UNKNOWN").upper()
                cola_actual = row.metric_value or 0
                serie.append({"estado": estado_actual, "cola": cola_actual,
                               "trafico": delta_r + delta_s, "rx": delta_r, "tx": delta_s,
                               "err": delta_err, "fresco": True})
            else:
                serie.append({"estado": estado_actual, "cola": cola_actual,
                               "trafico": 0, "rx": 0, "tx": 0, "err": 0, "fresco": False})
        resultado[ident] = serie
    return resultado


def _hospital_offline(db, config, hospital_id):
    """Mismo criterio que alerts_engine.infra._verificar_conectividad (reportes_historicos
    vs offline_minutes, contra la hora del servidor) -- para que el mapa no contradiga la
    señal OFFLINE que ya usa el resto del dashboard."""
    last = db.execute(
        text("SELECT timestamp FROM reportes_historicos WHERE hospital_id = :hid ORDER BY timestamp DESC LIMIT 1"),
        {"hid": hospital_id},
    ).fetchone()
    if not last:
        return False
    ts = _parsear_ts(last.timestamp)
    if ts is None:
        return False
    return (datetime.now() - ts) > timedelta(minutes=config.get("offline_minutes", 15))


@router.get("/api/hospital/{hospital_id}/mirth/mapa")
def obtener_mapa_mirth(hospital_id: str, minutos: int = 180, paso: int = 5, incluir_auto: int = 1,
                       db: Session = Depends(get_db),
                       current_user: dict = Depends(auth.require_hospital_access("software"))):
    config = cargar_config(db)
    umbrales = _umbrales(config)
    stale_min = config.get("mirth_stale_minutes", 15)

    paso = max(1, paso)
    pasos = max(1, min(_PASOS_MAX, minutos // paso))
    ahora = datetime.now()
    t0 = ahora - timedelta(minutes=paso * pasos)

    hosp = db.query(database.HospitalMetadata).filter_by(hospital_id=hospital_id).first()

    # --- Tablas de curación + snapshot técnico (pocas filas, todo el hospital) ---
    nodos_curados = db.query(database.MirthNodo).filter_by(hospital_id=hospital_id, activo=True).order_by(
        database.MirthNodo.orden
    ).all()
    nodos_por_id = {n.id: n for n in nodos_curados}
    meta_por_channel = {m.channel_id: m for m in db.query(database.MirthCanalMeta).filter_by(hospital_id=hospital_id).all()}
    topo_por_channel = {t.channel_id: t for t in db.query(database.MirthChannelTopology).filter_by(hospital_id=hospital_id).all()}
    component_a_channel = {t.component_id: t.channel_id for t in topo_por_channel.values()}
    padres_por_target = _mapa_padres(topo_por_channel)

    # --- Serie temporal (una query, ventana completa, igual patrón que /software) ---
    filas = db.execute(text("""
        SELECT component_id, status_value, metric_value, extra_data, timestamp
        FROM software_monitoring
        WHERE hospital_id = :hid AND LOWER(app_name) LIKE '%mirth%' AND timestamp >= :t0
        ORDER BY timestamp ASC
        LIMIT 20000
    """), {"hid": hospital_id, "t0": t0}).fetchall()

    filas_por_canal = {}       # identidad -> [(row, extra), ...] ASC
    ultimo_ts_hospital = None
    for row in filas:
        ident, extra = _identidad_de(row, component_a_channel)
        filas_por_canal.setdefault(ident, []).append((row, extra))
        ts = _parsear_ts(row.timestamp)
        if ts and (ultimo_ts_hospital is None or ts > ultimo_ts_hospital):
            ultimo_ts_hospital = ts

    tl_por_canal = _bucketizar(filas_por_canal, t0, paso, pasos)

    # Ajuste del último bucket ("vivo"): usa stale_min (más laxo que el
    # tamaño de bucket) en vez de "hubo fila en los últimos `paso` minutos"
    # -- si el hospital reporta cada 10 min y paso=5, la mitad de los
    # buckets no tendrían fila aunque todo esté sano. El resto de la serie
    # (para el scrubber/sparkline) sí usa el criterio estricto por bucket.
    for ident, filas_ident in filas_por_canal.items():
        ultimo_ts_canal = _parsear_ts(filas_ident[-1][0].timestamp)
        fresco_vivo = bool(
            ultimo_ts_canal and ultimo_ts_hospital
            and (ultimo_ts_hospital - ultimo_ts_canal) <= timedelta(minutes=stale_min)
        )
        if tl_por_canal.get(ident):
            tl_por_canal[ident][-1]["fresco"] = fresco_vivo

    # --- Ensamblado de orígenes/destinos/canales ---
    origenes_out = {n.clave: {"id": n.clave, "label": n.label, "sub": n.sub,
                               "humano": n.humano, "vm": n.vm, "auto": False}
                    for n in nodos_curados if n.tipo == "origen"}
    destinos_out = {n.clave: {"id": n.clave, "label": n.label, "sub": n.sub,
                               "humano": n.humano, "vm": n.vm, "auto": False}
                    for n in nodos_curados if n.tipo == "destino"}

    def _nodo_auto(bolsa, tipo, endpoint):
        clave = f"auto:{tipo}:{endpoint}"
        if clave not in bolsa:
            bolsa[clave] = {"id": clave, "label": endpoint, "sub": endpoint,
                             "humano": "Sin clasificar", "vm": None, "auto": True}
        return clave

    canales_out = []
    identidades_con_topo = set()

    for cid, t in topo_por_channel.items():
        identidades_con_topo.add(cid)
        meta = meta_por_channel.get(cid)
        if meta and meta.oculto:
            continue

        origen_clave = None
        if meta and meta.nodo_origen_id and meta.nodo_origen_id in nodos_por_id:
            origen_clave = nodos_por_id[meta.nodo_origen_id].clave
        elif incluir_auto and t.source_endpoint:
            origen_clave = _nodo_auto(origenes_out, "origen", t.source_endpoint)

        destino_endpoint = next((d.get("endpoint") for d in (t.destinos or []) if d.get("endpoint")), None)
        destino_clave = None
        if meta and meta.nodo_destino_id and meta.nodo_destino_id in nodos_por_id:
            destino_clave = nodos_por_id[meta.nodo_destino_id].clave
        elif incluir_auto and destino_endpoint:
            destino_clave = _nodo_auto(destinos_out, "destino", destino_endpoint)

        padres = padres_por_target.get(cid, [])

        canales_out.append({
            "id": cid,
            "component_id": t.component_id,
            "instancia": t.instancia,
            "nom": t.nombre,
            "hum": (meta.hum if meta else None) or None,
            "origen": origen_clave,
            "destino": destino_clave,
            "padre": padres[0] if padres else None,
            "padres": padres,
            "crit": (meta.crit if meta else None) or config.get("mirth_crit_default", "media"),
            "clasificado": meta is not None,
            "endpoint_origen": t.source_endpoint,
            "endpoint_destino": destino_endpoint,
            "tipo_alerta": f"MIRTH_{t.component_id[:35]}",
        })

    # Canales que reportan (tienen serie) pero no tienen topología técnica
    # todavía (agente viejo, o topología no llegó en este ciclo) -- el mapa
    # los sigue mostrando en modo degradado: sin padre, sin endpoints.
    for ident in filas_por_canal:
        if ident in identidades_con_topo:
            continue
        meta = meta_por_channel.get(ident)
        if meta and meta.oculto:
            continue
        ultima_row = filas_por_canal[ident][-1][0]
        canales_out.append({
            "id": ident,
            "component_id": ultima_row.component_id,
            "instancia": _extra(ultima_row).get("instancia", "Default"),
            "nom": ultima_row.component_id,
            "hum": (meta.hum if meta else None) or None,
            "origen": None,
            "destino": None,
            "padre": None,
            "padres": [],
            "crit": (meta.crit if meta else None) or config.get("mirth_crit_default", "media"),
            "clasificado": meta is not None,
            "endpoint_origen": None,
            "endpoint_destino": None,
            "tipo_alerta": f"MIRTH_{ultima_row.component_id[:35]}",
        })

    tl_out = []
    for idx in range(pasos):
        ts_bucket = t0 + timedelta(minutes=paso * idx)
        ch = {c["id"]: tl_por_canal.get(c["id"], [{}] * pasos)[idx] for c in canales_out}
        tl_out.append({"ts": ts_bucket.isoformat(), "ch": ch})

    inventario = _armar_inventario_canales(hospital_id, db)

    return {
        "hospital": {
            "id": hospital_id,
            "nombre": hosp.nombre if hosp else hospital_id,
            "instancias": sorted({c["instancia"] for c in canales_out}) or ["Default"],
        },
        "umbrales": umbrales,
        "meta": {
            "minutos": minutos, "paso_min": paso, "pasos": pasos,
            "t0": t0.isoformat(), "generado_en": ahora.isoformat(),
            "stale_min": stale_min,
            "hospital_offline": _hospital_offline(db, config, hospital_id),
            "topologia_disponible": bool(topo_por_channel),
            "sin_clasificar": inventario["resumen"]["sin_clasificar"],
        },
        "origenes": list(origenes_out.values()),
        "destinos": list(destinos_out.values()),
        "canales": canales_out,
        "tl": tl_out,
    }
