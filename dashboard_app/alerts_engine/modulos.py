"""
Módulos de monitoreo dados de baja (REQ-03, etapa 1: módulos completos). Ver docs/16.

Estado persistido en `monitoreo_modulos` (sin fila = activo):
- La ingesta llama a `registrar_collection_meta()` con cada reporte: `enabled == false` abre una
  racha `pendiente_baja`; si la racha cumple `monitoreo_gracia_horas` (6 h), pasa a `desactivado`.
  `enabled == true` borra la fila de origen `agente` (reactivación). La gracia se mide con los
  timestamps de los reportes del hospital: un hospital offline no cumple la gracia.
- La baja manual (Admin/Ingeniería) nace `desactivado`, con origen `manual`; es la única vía para
  los KPIs (`sql`), porque `collection_meta.sql` no es confiable (los KPIs también llegan por
  Elastic con `enabled_sql` apagado).

Una baja es *efectiva* si está `desactivado` y (es manual o el switch `monitoreo_bajas_enabled`
está prendido). Con el switch apagado, las bajas del agente solo se registran: la vista previa
(`preview()`) muestra qué alertas se cerrarían, y nada se oculta ni se cierra.

Efecto de una baja efectiva:
- `aplicar_bajas()` (una vez por baja, desde el tick) cierra las alertas abiertas del módulo con
  motivo `[BAJA]` y su ticket de Asana.
- `actualizar_estado_alerta()` consulta `baja_para()` y no abre ni reabre alertas del módulo.
- Las alertas cerradas con `[BAJA]` no cuentan como reincidencia al reactivar: arrancan de cero.
- La pestaña Software y el mapa de integraciones dejan de mostrar el módulo.
"""
from datetime import datetime, timedelta

import requests

import database

ESTADO_PENDIENTE = "pendiente_baja"
ESTADO_DESACTIVADO = "desactivado"
ORIGEN_AGENTE = "agente"
ORIGEN_MANUAL = "manual"
PREFIJO_BAJA = "[BAJA]"

# Módulo (clave de collection_meta) -> etiqueta, prefijos de `alertas.tipo` que le pertenecen,
# `app_name` de software_monitoring que deja de mostrarse, y si la baja la decide el agente.
# NETWORK_LATENCY y OFFLINE no pertenecen a ningún módulo: el agente siempre mide la red.
MODULOS = {
    "proxmox": {"label": "Hipervisor (Proxmox/VMware)", "prefijos": ("HOST_",), "apps": (), "agente": True},
    "idrac": {"label": "iDRAC (sensores y RAID)", "prefijos": ("TEMP_", "FAN_", "PSU_", "RAID_"), "apps": (), "agente": True},
    "wmi": {"label": "VMs / estaciones / equipos", "prefijos": ("VM_", "DISK_"), "apps": (), "agente": True},
    "mirth": {"label": "Mirth Connect", "prefijos": ("MIRTH_",), "apps": ("mirth",), "agente": True},
    "dicom_routing": {"label": "Autoenrute DICOM", "prefijos": ("DICOM_ROUTE_",), "apps": ("dicom_routing",), "agente": True},
    "ssl_monitoring": {"label": "Certificados SSL", "prefijos": (), "apps": ("ssl_certificate",), "agente": True},
    "suitestensa_logs": {"label": "Logs de SuiteEstensa", "prefijos": (), "apps": ("elasticsearch",), "agente": True},
    "sql_integrity": {"label": "Integridad de bases (CHECKDB)", "prefijos": ("CHECKDB_",), "apps": ("sql_integrity",), "agente": True},
    "sql_backups": {"label": "Último backup de las bases", "prefijos": ("SQLBACKUP_",), "apps": ("sql_backup",), "agente": True},
    "sql": {"label": "KPIs de uso (RIS/PACS)", "prefijos": ("KPI_INACT_",), "apps": (), "agente": False},
}

# Cache de bajas efectivas: {hospital_id: {modulo: fila}}. Se refresca una vez por tick
# (procesar_offline), igual que las exclusiones.
_BAJAS_CACHE = {}


# ---------------------------------------------------------------------------
# Config (leída directo de la tabla: la ingesta no pasa por cargar_config)
# ---------------------------------------------------------------------------
def _valor_config(db, clave, default):
    fila = db.query(database.ConfigModel).filter_by(clave=clave).first()
    return fila.valor if fila else default


def gracia_horas(db):
    try:
        return max(1.0, float(_valor_config(db, "monitoreo_gracia_horas", 6)))
    except (TypeError, ValueError):
        return 6.0


