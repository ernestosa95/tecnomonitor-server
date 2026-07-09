"""
informes_ia_router.py
======================

Router NUEVO y AISLADO para el módulo informes_ia.
No modifica dashboard.py, auth.py ni database.py.

Se monta en server.py (Fase 2) con:
    from informes_ia_router import router as informes_ia_router
    app.include_router(informes_ia_router)

El `ServicioReportes` (instancia real del módulo) se construye una sola vez
en el lifespan de server.py y se guarda en `app.state.servicio_reportes_ia`.
Este router NUNCA lo instancia — solo lo consume vía `request.app.state`.
Así, si informes_ia falla al montarse (falta GEMINI_API_KEY, etc.), el
resto de la app sigue funcionando y este router responde 503 en vez de
tumbar el proceso completo (mismo patrón de circuit breaker que ya usan
con Asana).

# ============================================================================
# ⚠️ DOS SUPUESTOS A CONFIRMAR ANTES DE MONTAR ESTE ROUTER EN server.py:
#
# 1. ROLES: asumo que "Admin", "Ingenieria" y "Comercial" pueden SOLICITAR
#    y VER informes IA, pero que solo "Admin" puede APROBARLOS (paso
#    irreversible). Si el criterio real es otro, ajustar las tuplas en
#    `require_roles(...)` de cada endpoint más abajo.
#
# 2. NOMBRE DE USUARIO: como auth.get_current_user() solo devuelve
#    {"email","role"} (sin id ni full_name), este router hace una consulta
#    extra a UserModel para completar {"id","nombre","rol"} que pide
#    informes_ia. Si preferís evitar esa query extra, se puede cachear en
#    otro lado — pero por ahora prioricé no tocar auth.py.
# ============================================================================
"""

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

import auth
import database

# EstadoInvalido es la excepción tipada que define el propio módulo
# informes_ia (ver guía de integración, sección 5).
from informes_ia.historial.servicio import EstadoInvalido

router = APIRouter(prefix="/api/informes-ia", tags=["informes_ia"])


# ----------------------------------------------------------------------------
# DB local, mismo patrón que ya usa dashboard.py (no comparte sesión con
# reportes_db.sqlite, que es interna del módulo informes_ia).
# ----------------------------------------------------------------------------
def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ----------------------------------------------------------------------------
# Acceso al servicio ya montado en server.py. Si no está disponible
# (informes_ia no pudo inicializarse), responde 503 en vez de un 500 feo.
# ----------------------------------------------------------------------------
def get_servicio(request: Request):
    servicio = getattr(request.app.state, "servicio_reportes_ia", None)
    if servicio is None:
        raise HTTPException(
            status_code=503,
            detail="El módulo de informes IA no está disponible en este momento.",
        )
    return servicio


def get_almacen(request: Request):
    almacen = getattr(request.app.state, "almacen_reportes_ia", None)
    if almacen is None:
        raise HTTPException(
            status_code=503,
            detail="El módulo de informes IA no está disponible en este momento.",
        )
    return almacen


def _usuario_para_informes_ia(current_user: dict, db: Session) -> dict:
    """
    Adapta el current_user de auth.py ({"email","role"}) al formato que
    espera informes_ia ({"id","nombre","rol"}).
    """
    user = db.query(database.UserModel).filter(
        database.UserModel.email == current_user["email"]
    ).first()
    if not user:
        # No debería pasar (get_current_user ya validó contra la DB), pero
        # por las dudas no reventamos el endpoint por esto.
        return {"id": current_user["email"], "nombre": current_user["email"],
                "rol": current_user["role"]}
    return {"id": user.id, "nombre": user.full_name or user.email, "rol": user.role}


# ----------------------------------------------------------------------------
# DTOs
# ----------------------------------------------------------------------------
class PeticionInformeDTO(BaseModel):
    hospital_id: str
    fecha_inicio: str   # "YYYY-MM-DD HH:MM:SS", tal como pide la guía
    fecha_fin: str
    tipo_reporte: str   # "cliente" | "interno"


class JsonEditadoDTO(BaseModel):
    # El módulo acepta el JSON tal cual (no valida qué campos se editaron:
    # eso lo controla la app). Ver sección 4.3 / 9 de la guía.
    data: Dict[str, Any]


# ----------------------------------------------------------------------------
# Endpoints — 1:1 con la tabla de la sección 7 de la guía
# ----------------------------------------------------------------------------

@router.post("/reportes", status_code=202)
def solicitar_reporte(
    peticion: PeticionInformeDTO,
    db: Session = Depends(get_db),
    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial")),
    servicio=Depends(get_servicio),
):
    usuario = _usuario_para_informes_ia(current_user, db)
    report_id = servicio.solicitar_reporte(peticion.model_dump(), solicitado_por=usuario)
    return {"report_id": report_id, "estado": "en_espera"}


@router.get("/reportes/{report_id}/estado")
def consultar_estado(
    report_id: str,
    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial")),
    servicio=Depends(get_servicio),
):
    estado = servicio.consultar_estado(report_id)
    if estado is None:
        raise HTTPException(404, "No existe el reporte")
    return {"report_id": report_id, "estado": estado}


@router.get("/reportes/{report_id}/json")
def obtener_json(
    report_id: str,
    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial")),
    servicio=Depends(get_servicio),
):
    try:
        return servicio.obtener_json(report_id)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))


@router.put("/reportes/{report_id}/json")
def guardar_json_editado(
    report_id: str,
    body: JsonEditadoDTO,
    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial")),
    servicio=Depends(get_servicio),
):
    try:
        servicio.guardar_json_editado(report_id, body.data)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))
    return {"status": "ok"}


@router.post("/reportes/{report_id}/aprobar")
def aprobar(
    report_id: str,
    db: Session = Depends(get_db),
    # ⚠️ Ver supuesto (1) arriba: aprobar es el paso irreversible, lo dejé
    # restringido solo a Admin. Ajustar si corresponde otro rol (ej. Gerencia).
    current_user: dict = Depends(auth.require_roles("Admin")),
    servicio=Depends(get_servicio),
):
    aprobador = _usuario_para_informes_ia(current_user, db)
    try:
        servicio.aprobar(report_id, aprobado_por=aprobador)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))
    return {"status": "ok", "estado": "aprobado"}


@router.get("/reportes/{report_id}/pdf")
def obtener_pdf(
    report_id: str,
    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial")),
    servicio=Depends(get_servicio),
):
    from fastapi.responses import Response

    try:
        pdf_bytes = servicio.obtener_reporte(report_id)
    except KeyError:
        raise HTTPException(404, "No existe el reporte")
    except EstadoInvalido as e:
        raise HTTPException(409, str(e))
    return Response(content=pdf_bytes, media_type="application/pdf")


@router.get("/reportes")
def listar_reportes(
    solicitante: str | None = None,
    current_user: dict = Depends(auth.require_roles("Admin", "Ingenieria", "Comercial")),
    almacen=Depends(get_almacen),
):
    if solicitante:
        reportes = almacen.listar_por_solicitante(solicitante)
    else:
        reportes = almacen.listar()
    return [
        {
            "report_id": r.report_id,
            "estado": r.estado,
            "peticion": r.peticion,
            "solicitado_por": r.solicitado_por,
            "aprobado_por": r.aprobado_por,
            "aprobado_en": r.aprobado_en,
            "creado_en": r.creado_en,
            "actualizado_en": r.actualizado_en,
        }
        for r in reportes
    ]