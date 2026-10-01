"""
Reporte del agente -> filas del esquema Postgres (postgres/esquema.sql).

Función pura, sin base: la usan la carga del histórico (Fase 4) y la ingesta
nueva (Fase 3), así que las dos separan el reporte exactamente igual.

Separa cada reporte en:
  - métricas (lo que cambia reporte a reporte, medido en la Fase 0): host,
    sensores, VMs, discos y servicios, en filas tipadas;
  - inventario: el reporte SIN esas métricas ni lo volátil. Se guarda solo
    cuando su hash cambia, así que tiene que ser estable entre reportes;
  - collection_meta, aparte (tabla `recoleccion`).

Tolera las variantes de los agentes 4.0 a 4.5 vistas en producción: el RAID
en `storage_layer` o en `storage`, y las VMs de VMware en `physical_layer.vms`.
"""
import copy
import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class Filas:
    host: dict
    sensores: list = field(default_factory=list)     # {tipo, nombre, valor}
    vms: list = field(default_factory=list)          # {vm, cpu_pct, ram_pct, ram_usada_gb, arranque, estado, motivo, error}
    discos: list = field(default_factory=list)       # {vm, montaje, uso_pct, libre_gb, latencia_ms}
    servicios: list = field(default_factory=list)    # {vm, servicio, cpu_pct, ram_mb, hilos, handles}
    meta: Optional[dict] = None                      # collection_meta
    inventario: dict = field(default_factory=dict)
    inventario_hash: str = ""


def _num(v):
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _entero(v):
    n = _num(v)
    return int(n) if n is not None else None


def _arranque(ts, uptime):
    """Hora de arranque redondeada al minuto (el uptime se mide con segundos de diferencia del ts)."""
    seg = _num(uptime)
    if ts is None or seg is None:
        return None
    a = ts - timedelta(seconds=seg)
    return a.replace(second=0, microsecond=0) + (timedelta(minutes=1) if a.second >= 30 else timedelta())


def _quitar(d, *claves):
    if isinstance(d, dict):
        for k in claves:
            d.pop(k, None)


# Direcciones de memoria en textos de excepción ("... at 0x7f3a...>"): cambian en cada reporte.
_HEX = re.compile(r" at 0x[0-9a-fA-F]+")


def hash_inventario(inv):
    canon = json.dumps(inv, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canon.encode()).hexdigest()