def switch_activo(db):
    return str(_valor_config(db, "monitoreo_bajas_enabled", "0")) == "1"


def modulo_de_tipo(tipo):
    t = tipo or ""
    for modulo, d in MODULOS.items():
        if any(t.startswith(p) for p in d["prefijos"]):
            return modulo
    return None


def _es_efectiva(fila, switch):
    return fila.estado == ESTADO_DESACTIVADO and (fila.origen == ORIGEN_MANUAL or switch)


# ---------------------------------------------------------------------------
# Ingesta
# ---------------------------------------------------------------------------
def registrar_collection_meta(db, hospital_id, collection_meta, ts):
    """
    Actualiza `monitoreo_modulos` con lo que declara un reporte. No hace commit (lo hace el
    caller). Tolera reportes sin `collection_meta` (agentes viejos) y módulos que el agente no
    declara: en ambos casos no cambia nada.
    """
    if not isinstance(collection_meta, dict) or not hospital_id or ts is None:
        return
    gracia = timedelta(hours=gracia_horas(db))
    filas = {f.modulo: f for f in db.query(database.MonitoreoModulo).filter_by(hospital_id=hospital_id)}

    for modulo, d in MODULOS.items():
        if not d["agente"]:
            continue
        meta = collection_meta.get(modulo)
        if not isinstance(meta, dict) or not isinstance(meta.get("enabled"), bool):
            continue
        fila = filas.get(modulo)

        if meta["enabled"]:
            # Reactivación: la baja del agente se borra; la manual se respeta.
            if fila and fila.origen == ORIGEN_AGENTE:
                print(f"🔁 [Módulos] {hospital_id}: '{modulo}' reactivado por el agente.")
                db.delete(fila)
            continue

        if fila is None:
            db.add(database.MonitoreoModulo(
                hospital_id=hospital_id, modulo=modulo, estado=ESTADO_PENDIENTE,
                origen=ORIGEN_AGENTE, declarado_off_desde=ts, ultimo_reporte_off=ts,
                motivo="Desactivado en el agente", actualizado_por="agente",
            ))
            continue
        if fila.origen != ORIGEN_AGENTE:
            continue
        fila.ultimo_reporte_off = ts
        if fila.estado == ESTADO_PENDIENTE and fila.declarado_off_desde and ts - fila.declarado_off_desde >= gracia:
            fila.estado = ESTADO_DESACTIVADO
            fila.desactivado_desde = ts
            print(f"⏸️ [Módulos] {hospital_id}: '{modulo}' pasa a desactivado (gracia cumplida).")


# ---------------------------------------------------------------------------
# Motor de alertas
# ---------------------------------------------------------------------------
def cargar_bajas(db):
    """Refresca el cache de bajas efectivas. Una vez por tick, desde procesar_offline()."""
    global _BAJAS_CACHE
    try:
        switch = switch_activo(db)
        cache = {}
        for f in db.query(database.MonitoreoModulo).filter_by(estado=ESTADO_DESACTIVADO).all():
            if _es_efectiva(f, switch):
                cache.setdefault(f.hospital_id, {})[f.modulo] = f.id
        _BAJAS_CACHE = cache
    except Exception as e:
        print(f"⚠️ [Módulos] No se pudieron cargar las bajas: {repr(e)}")
        _BAJAS_CACHE = {}
    return _BAJAS_CACHE


def baja_para(hospital_id, tipo=None, modulo=None):
    """True si el módulo de la alerta (o el módulo pedido) está dado de baja en el hospital."""
    modulo = modulo or modulo_de_tipo(tipo)
    return bool(modulo and modulo in _BAJAS_CACHE.get(hospital_id, {}))


def bajas_efectivas_hospital(db, hospital_id):
    """Módulos con baja efectiva de un hospital, leído de la base (para la API, sin cache)."""
    switch = switch_activo(db)
    return {f.modulo for f in db.query(database.MonitoreoModulo).filter_by(hospital_id=hospital_id)
            if _es_efectiva(f, switch)}


def _alertas_del_modulo(db, hospital_id, modulo):
    prefijos = MODULOS.get(modulo, {}).get("prefijos", ())
    if not prefijos:
        return []
    activas = db.query(database.AlertaModel).filter(
        database.AlertaModel.hospital_id == hospital_id,
        database.AlertaModel.is_active == 1,
    ).all()
    return [a for a in activas if any((a.tipo or "").startswith(p) for p in prefijos)]


