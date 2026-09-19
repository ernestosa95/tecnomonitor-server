"""
Administración del mapa de integraciones Mirth: nodos curados (sistemas
origen/destino) y metadata de canal (criticidad, nombre humano, asignación
a nodos). Mismo patrón CRUD que hospitales_metadata.py.

Ver docs/13-contrato-topologia-mirth.md.
"""
import re
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

import auth
import database
from core import get_db

router = APIRouter()

_CLAVE_RE = re.compile(r'^[a-z0-9_-]{1,32}$')


# --- DTOs ---

class NodoDTO(BaseModel):
    tipo: Literal["origen", "destino"]
    clave: str
    label: str
    sub: Optional[str] = None
    humano: Optional[str] = None
    vm: Optional[str] = None
    orden: int = 0
    activo: bool = True

    @field_validator("clave")
    @classmethod
    def _clave_valida(cls, v):
        if not _CLAVE_RE.match(v):
            raise ValueError("clave debe ser minúsculas/números/guiones/guion_bajo, 1-32 caracteres")
        return v


class CanalMetaDTO(BaseModel):
    instancia: str = "Default"
    hum: Optional[str] = None
    crit: Literal["alta", "media", "baja"] = "media"
    nodo_origen_id: Optional[int] = None
    nodo_destino_id: Optional[int] = None
    oculto: bool = False
    notas: Optional[str] = None


class AdoptarNodoDTO(BaseModel):
    tipo: Literal["origen", "destino"]
    clave: str
    label: str
    sub: Optional[str] = None
    humano: Optional[str] = None
    channel_ids: List[str] = []


# --- Helpers ---

def _hospital_o_404(db, hid):
    h = db.query(database.HospitalMetadata).filter_by(hospital_id=hid).first()
    if not h:
        raise HTTPException(status_code=404, detail="Hospital no encontrado")
    return h


def _nodo_dict(f):
    return {
        "id": f.id, "tipo": f.tipo, "clave": f.clave, "label": f.label,
        "sub": f.sub, "humano": f.humano, "vm": f.vm, "orden": f.orden, "activo": f.activo,
    }


def _meta_dict(m):
    return {
        "hum": m.hum, "crit": m.crit,
        "nodo_origen_id": m.nodo_origen_id, "nodo_destino_id": m.nodo_destino_id,
        "oculto": m.oculto, "notas": m.notas,
    }


def _sugerencia_nodo(topo_fila):
    """A partir del endpoint técnico reportado, sugiere una clave/label para
    "Adoptar" un nodo nuevo -- no crea nada, solo precarga el formulario."""
    sug = {}
    if topo_fila.source_endpoint:
        sug["origen"] = {"clave_auto": f"auto:origen:{topo_fila.source_endpoint}",
                          "label": topo_fila.source_endpoint}
    for d in (topo_fila.destinos or []):
        if d.get("endpoint"):
            sug["destino"] = {"clave_auto": f"auto:destino:{d['endpoint']}", "label": d["endpoint"]}
            break
    return sug or None


def _ultimo_estado_por_component(db, hid):
    filas = db.execute(text("""
        WITH RankedData AS (
            SELECT component_id, status_value, metric_value, timestamp,
                   ROW_NUMBER() OVER(PARTITION BY component_id ORDER BY timestamp DESC) as rn
            FROM software_monitoring
            WHERE hospital_id = :hid AND LOWER(app_name) LIKE '%mirth%'
        )
        SELECT component_id, status_value, metric_value, timestamp
        FROM RankedData WHERE rn = 1
    """), {"hid": hid}).fetchall()
    return {
        f.component_id: {
            "status": f.status_value, "queued": f.metric_value,
            "visto": f.timestamp.isoformat() if hasattr(f.timestamp, "isoformat") else f.timestamp,
        }
        for f in filas
    }


def _armar_inventario_canales(hid, db):
    topo_por_channel = {t.channel_id: t for t in db.query(database.MirthChannelTopology).filter_by(hospital_id=hid).all()}
    meta_por_channel = {m.channel_id: m for m in db.query(database.MirthCanalMeta).filter_by(hospital_id=hid).all()}
    ultimo_por_component = _ultimo_estado_por_component(db, hid)

    vistos = set()
    canales = []

    for cid, t in topo_por_channel.items():
        vistos.add(cid)
        meta = meta_por_channel.get(cid)
        canales.append({
            "channel_id": cid,
            "instancia": t.instancia,
            "nombre": t.nombre,
            "clasificado": meta is not None,
            "meta": _meta_dict(meta) if meta else None,
            "tecnico": {
                "source_transport": t.source_transport,
                "source_endpoint": t.source_endpoint,
                "destinos": t.destinos,
                "last_seen": t.last_seen.isoformat() if hasattr(t.last_seen, "isoformat") else t.last_seen,
            },
            "ultimo_estado": ultimo_por_component.get(t.component_id),
            "sugerencia": _sugerencia_nodo(t),
        })

    # Canales curados que ya no aparecen en la topología actual (agente que
    # dejó de reportarlos, o curados a mano sin topología nunca reportada).
    for cid, meta in meta_por_channel.items():
        if cid in vistos:
            continue
        canales.append({
            "channel_id": cid,
            "instancia": meta.instancia,
            "nombre": meta.nombre_tecnico or cid,
            "clasificado": True,
            "meta": _meta_dict(meta),
            "tecnico": None,
            "ultimo_estado": None,
            "sugerencia": None,
        })

    total = len(canales)
    clasificados = sum(1 for c in canales if c["clasificado"])
    return {
        "canales": canales,
        "resumen": {"total": total, "clasificados": clasificados, "sin_clasificar": total - clasificados},
    }


