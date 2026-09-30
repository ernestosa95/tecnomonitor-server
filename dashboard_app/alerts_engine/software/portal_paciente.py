"""
Portal paciente: cola de publicación RIS + MPS (REQ-07, agente 4.5.4). Ver docs/16 (REQ-07) y
docs/10-contrato-ingesta-agente.md §7.7.

El agente manda en cada ciclo, por estado, cuántos estudios hay en el RIS (`PublicationState`) y en
la cola del MPS (`ExtMPS.QUEUE`), sin decir qué significa cada estado. Acá se clasifica cada estado
como pendiente / error / final / no listo (`clasificar()`), así un código nuevo del MPS se resuelve
en el server sin recompilar el agente.

Criterio único (`estado_portal()`), que usan este detector y la tarjeta de la pestaña Software:
- DEMORADO: el estudio más antiguo en un estado pendiente del MPS lleva más de `portal_max_hours`
  en la cola (hora de entrada a la cola, `IN_TIME`). Solo el MPS: en el RIS la fecha disponible es
  la de admisión, no la de pedido de publicación, y exageraría la espera.
- BLOQUEOS: hay estudios en un estado de error del MPS que entraron a la cola en las últimas 24 h.
  Los bloqueados viejos se muestran, pero no alertan (si no, alertarían para siempre).
- SIN_LECTURA: la última lectura tiene más de LECTURA_VIGENTE_HORAS; no se evalúa (queda como
  estaba), igual que los backups.

Alertas (apagadas por default, `portal_alert_enabled`): PORTAL_DEMORA y PORTAL_BLOQUEOS, WARNING,
una por hospital, se cierran solas cuando la condición desaparece. Reusan los responsables de
Infraestructura ('global_alert_responsible_email').
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import text

from ..config import _followers_de
from ..estado import actualizar_estado_alerta

APP_NAME = "patient_portal"
LECTURA_VIGENTE_HORAS = 3      # el pipeline corre cada 5 min (o cada 1 h si se lo pasó a ese cajón)

PENDIENTE, ERROR, FINAL, NO_LISTO, OTRO = "pendiente", "error", "final", "no_listo", "otro"

# Clasificación confirmada con los datos reales (capturas del 2026-09-30). Un código que no está acá
# se clasifica por su descripción (_POR_PALABRA) y, si tampoco, queda como OTRO (se grafica, no alerta).
_POR_CODIGO = {
    ("RIS", "1"): FINAL,         # Published
    ("RIS", "2"): FINAL,         # Not published (sin acción pendiente)
    ("RIS", "3"): FINAL,         # Revoked (retirado del portal)
    ("RIS", "4"): PENDIENTE,     # To be published
    ("RIS", "5"): PENDIENTE,     # To be withdrawn (espera que lo retiren)
    ("RIS", "NULL"): NO_LISTO,   # el informe todavía no es definitivo: flujo normal
    ("MPS", "1"): PENDIENTE,     # IDLE: en cola, esperando la generación de la ISO
    ("MPS", "2"): PENDIENTE,     # PENDING
    ("MPS", "3"): PENDIENTE,     # CREATING: generando la ISO
    ("MPS", "4"): FINAL,         # DONE
    ("MPS", "5"): ERROR,         # FAILED
    ("MPS", "6"): ERROR,         # BLOCKED
    ("MPS", "7"): ERROR,         # ABORTED
    ("MPS", "9"): FINAL,         # BURNER: ISO generada y entregada
}
_POR_PALABRA = (
    (ERROR, ("ERROR", "FAIL", "BLOCK", "ABORT", "CANCEL")),
    (FINAL, ("PUBLISHED", "BURNER", "DONE", "COMPLETE", "SENT", "REVOKED")),
    (PENDIENTE, ("IDLE", "CREAT", "WAIT", "PEND", "QUEUE", "TO BE", "PROGRESS", "SENDING")),
)

# Clases que se dibujan en la línea de tiempo (las finales y "no listo" crecen con el uso normal y
# taparían a las que importan).
CLASES_GRAFICO = (PENDIENTE, ERROR, OTRO)

ESTADO_OK, ESTADO_DEMORADO, ESTADO_BLOQUEOS, ESTADO_SIN_LECTURA = "OK", "DEMORADO", "BLOQUEOS", "SIN_LECTURA"


def clasificar(origen, codigo, descripcion):
    clase = _POR_CODIGO.get((origen, str(codigo)))
    if clase:
        return clase
    desc = str(descripcion or "").upper()
    if origen == "RIS" and str(codigo) == "NULL":
        return NO_LISTO
    for clase, palabras in _POR_PALABRA:
        if any(p in desc for p in palabras):
            return clase
    return OTRO


def _orden_codigo(codigo):
    """Los códigos numéricos en orden numérico (3 antes que 12); el resto (NULL) al final."""
    return (0, int(codigo), "") if str(codigo).isdigit() else (1, 0, str(codigo))


def _fecha(valor):
    if isinstance(valor, datetime):
        return valor
    if isinstance(valor, str) and valor:
        try:
            return datetime.fromisoformat(valor[:19].replace(" ", "T"))
        except ValueError:
            return None
    return None


def _extra(valor):
    if isinstance(valor, str):
        try:
            valor = json.loads(valor)
        except ValueError:
            return {}
    return valor if isinstance(valor, dict) else {}


def _item(fila):
    ex = _extra(fila.extra_data)
    origen = ex.get("origin") or str(fila.component_id).split(":", 1)[0]
    codigo = str(ex.get("code") if ex.get("code") is not None else str(fila.component_id).split(":", 1)[-1])
    desc = ex.get("state") or fila.status_value or "UNKNOWN"
    return {
        "key": fila.component_id,
        "origin": origen,
        "code": codigo,
        "state": desc,
        "clase": clasificar(origen, codigo, desc),
        "total": int(fila.metric_value or 0),
        "last_24h": int(ex.get("last_24h") or 0),
        "pending_iso": int(ex.get("pending_iso") or 0),
        "with_iso": int(ex.get("with_iso") or 0),
        "oldest": ex.get("oldest"),
    }


def estado_portal(db, hospital_id, max_horas, ahora=None):
    """
    Resumen de la última lectura del hospital (None si nunca mandó datos del portal): cada estado
    clasificado, los totales que importan, la antigüedad del pendiente más viejo del MPS y el estado
    general con el mismo criterio que la alerta.
    """
    ahora = ahora or datetime.now()
    # El valor crudo de MAX() se reusa tal cual en el filtro de igualdad: convertirlo a datetime y
    # volver a bindearlo puede no coincidir con el texto guardado (microsegundos en SQLite).
    ultimo_raw = db.execute(text("""
        SELECT MAX(timestamp) FROM software_monitoring
        WHERE hospital_id = :hid AND app_name = :app
    """), {"hid": hospital_id, "app": APP_NAME}).scalar()
    ultimo = _fecha(ultimo_raw)
    if ultimo is None:
        return None

    filas = db.execute(text("""
        SELECT component_id, status_value, metric_value, extra_data
        FROM software_monitoring
        WHERE hospital_id = :hid AND app_name = :app AND timestamp = :ts
    """), {"hid": hospital_id, "app": APP_NAME, "ts": ultimo_raw}).fetchall()
    estados = sorted((_item(f) for f in filas), key=lambda e: (e["origin"] != "RIS", _orden_codigo(e["code"])))

    mps_pend = [e for e in estados if e["origin"] == "MPS" and e["clase"] == PENDIENTE]
    mps_err = [e for e in estados if e["origin"] == "MPS" and e["clase"] == ERROR]
    fechas = [_fecha(e["oldest"]) for e in mps_pend if e["total"] > 0]
    mas_antiguo = min((f for f in fechas if f), default=None)
    edad = round((ahora - mas_antiguo).total_seconds() / 3600, 1) if mas_antiguo else None

    resumen = {
        "last_seen": ultimo.strftime("%Y-%m-%d %H:%M:%S"),
        "max_hours": max_horas,
        "ris_pendientes": sum(e["total"] for e in estados if e["origin"] == "RIS" and e["clase"] == PENDIENTE),
        "mps_pendientes": sum(e["total"] for e in mps_pend),
        "mps_sin_iso": sum(e["pending_iso"] for e in mps_pend),
        "bloqueados": sum(e["total"] for e in mps_err),
        "bloqueados_24h": sum(e["last_24h"] for e in mps_err),
        "mas_antiguo": mas_antiguo.strftime("%Y-%m-%d %H:%M:%S") if mas_antiguo else None,
        "age_hours": edad,
        "states": estados,
    }

    problemas = []
    if (ahora - ultimo).total_seconds() > LECTURA_VIGENTE_HORAS * 3600:
        resumen["status"] = ESTADO_SIN_LECTURA
    else:
        if edad is not None and edad > max_horas:
            problemas.append(ESTADO_DEMORADO)
        if resumen["bloqueados_24h"] > 0:
            problemas.append(ESTADO_BLOQUEOS)
        resumen["status"] = problemas[0] if problemas else ESTADO_OK
    resumen["problemas"] = problemas
    return resumen


def serie_portal(db, hospital_id, minutos):
    """
    Línea de tiempo por estado (solo las clases de CLASES_GRAFICO) en los últimos `minutos`.
    Todas las filas de una lectura comparten el timestamp, así que el eje X son las lecturas. Un
    estado que falta en una lectura queda como hueco (None), no como 0: una caída a cero se leería
    como "la cola se vació".
    """
    if not minutos:
        return {"labels": [], "series": []}
    desde = datetime.now() - timedelta(minutes=minutos)
    filas = db.execute(text("""
        SELECT component_id, status_value, metric_value, extra_data, timestamp
        FROM software_monitoring
        WHERE hospital_id = :hid AND app_name = :app AND timestamp >= :desde
        ORDER BY timestamp ASC
    """), {"hid": hospital_id, "app": APP_NAME, "desde": desde}).fetchall()

    etiquetas, por_clave, info = [], {}, {}
    for f in filas:
        ts = _fecha(f.timestamp)
        if ts is None:
            continue
        ts_txt = ts.strftime("%Y-%m-%d %H:%M:%S")
        if not etiquetas or etiquetas[-1] != ts_txt:
            etiquetas.append(ts_txt)
        it = _item(f)
        if it["clase"] not in CLASES_GRAFICO:
            continue
        info[it["key"]] = it                     # la descripción más reciente gana
        por_clave.setdefault(it["key"], {})[ts_txt] = it["total"]

    series = []
    for clave, valores in por_clave.items():
        it = info[clave]
        series.append({
            "key": clave, "origin": it["origin"], "code": it["code"], "state": it["state"], "clase": it["clase"],
            "points": [valores.get(t) for t in etiquetas],
        })
    series.sort(key=lambda s: (s["origin"] != "MPS", s["clase"] != ERROR, _orden_codigo(s["code"])))
    return {"labels": etiquetas, "series": series}


def verificar_portal_paciente(db, config, hospitales_activos):
    asana_followers = _followers_de(db, config)  # default: global_alert_responsible_email
    try:
        max_horas = max(1, int(config.get("portal_max_hours", 6) or 6))
    except (TypeError, ValueError):
        max_horas = 6

    for hosp in hospitales_activos:
        est = estado_portal(db, hosp.hospital_id, max_horas)
        if est is None or est["status"] == ESTADO_SIN_LECTURA:
            continue

        if ESTADO_DEMORADO in est["problemas"]:
            nivel = "WARNING"
            mensaje = (f"Portal paciente: el estudio más antiguo pendiente en la cola del MPS entró hace "
                       f"{est['age_hours']:.0f} h ({est['mas_antiguo']}; umbral: {max_horas} h). "
                       f"Pendientes en el MPS: {est['mps_pendientes']} ({est['mps_sin_iso']} sin ISO generada).")
        else:
            nivel = "OK"
            mensaje = f"Portal paciente: cola del MPS al día ({est['mps_pendientes']} pendientes)."
        actualizar_estado_alerta(
            db=db, hid=hosp.hospital_id, tipo_unico="PORTAL_DEMORA", nivel=nivel, mensaje=mensaje,
            asana_proj_id=hosp.asana_project_id, asana_followers=asana_followers,
            titulo_visible="Portal paciente: cola demorada",
        )

        if ESTADO_BLOQUEOS in est["problemas"]:
            nivel = "WARNING"
            mensaje = (f"Portal paciente: {est['bloqueados_24h']} estudio(s) con error en la cola del MPS "
                       f"(bloqueado, fallido o abortado) en las últimas 24 h ({est['bloqueados']} con error "
                       f"en total en los últimos 30 días).")
        else:
            nivel = "OK"
            mensaje = "Portal paciente: sin errores nuevos en la cola del MPS en las últimas 24 h."
        actualizar_estado_alerta(
            db=db, hid=hosp.hospital_id, tipo_unico="PORTAL_BLOQUEOS", nivel=nivel, mensaje=mensaje,
            asana_proj_id=hosp.asana_project_id, asana_followers=asana_followers,
            titulo_visible="Portal paciente: estudios con error",
        )