def cerrar_alertas(db, alertas, motivo):
    """
    Cierra alertas (y su ticket de Asana) con el prefijo [BAJA]: al reactivar no cuentan como
    reincidencia. No hace commit. Devuelve cuántas cerró.
    """
    from .estado import asana_conector  # misma instancia que resolvió estado.py

    ahora = datetime.now()
    for a in alertas:
        if a.asana_task_gid and asana_conector:
            try:
                asana_conector.cerrar_tarea_asana(a.asana_task_gid, a.hospital_id, a.tipo, ahora)
            except Exception as e:
                print(f"⚠️ [Módulos] No se pudo cerrar en Asana {a.asana_task_gid}: {e}")
        a.is_active = 0
        a.end_time = ahora
        a.mensaje = f"{PREFIJO_BAJA} {motivo}: {a.mensaje}"
    return len(alertas)


def _avisar_ws():
    try:
        requests.post("http://127.0.0.1:8001/api/internal/trigger-ws", timeout=1)
    except Exception:
        pass


def aplicar_baja(db, fila):
    """Acciones de baja de una fila (una sola vez): cierra las alertas abiertas del módulo."""
    etiqueta = MODULOS.get(fila.modulo, {}).get("label", fila.modulo)
    n = cerrar_alertas(db, _alertas_del_modulo(db, fila.hospital_id, fila.modulo),
                       f"Monitoreo desactivado ({etiqueta})")
    fila.alertas_cerradas = (fila.alertas_cerradas or 0) + n
    fila.acciones_aplicadas_en = datetime.now()
    if n:
        print(f"🔕 [Módulos] {fila.hospital_id}: '{fila.modulo}' dado de baja, {n} alerta(s) cerrada(s).")
    return n


def aplicar_bajas(db):
    """Ejecuta las acciones pendientes de las bajas efectivas. Una vez por tick."""
    switch = switch_activo(db)
    pendientes = db.query(database.MonitoreoModulo).filter(
        database.MonitoreoModulo.estado == ESTADO_DESACTIVADO,
        database.MonitoreoModulo.acciones_aplicadas_en.is_(None),
    ).all()
    total = 0
    for fila in pendientes:
        if _es_efectiva(fila, switch):
            total += aplicar_baja(db, fila)
    db.commit()
    if total:
        _avisar_ws()
    return total


def cerrar_alertas_hospital(db, hospital_id, motivo):
    """Baja del hospital desde el server (is_visible / alerts_enabled en falso). Hace commit."""
    activas = db.query(database.AlertaModel).filter(
        database.AlertaModel.hospital_id == hospital_id,
        database.AlertaModel.is_active == 1,
    ).all()
    n = cerrar_alertas(db, activas, motivo)
    db.commit()
    if n:
        print(f"🔕 [Módulos] {hospital_id}: {n} alerta(s) cerrada(s) por baja del hospital.")
        _avisar_ws()
    return n


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def serializar(fila, switch, db=None):
    d = MODULOS.get(fila.modulo, {})
    fmt = lambda v: v.strftime("%Y-%m-%d %H:%M") if v else None  # noqa: E731
    out = {
        "hospital_id": fila.hospital_id,
        "modulo": fila.modulo,
        "label": d.get("label", fila.modulo),
        "estado": fila.estado,
        "origen": fila.origen,
        "efectiva": _es_efectiva(fila, switch),
        "declarado_off_desde": fmt(fila.declarado_off_desde),
        "ultimo_reporte_off": fmt(fila.ultimo_reporte_off),
        "desactivado_desde": fmt(fila.desactivado_desde),
        "acciones_aplicadas_en": fmt(fila.acciones_aplicadas_en),
        "alertas_cerradas": fila.alertas_cerradas or 0,
        "motivo": fila.motivo,
        "actualizado_por": fila.actualizado_por,
    }
    if db is not None and not fila.acciones_aplicadas_en:
        out["alertas_a_cerrar"] = [
            {"tipo": a.tipo, "mensaje": a.mensaje}
            for a in _alertas_del_modulo(db, fila.hospital_id, fila.modulo)
        ]
    return out


def preview(db):
    """Todas las bajas registradas, con las alertas que se cerrarían si todavía no se aplicaron."""
    switch = switch_activo(db)
    filas = db.query(database.MonitoreoModulo).order_by(
        database.MonitoreoModulo.hospital_id, database.MonitoreoModulo.modulo).all()
    items = [serializar(f, switch, db) for f in filas]
    return {
        "switch": switch,
        "gracia_horas": gracia_horas(db),
        "total": len(items),
        "alertas_a_cerrar": sum(len(i.get("alertas_a_cerrar", [])) for i in items
                                if i["estado"] == ESTADO_DESACTIVADO),
        "modulos": items,
    }