# --- Nodos ---

@router.get("/api/hospital/{hid}/mirth/nodos")
def listar_nodos(hid: str, db: Session = Depends(get_db),
                  current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    filas = db.query(database.MirthNodo).filter_by(hospital_id=hid).order_by(
        database.MirthNodo.tipo, database.MirthNodo.orden
    ).all()
    return [_nodo_dict(f) for f in filas]


@router.post("/api/hospital/{hid}/mirth/nodos")
def crear_nodo(hid: str, req: NodoDTO, db: Session = Depends(get_db),
               current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    _hospital_o_404(db, hid)
    existe = db.query(database.MirthNodo).filter_by(hospital_id=hid, tipo=req.tipo, clave=req.clave).first()
    if existe:
        raise HTTPException(status_code=400, detail=f"Ya existe un nodo {req.tipo} con clave '{req.clave}'")
    nuevo = database.MirthNodo(hospital_id=hid, **req.model_dump())
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    return {"status": "ok", "id": nuevo.id}


@router.put("/api/hospital/{hid}/mirth/nodos/{nodo_id}")
def editar_nodo(hid: str, nodo_id: int, req: NodoDTO, db: Session = Depends(get_db),
                current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    n = db.query(database.MirthNodo).filter_by(id=nodo_id, hospital_id=hid).first()
    if not n:
        raise HTTPException(status_code=404, detail="Nodo no encontrado")
    if (req.tipo, req.clave) != (n.tipo, n.clave):
        dup = db.query(database.MirthNodo).filter_by(hospital_id=hid, tipo=req.tipo, clave=req.clave).first()
        if dup and dup.id != nodo_id:
            raise HTTPException(status_code=400, detail=f"Ya existe un nodo {req.tipo} con clave '{req.clave}'")
    n.tipo, n.clave, n.label = req.tipo, req.clave, req.label
    n.sub, n.humano, n.vm = req.sub, req.humano, req.vm
    n.orden, n.activo = req.orden, req.activo
    db.commit()
    return {"status": "ok"}


@router.patch("/api/hospital/{hid}/mirth/nodos/{nodo_id}/toggle")
def toggle_nodo(hid: str, nodo_id: int, db: Session = Depends(get_db),
                current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    n = db.query(database.MirthNodo).filter_by(id=nodo_id, hospital_id=hid).first()
    if not n:
        raise HTTPException(status_code=404, detail="Nodo no encontrado")
    n.activo = not n.activo
    db.commit()
    return {"status": "ok", "activo": n.activo}


@router.delete("/api/hospital/{hid}/mirth/nodos/{nodo_id}")
def borrar_nodo(hid: str, nodo_id: int, db: Session = Depends(get_db),
                current_user: dict = Depends(auth.require_roles("Admin"))):
    n = db.query(database.MirthNodo).filter_by(id=nodo_id, hospital_id=hid).first()
    if not n:
        raise HTTPException(status_code=404, detail="Nodo no encontrado")
    # SQLite no enforcea FKs (database.py:24-29) -- nulear referencias a mano.
    db.query(database.MirthCanalMeta).filter_by(hospital_id=hid, nodo_origen_id=nodo_id).update({"nodo_origen_id": None})
    db.query(database.MirthCanalMeta).filter_by(hospital_id=hid, nodo_destino_id=nodo_id).update({"nodo_destino_id": None})
    db.delete(n)
    db.commit()
    return {"status": "ok"}


@router.post("/api/hospital/{hid}/mirth/nodos/adoptar")
def adoptar_nodo(hid: str, req: AdoptarNodoDTO, db: Session = Depends(get_db),
                 current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    """Crea un nodo curado a partir de una sugerencia 'auto' y lo asigna de
    una a todos los channel_ids indicados (crea la fila de curación de esos
    canales si todavía no existía)."""
    _hospital_o_404(db, hid)
    existe = db.query(database.MirthNodo).filter_by(hospital_id=hid, tipo=req.tipo, clave=req.clave).first()
    if existe:
        raise HTTPException(status_code=400, detail=f"Ya existe un nodo {req.tipo} con clave '{req.clave}'")

    nodo = database.MirthNodo(hospital_id=hid, tipo=req.tipo, clave=req.clave,
                               label=req.label, sub=req.sub, humano=req.humano)
    db.add(nodo)
    db.flush()  # para tener nodo.id sin cerrar la transacción

    campo = "nodo_origen_id" if req.tipo == "origen" else "nodo_destino_id"
    asignados = 0
    for cid in req.channel_ids:
        fila = db.query(database.MirthCanalMeta).filter_by(hospital_id=hid, channel_id=cid).first()
        if fila is None:
            topo = db.query(database.MirthChannelTopology).filter_by(hospital_id=hid, channel_id=cid).first()
            fila = database.MirthCanalMeta(
                hospital_id=hid, instancia=topo.instancia if topo else "Default",
                channel_id=cid, nombre_tecnico=topo.nombre if topo else None,
                updated_by=current_user.get("email"),
            )
            db.add(fila)
        setattr(fila, campo, nodo.id)
        fila.updated_by = current_user.get("email")
        asignados += 1

    db.commit()
    return {"status": "ok", "nodo_id": nodo.id, "canales_asignados": asignados}


# --- Canales ---

@router.get("/api/hospital/{hid}/mirth/canales")
def listar_canales(hid: str, db: Session = Depends(get_db),
                   current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    return _armar_inventario_canales(hid, db)


@router.get("/api/hospital/{hid}/mirth/sin-clasificar")
def canales_sin_clasificar(hid: str, db: Session = Depends(get_db),
                           current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    data = _armar_inventario_canales(hid, db)
    huerfanos = [c for c in data["canales"] if not c["clasificado"]]
    return {"canales": huerfanos, "total": len(huerfanos)}


@router.put("/api/hospital/{hid}/mirth/canales/{channel_id}")
def curar_canal(hid: str, channel_id: str, req: CanalMetaDTO, db: Session = Depends(get_db),
                current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    _hospital_o_404(db, hid)

    if req.nodo_origen_id is not None:
        n = db.query(database.MirthNodo).filter_by(id=req.nodo_origen_id, hospital_id=hid, tipo="origen").first()
        if not n:
            raise HTTPException(status_code=400, detail="nodo_origen_id inválido para este hospital")
    if req.nodo_destino_id is not None:
        n = db.query(database.MirthNodo).filter_by(id=req.nodo_destino_id, hospital_id=hid, tipo="destino").first()
        if not n:
            raise HTTPException(status_code=400, detail="nodo_destino_id inválido para este hospital")

    topo = db.query(database.MirthChannelTopology).filter_by(hospital_id=hid, channel_id=channel_id).first()
    fila = db.query(database.MirthCanalMeta).filter_by(
        hospital_id=hid, instancia=req.instancia, channel_id=channel_id
    ).first()

    if fila is None:
        fila = database.MirthCanalMeta(
            hospital_id=hid, instancia=req.instancia, channel_id=channel_id,
            nombre_tecnico=topo.nombre if topo else None,
            updated_by=current_user.get("email"),
        )
        db.add(fila)
    else:
        fila.nombre_tecnico = topo.nombre if topo else fila.nombre_tecnico
        fila.updated_by = current_user.get("email")

    fila.hum = req.hum
    fila.crit = req.crit
    fila.nodo_origen_id = req.nodo_origen_id
    fila.nodo_destino_id = req.nodo_destino_id
    fila.oculto = req.oculto
    fila.notas = req.notas

    db.commit()
    return {"status": "ok"}


@router.delete("/api/hospital/{hid}/mirth/canales/{channel_id}")
def borrar_curacion_canal(hid: str, channel_id: str, db: Session = Depends(get_db),
                          current_user: dict = Depends(auth.require_roles("Admin"))):
    fila = db.query(database.MirthCanalMeta).filter_by(hospital_id=hid, channel_id=channel_id).first()
    if not fila:
        raise HTTPException(status_code=404, detail="No hay curación para este canal")
    db.delete(fila)
    db.commit()
    return {"status": "ok"}


# --- Resumen global ---

@router.get("/api/mirth/pendientes")
def pendientes_globales(db: Session = Depends(get_db),
                        current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria"))):
    resultado = {}
    for h in db.query(database.HospitalMetadata).filter_by(is_visible=True).all():
        data = _armar_inventario_canales(h.hospital_id, db)
        if data["resumen"]["sin_clasificar"] > 0:
            resultado[h.hospital_id] = data["resumen"]["sin_clasificar"]
    return {"pendientes_por_hospital": resultado, "total": sum(resultado.values())}