def transformar(ts: datetime, data: dict, host_status: Optional[str] = None) -> Filas:
    """`ts`: hora del reporte (con zona). `data`: el JSON del agente (no se modifica)."""
    inv = copy.deepcopy(data) if isinstance(data, dict) else {}

    # --- Host ---
    phy = inv.get("physical_layer") or {}
    tele = phy.get("telemetry") or {}
    cpu, ram = tele.get("cpu") or {}, tele.get("ram") or {}
    host_info = phy.get("host_info") or {}
    sensors = phy.get("sensors") or {}
    power = sensors.get("power") or {}
    net = phy.get("network_health") or {}

    filas = Filas(host={
        "cpu_pct": _num(cpu.get("usage_percent")),
        "ram_pct": _num(ram.get("usage_percent")),
        "ram_usada_gb": _num(ram.get("used_gb")),
        "potencia_w": _num(power.get("watts_current")),
        "latencia_ms": _num(net.get("cloud_latency_ms")),
        "subida_mbps": _num(net.get("upload_usage_mbps")),
        "bajada_mbps": _num(net.get("download_usage_mbps")),
        "arranque": _arranque(ts, host_info.get("uptime_seconds")),
        "host_status": host_status,
    })
    _quitar(cpu, "usage_percent")
    _quitar(ram, "usage_percent", "used_gb")
    _quitar(host_info, "uptime_seconds")
    _quitar(power, "watts_current")
    _quitar(net, "cloud_latency_ms", "upload_usage_mbps", "download_usage_mbps", "last_check")

    for tipo, lista, clave in (("temp", sensors.get("temperatures"), "value"),
                               ("fan", sensors.get("fans"), "value"),
                               ("psu", power.get("supplies"), "watts")):
        for s in lista or []:
            if isinstance(s, dict) and s.get("name"):
                filas.sensores.append({"tipo": tipo, "nombre": s["name"], "valor": _num(s.get(clave))})
                s.pop(clave, None)

    for clave_raid in ("storage_layer", "storage"):
        raid = phy.get(clave_raid)
        if isinstance(raid, dict) and isinstance(raid.get("error"), str):
            raid["error"] = _HEX.sub("", raid["error"])

    # --- VMs: virtual_layer (Proxmox/WMI) y physical_layer.vms (VMware) ---
    for vm in list(inv.get("virtual_layer") or []) + list(phy.get("vms") or []):
        if not isinstance(vm, dict) or not vm.get("id"):
            continue
        vid = vm["id"]
        vt = vm.get("telemetry") or {}
        vcpu, vram = vt.get("cpu") or {}, vt.get("ram") or {}
        filas.vms.append({
            "vm": vid,
            "cpu_pct": _num(vcpu.get("usage_percent")),
            "ram_pct": _num(vram.get("usage_percent")),
            "ram_usada_gb": _num(vram.get("used_gb")),
            "arranque": _arranque(ts, vt.get("uptime_seconds")),
            # Estado de la VM: oscila (Online/Offline, errores de WMI), es serie y no inventario.
            "estado": vm.get("state"),
            "motivo": vm.get("state_reason"),
            "error": vm.get("wmi_error"),
        })
        _quitar(vm, "state", "state_reason", "wmi_error")
        _quitar(vcpu, "usage_percent")
        _quitar(vram, "usage_percent", "used_gb")
        _quitar(vt, "uptime_seconds")

        for disco in vm.get("storage") or []:
            if not isinstance(disco, dict) or not disco.get("mount_point"):
                continue
            perf = disco.get("performance") or {}
            filas.discos.append({
                "vm": vid, "montaje": disco["mount_point"],
                "uso_pct": _num(disco.get("usage_percent")), "libre_gb": _num(disco.get("free_gb")),
                "latencia_ms": _num(perf.get("latency_ms")),
            })
            _quitar(disco, "usage_percent", "free_gb")
            _quitar(perf, "latency_ms")

        for srv in ((vm.get("application_layer") or {}).get("services") or []):
            if not isinstance(srv, dict) or not srv.get("name"):
                continue
            vs = srv.get("vital_signs") or {}
            filas.servicios.append({
                "vm": vid, "servicio": srv["name"],
                "cpu_pct": _num(vs.get("cpu_percent")), "ram_mb": _num(vs.get("ram_mb")),
                "hilos": _entero(vs.get("threads")), "handles": _entero(vs.get("handles")),
            })
            _quitar(vs, "cpu_percent", "ram_mb", "threads", "handles")

    # --- Lo que no es inventario ---
    filas.meta = inv.pop("collection_meta", None)
    _quitar(inv.get("envelope"), "timestamp")
    _quitar(inv, "application_metrics", "software_monitoring")   # van a sus propias tablas

    filas.inventario = _sin_vacios(inv)
    filas.inventario_hash = hash_inventario(filas.inventario)
    return filas


