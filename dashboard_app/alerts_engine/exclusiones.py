"""
Motor de exclusiones: reglas de supresión de alertas (ruido conocido,
mantenimiento, discos que viven al 92% por diseño, etc.). Autocontenido --
no depende de ningún detector puntual, cualquier detector nuevo lo atraviesa
automáticamente porque `estado.actualizar_estado_alerta` lo consulta primero.

Ver docs/09-plan-refactor-alertas.md.
"""
import re
from datetime import datetime

import database

# Cache de exclusiones: se refresca una vez por tick, no por hospital.
_EXCLUSIONES_CACHE = []
_EXCLUSIONES_TS = None

_ORDEN_NIVEL = {"OK": 0, "NOTICE": 1, "WARNING": 2, "CRITICAL": 3}


def cargar_exclusiones(db):
    """
    Trae las reglas activas y no vencidas, ya compiladas.
    Se llama UNA vez por tick desde el orquestador (procesar_offline).
    """
    global _EXCLUSIONES_CACHE, _EXCLUSIONES_TS
    ahora = datetime.now()
    reglas = []

    try:
        filas = db.query(database.AlertExclusionModel).filter(
            database.AlertExclusionModel.enabled == True  # noqa: E712
        ).all()
    except Exception as e:
        print(f"⚠️ [Exclusiones] No se pudieron cargar: {repr(e)}")
        _EXCLUSIONES_CACHE = []
        return []

    for f in filas:
        # Vencidas: se ignoran en caliente, no se borran (queda la auditoría)
        if f.expires_at and f.expires_at <= ahora:
            continue

        compilado = None
        if f.modo_match == "regex":
            try:
                compilado = re.compile(f.patron, re.IGNORECASE)
            except re.error as e:
                print(f"⚠️ [Exclusiones] Regla #{f.id} con regex inválida, se saltea: {e}")
                continue

        reglas.append({
            "id": f.id,
            "hospital_id": (f.hospital_id or "*").strip(),
            "patron": (f.patron or "").strip(),
            "patron_lower": (f.patron or "").strip().lower(),
            "modo_match": f.modo_match or "prefix",
            "accion": f.accion or "total",
            "nivel_max": _ORDEN_NIVEL.get(f.nivel_max, 3),
            "rx": compilado,
        })

    _EXCLUSIONES_CACHE = reglas
    _EXCLUSIONES_TS = ahora
    return reglas


def _match_patron(tipo, regla):
    t = (tipo or "").lower()
    p = regla["patron_lower"]
    modo = regla["modo_match"]

    if modo == "exact":
        return t == p
    if modo == "contains":
        return p in t
    if modo == "regex":
        return bool(regla["rx"] and regla["rx"].search(tipo or ""))
    # default: prefix
    return t.startswith(p)


def evaluar_exclusion(hid, tipo, nivel):
    """
    Devuelve la regla que aplica, o None.

    Gana la primera que matchea por especificidad: las reglas de hospital
    puntual se evalúan antes que las globales ('*'), así una regla amplia
    no pisa el criterio fino de un hospital.
    """
    if nivel == "OK" or not _EXCLUSIONES_CACHE:
        return None

    sev = _ORDEN_NIVEL.get(nivel, 3)

    especificas = [r for r in _EXCLUSIONES_CACHE if r["hospital_id"] == hid]
    globales = [r for r in _EXCLUSIONES_CACHE if r["hospital_id"] == "*"]

    for regla in especificas + globales:
        if sev > regla["nivel_max"]:
            continue  # el hallazgo es más grave de lo que la regla suprime
        if _match_patron(tipo, regla):
            return regla
    return None


def _registrar_hit(db, regla_id):
    """Contador de uso de la regla. Best-effort: si falla, no rompe el tick."""
    try:
        r = db.query(database.AlertExclusionModel).filter_by(id=regla_id).first()
        if r:
            r.hits = (r.hits or 0) + 1
            r.last_hit = datetime.now()
            db.commit()
    except Exception:
        try:
            db.rollback()
        except Exception:
            pass