def _sin_vacios(x):
    """Quita los objetos que quedaron vacíos al sacar las métricas ({"cpu": {}} = sin cpu)."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            v = _sin_vacios(v)
            if v != {}:
                out[k] = v
        return out
    if isinstance(x, list):
        return [_sin_vacios(v) for v in x]
    return x


# ---------------------------------------------------------------------------
# Software (hoy una fila de software_monitoring) -> tabla tipada
# ---------------------------------------------------------------------------
def _ts_local(valor, zona):
    """Fecha local sin zona del agente ("2026-09-02T08:45:37") -> datetime con la zona del hospital."""
    if not valor:
        return None
    try:
        t = datetime.fromisoformat(str(valor).replace("Z", ""))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=zona)


def software(app, componente, estado, valor, extra, zona):
    """
    Una lectura de software -> (tabla, columnas sin ts ni hospital_id). Para el
    autoenrute devuelve además la fila de `dicom_reglas` (catálogo de nodos).
    """
    extra = extra if isinstance(extra, dict) else {}
    if app == "mirth":
        return "mirth_canal_metricas", {
            "componente": componente, "instancia": extra.get("instancia"), "channel_id": extra.get("channel_id"),
            "estado": estado, "encolados": _entero(valor), "recibidos": _entero(extra.get("recibidos")),
            "enviados": _entero(extra.get("enviados")), "errores": _entero(extra.get("errored")),
            "ultimo_error": extra.get("last_error"),
        }
    if app == "dicom_routing":
        return "cola_dicom_metricas", {
            "regla": componente, "pendientes": _entero(valor),
            "_regla": {
                "etiqueta": extra.get("label"),
                "origen_key": _entero(extra.get("from_key")), "origen_nick": extra.get("from_nickname"),
                "origen_host": extra.get("from_hostname"),
                "destino_key": _entero(extra.get("to_key")), "destino_nick": extra.get("to_nickname"),
                "destino_host": extra.get("to_hostname"),
            },
        }
    if app == "patient_portal":
        return "portal_estado_metricas", {
            "componente": componente, "origen": extra.get("origin"),
            "codigo": None if extra.get("code") is None else str(extra.get("code")),
            "estado": extra.get("state") or estado, "total": _entero(valor),
            "ultimas_24h": _entero(extra.get("last_24h")), "sin_iso": _entero(extra.get("pending_iso")),
            "con_iso": _entero(extra.get("with_iso")), "mas_antiguo": _ts_local(extra.get("oldest"), zona),
            "fuente": extra.get("source"),
        }
    if app in ("sql_integrity", "sql_backup"):
        return "sql_eventos", {"app": app, "base": componente, "estado": estado, "valor": _entero(valor),
                               "extra": extra}
    return "software_eventos", {"app": app, "componente": componente, "estado": estado,
                                "valor": _entero(valor), "extra": extra}


# ---------------------------------------------------------------------------
# KPIs de uso (application_metrics) -> kpi_reporte + ítems
# ---------------------------------------------------------------------------
_RIS = ("totales", "citados", "admitidos", "ejecutados", "con_imagen", "borradores", "definitivos", "suspendidos")


def kpis(metrics, zona):
    """application_metrics -> (cabecera, ris, pacs, usuarios); cada ítem conserva su posición."""
    m = metrics if isinstance(metrics, dict) else {}
    cabecera = {
        "desde": _ts_local(m.get("start_time_extraction"), zona),
        "hasta": _ts_local(m.get("end_time_extraction"), zona),
        "intervalo_horas": _num(m.get("extraction_interval_hours")),
    }
    ris = [{"orden": i, "equipo": it.get("equipo"), "aet": it.get("aet"), "modalidad": it.get("mod"),
            **{k: _entero(it.get(k)) for k in _RIS}}
           for i, it in enumerate(m.get("ris") or []) if isinstance(it, dict)]
    pacs = [{"orden": i, "aet": it.get("aet"), "modalidad": it.get("mod"), "almacenados": _entero(it.get("almacenados"))}
            for i, it in enumerate(m.get("pacs") or []) if isinstance(it, dict)]
    usuarios = [{"orden": i, "rol": it.get("rol"), "usuarios_unicos": _entero(it.get("usuarios_unicos")),
                 "inicios_sesion": _entero(it.get("inicios_sesion"))}
                for i, it in enumerate(m.get("users") or []) if isinstance(it, dict)]
    return cabecera, ris, pacs, usuarios
