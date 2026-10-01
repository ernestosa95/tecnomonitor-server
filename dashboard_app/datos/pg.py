"""
Las funciones de datos/ cuando la sesión apunta a Postgres (esquema de postgres/esquema.sql).

Cada función pública de infra, uso y software delega acá si `es_postgres(db)`, y tiene que
devolver exactamente lo mismo que su versión SQLite: misma forma, mismas horas (locales sin
zona) y los mismos dicts que el código ya conoce (reporte del agente, application_metrics,
extra_data de software). La paridad se verifica contra la foto de producción (docs/14 §9.3, A1).

Reporte completo del agente:
  - el último de cada hospital: `estado_actual_hospital` (tal cual llegó);
  - uno anterior dentro de los 30 días: `reporte_crudo` (tal cual llegó);
  - más viejo: se reconstruye con la versión de inventario vigente + las métricas de ese
    momento (`reconstruir`). Sale igual salvo lo que no se guarda: `last_check` de la red, el
    uptime exacto (se guarda la hora de arranque al minuto) y la dirección de memoria de los
    textos de error.
"""
import copy

from sqlalchemy import text

from .tiempo import a_local, a_pg

# ---------------------------------------------------------------------------
# Infraestructura
# ---------------------------------------------------------------------------


def ultimo_timestamp(db, hospital_id):
    ts = db.execute(text("SELECT ts FROM estado_actual_hospital WHERE hospital_id = :h"),
                    {"h": hospital_id}).scalar()
    return a_local(ts)


def ultimo_reporte(db, hospital_id, hasta=None):
    from .infra import Reporte, json_a_dict
    if hasta is None:
        f = db.execute(text("SELECT hospital_id, ts, host_status, datos FROM estado_actual_hospital "
                            "WHERE hospital_id = :h"), {"h": hospital_id}).fetchone()
        if f:
            return Reporte(f.hospital_id, a_local(f.ts), f.host_status, json_a_dict(f.datos))
        return None
    f = db.execute(text("SELECT ts, host_status FROM metricas_host WHERE hospital_id = :h AND ts <= :hasta "
                        "ORDER BY ts DESC LIMIT 1"), {"h": hospital_id, "hasta": a_pg(hasta)}).fetchone()
    if not f:
        return None
    crudo = db.execute(text("SELECT datos FROM reporte_crudo WHERE hospital_id = :h AND ts = :ts"),
                       {"h": hospital_id, "ts": f.ts}).scalar()
    datos = json_a_dict(crudo) if crudo is not None else reconstruir(db, hospital_id, f.ts)
    return Reporte(hospital_id, a_local(f.ts), f.host_status, datos)


def ultimos_reportes(db):
    from .infra import Reporte, json_a_dict
    filas = db.execute(text("SELECT hospital_id, ts, host_status, datos FROM estado_actual_hospital")).fetchall()
    return [Reporte(f.hospital_id, a_local(f.ts), f.host_status, json_a_dict(f.datos)) for f in filas]


def valores_recientes(db, hospital_id, ruta_json, desde):
    """`ruta_json` como en SQLite ('$.a.b'). collection_meta sale de `recoleccion`; lo demás, del crudo."""
    partes = [p for p in ruta_json.lstrip("$").split(".") if p]
    if partes and partes[0] == "collection_meta":
        tabla, col, camino = "recoleccion", "meta", partes[1:]
        # Reportes sin collection_meta (agentes viejos) no tienen fila en `recoleccion`: se
        # completan con None contra metricas_host, que tiene una fila por reporte.
        filas = db.execute(text(f"""
            SELECT m.ts, r.{col} #> :camino AS valor FROM metricas_host m
            LEFT JOIN {tabla} r ON r.hospital_id = m.hospital_id AND r.ts = m.ts
            WHERE m.hospital_id = :h AND m.ts >= :desde ORDER BY m.ts
        """), {"h": hospital_id, "desde": a_pg(desde), "camino": camino}).fetchall()
    else:
        filas = db.execute(text("SELECT ts, datos #> :camino AS valor FROM reporte_crudo "
                                "WHERE hospital_id = :h AND ts >= :desde ORDER BY ts"),
                           {"h": hospital_id, "desde": a_pg(desde), "camino": partes}).fetchall()
    return [f.valor for f in filas]


def serie_infra(db, hospital_id, desde, hasta=None, max_puntos=None, limite=None):
    from .infra import PuntoInfra
    filtro_hasta = " AND ts <= :hasta" if hasta is not None else ""
    filtro_limite = " LIMIT :limite" if limite else ""
    filas = db.execute(text(
        "SELECT ts, cpu_pct, ram_pct, latencia_ms, subida_mbps, bajada_mbps FROM metricas_host "
        f"WHERE hospital_id = :h AND ts >= :desde{filtro_hasta} ORDER BY ts{filtro_limite}"),
        {"h": hospital_id, "desde": a_pg(desde), "hasta": a_pg(hasta), "limite": limite}).fetchall()
    if max_puntos and len(filas) > max_puntos:
        filas = filas[::max(1, int(len(filas) / max_puntos))]
    if not filas:
        return []

    instantes = [f.ts for f in filas]
    rango = {"h": hospital_id, "t0": instantes[0], "t1": instantes[-1]}
    temps, vms = {}, {}
    for s in db.execute(text("SELECT ts, nombre, valor FROM metricas_sensor WHERE hospital_id = :h AND tipo = 'temp' "
                             "AND ts BETWEEN :t0 AND :t1 ORDER BY ts, orden"), rango):
        if s.valor is not None:
            temps.setdefault(s.ts, {})[s.nombre] = s.valor
    for v in db.execute(text("SELECT ts, vm, cpu_pct, ram_pct FROM metricas_vm WHERE hospital_id = :h "
                             "AND origen = 'virtual_layer' AND ts BETWEEN :t0 AND :t1 ORDER BY ts, orden"), rango):
        # Sin dato, 0: es lo que mostraba el gráfico leyendo el JSON (docs/14 §5.1, decisión pendiente).
        vms.setdefault(v.ts, {})[v.vm] = {"cpu": v.cpu_pct if v.cpu_pct is not None else 0,
                                          "ram": v.ram_pct if v.ram_pct is not None else 0}

    puntos = []
    for f in filas:
        t = temps.get(f.ts, {})
        puntos.append(PuntoInfra(
            timestamp=a_local(f.ts), cpu_host=f.cpu_pct, ram_host=f.ram_pct,
            temp_amb=next((v for n, v in t.items() if "Ambient" in n), None),
            temperaturas=t, red={"lat": f.latencia_ms, "up": f.subida_mbps, "dw": f.bajada_mbps},
            vms=vms.get(f.ts, {}),
        ))
    return puntos


def reconstruir(db, hospital_id, ts):
    """Reporte del agente a partir del inventario vigente en `ts` y las métricas de ese reporte."""
    inv = db.execute(text("SELECT datos FROM inventario WHERE hospital_id = :h AND vigente_desde <= :ts "
                          "AND (vigente_hasta IS NULL OR vigente_hasta > :ts) ORDER BY vigente_desde DESC LIMIT 1"),
                     {"h": hospital_id, "ts": ts}).scalar()
    d = copy.deepcopy(inv) if isinstance(inv, dict) else {}
    p = {"h": hospital_id, "ts": ts}
    host = db.execute(text("SELECT * FROM metricas_host WHERE hospital_id = :h AND ts = :ts"), p).fetchone()
    meta = db.execute(text("SELECT meta FROM recoleccion WHERE hospital_id = :h AND ts = :ts"), p).scalar()

    def poner(dic, clave, valor):
        if valor is not None:
            dic[clave] = valor

    d.setdefault("envelope", {})["timestamp"] = a_local(ts).isoformat()
    if meta is not None:
        d["collection_meta"] = meta
    phy = d.setdefault("physical_layer", {})
    if host is not None:
        tele = phy.setdefault("telemetry", {})
        poner(tele.setdefault("cpu", {}), "usage_percent", host.cpu_pct)
        ram = tele.setdefault("ram", {})
        poner(ram, "used_gb", host.ram_usada_gb)
        poner(ram, "usage_percent", host.ram_pct)
        if host.arranque is not None:
            phy.setdefault("host_info", {})["uptime_seconds"] = int((ts - host.arranque).total_seconds())
        if host.potencia_w is not None:
            phy.setdefault("sensors", {}).setdefault("power", {})["watts_current"] = host.potencia_w
        if any(v is not None for v in (host.latencia_ms, host.subida_mbps, host.bajada_mbps)):
            net = phy.setdefault("network_health", {})
            poner(net, "upload_usage_mbps", host.subida_mbps)
            poner(net, "download_usage_mbps", host.bajada_mbps)
            poner(net, "cloud_latency_ms", host.latencia_ms)

    sensores = {(s.tipo, s.nombre): s.valor for s in db.execute(
        text("SELECT tipo, nombre, valor FROM metricas_sensor WHERE hospital_id = :h AND ts = :ts"), p)}
    sens = phy.get("sensors") or {}
    for tipo, lista, clave in (("temp", sens.get("temperatures"), "value"), ("fan", sens.get("fans"), "value"),
                               ("psu", (sens.get("power") or {}).get("supplies"), "watts")):
        for s in lista or []:
            if (tipo, s.get("name")) in sensores:
                poner(s, clave, sensores[(tipo, s.get("name"))])

    vms = {(v.origen, v.vm): v for v in db.execute(
        text("SELECT * FROM metricas_vm WHERE hospital_id = :h AND ts = :ts"), p)}
    discos = {(x.vm, x.montaje): x for x in db.execute(
        text("SELECT * FROM metricas_disco WHERE hospital_id = :h AND ts = :ts"), p)}
    servicios = {(x.vm, x.servicio): x for x in db.execute(
        text("SELECT * FROM metricas_servicio WHERE hospital_id = :h AND ts = :ts"), p)}
    for origen, lista in (("virtual_layer", d.get("virtual_layer")), ("hipervisor", phy.get("vms"))):
        for vm in lista or []:
            m = vms.get((origen, vm.get("id")))
            if m is None:
                continue
            poner(vm, "state", m.estado)
            poner(vm, "state_reason", m.motivo)
            poner(vm, "wmi_error", m.error)
            vt = vm.setdefault("telemetry", {})
            poner(vt.setdefault("cpu", {}), "usage_percent", m.cpu_pct)
            vram = vt.setdefault("ram", {})
            poner(vram, "used_gb", m.ram_usada_gb)
            poner(vram, "usage_percent", m.ram_pct)
            if m.arranque is not None:
                vt["uptime_seconds"] = int((ts - m.arranque).total_seconds())
            for disco in vm.get("storage") or []:
                x = discos.get((vm.get("id"), disco.get("mount_point")))
                if x is not None:
                    poner(disco, "free_gb", x.libre_gb)
                    poner(disco, "usage_percent", x.uso_pct)
                    if x.latencia_ms is not None:
                        disco.setdefault("performance", {})["latency_ms"] = x.latencia_ms
            for srv in (vm.get("application_layer") or {}).get("services") or []:
                x = servicios.get((vm.get("id"), srv.get("name")))
                if x is not None:
                    vs = srv.setdefault("vital_signs", {})
                    poner(vs, "cpu_percent", x.cpu_pct)
                    poner(vs, "ram_mb", x.ram_mb)
                    poner(vs, "threads", x.hilos)
                    poner(vs, "handles", x.handles)
    return d


# ---------------------------------------------------------------------------
# KPIs de uso
# ---------------------------------------------------------------------------
_RIS = ("totales", "citados", "admitidos", "ejecutados", "con_imagen", "borradores", "definitivos", "suspendidos")


def _iso_local(ts):
    return a_local(ts).strftime("%Y-%m-%dT%H:%M:%S") if ts is not None else None


def _reportes_kpi(db, cabeceras):
    """Cabeceras de kpi_reporte -> [ReporteUso] con application_metrics armado como lo mandó el agente."""
    from .uso import ReporteUso
    if not cabeceras:
        return []
    ids = [c.id for c in cabeceras]
    items = {i: {"ris": [], "pacs": [], "users": []} for i in ids}
    for r in db.execute(text("SELECT * FROM kpi_ris WHERE reporte_id = ANY(:ids) ORDER BY reporte_id, orden"),
                        {"ids": ids}):
        items[r.reporte_id]["ris"].append({"equipo": r.equipo, "aet": r.aet, "mod": r.modalidad,
                                           **{k: getattr(r, k) for k in _RIS}})
    for r in db.execute(text("SELECT * FROM kpi_pacs WHERE reporte_id = ANY(:ids) ORDER BY reporte_id, orden"),
                        {"ids": ids}):
        items[r.reporte_id]["pacs"].append({"aet": r.aet, "mod": r.modalidad, "almacenados": r.almacenados})
    for r in db.execute(text("SELECT * FROM kpi_usuarios WHERE reporte_id = ANY(:ids) ORDER BY reporte_id, orden"),
                        {"ids": ids}):
        items[r.reporte_id]["users"].append({"rol": r.rol, "usuarios_unicos": r.usuarios_unicos,
                                             "inicios_sesion": r.inicios_sesion})
    salida = []
    for c in cabeceras:
        metrics = {"extraction_interval_hours": c.intervalo_horas,
                   "start_time_extraction": _iso_local(c.desde), "end_time_extraction": _iso_local(c.hasta),
                   **items[c.id]}
        salida.append(ReporteUso(timestamp=a_local(c.insertado),
                                 fecha_evento=a_local(c.desde) if c.desde is not None else a_local(c.insertado),
                                 metrics=metrics))
    return salida


def reportes_uso(db, hospital_id, desde=None):
    filtro = " AND insertado >= :desde" if desde is not None else ""
    cab = db.execute(text(f"SELECT * FROM kpi_reporte WHERE hospital_id = :h{filtro} ORDER BY insertado, id"),
                     {"h": hospital_id, "desde": a_pg(desde)}).fetchall()
    return _reportes_kpi(db, cab)


def reportes_uso_por_evento(db, hospital_id, desde, hasta=None, limite=None):
    from .uso import MARGEN_INSERCION
    filtro_limite = " LIMIT :limite" if limite else ""
    cab = db.execute(text("SELECT * FROM kpi_reporte WHERE hospital_id = :h AND insertado >= :desde "
                          f"ORDER BY insertado, id{filtro_limite}"),
                     {"h": hospital_id, "desde": a_pg(desde - MARGEN_INSERCION), "limite": limite}).fetchall()
    return [r for r in _reportes_kpi(db, cab)
            if r.fecha_evento is not None and r.fecha_evento >= desde and (hasta is None or r.fecha_evento < hasta)]


# ---------------------------------------------------------------------------
# Software: cada app vive en su tabla; se devuelven como Lectura con el
# extra_data que armaba la ingesta en software_monitoring.
# ---------------------------------------------------------------------------
_SELECT = {
    "mirth": ("mirth_canal_metricas", "componente", """
        SELECT 'mirth' AS app_name, componente AS component_id, estado AS status_value, encolados AS metric_value,
               ts, NULL::bigint AS id, instancia, ultimo_error, recibidos, enviados, channel_id, errores
        FROM mirth_canal_metricas WHERE hospital_id = :h"""),
    "dicom_routing": ("cola_dicom_metricas", "regla", """
        SELECT 'dicom_routing' AS app_name, c.regla AS component_id, 'OK' AS status_value,
               c.pendientes AS metric_value, c.ts, NULL::bigint AS id, r.etiqueta, r.origen_key, r.origen_nick,
               r.origen_host, r.destino_key, r.destino_nick, r.destino_host
        FROM cola_dicom_metricas c LEFT JOIN dicom_reglas r ON r.hospital_id = c.hospital_id AND r.regla = c.regla
        WHERE c.hospital_id = :h"""),
    "patient_portal": ("portal_estado_metricas", "componente", """
        SELECT 'patient_portal' AS app_name, componente AS component_id, estado AS status_value, total AS metric_value,
               ts, NULL::bigint AS id, origen, codigo, ultimas_24h, sin_iso, con_iso, mas_antiguo, fuente
        FROM portal_estado_metricas WHERE hospital_id = :h"""),
    "sql": ("sql_eventos", "base", """
        SELECT app AS app_name, base AS component_id, estado AS status_value, valor AS metric_value, ts, id, extra
        FROM sql_eventos WHERE hospital_id = :h AND app = :app"""),
    "otros": ("software_eventos", "componente", """
        SELECT app AS app_name, componente AS component_id, estado AS status_value, valor AS metric_value, ts,
               NULL::bigint AS id, extra
        FROM software_eventos WHERE hospital_id = :h AND app = :app"""),
}


def _clave(app):
    if app in ("mirth", "dicom_routing", "patient_portal"):
        return app
    return "sql" if app in ("sql_integrity", "sql_backup") else "otros"


def _extra(app, f):
    if app == "mirth":
        return {"instancia": f.instancia, "last_error": f.ultimo_error, "recibidos": f.recibidos,
                "enviados": f.enviados, "channel_id": f.channel_id, "errored": f.errores}
    if app == "dicom_routing":
        return {"label": f.etiqueta, "from_key": f.origen_key, "from_nickname": f.origen_nick,
                "from_hostname": f.origen_host, "to_key": f.destino_key, "to_nickname": f.destino_nick,
                "to_hostname": f.destino_host}
    if app == "patient_portal":
        return {"origin": f.origen, "code": f.codigo, "state": f.status_value, "last_24h": f.ultimas_24h,
                "pending_iso": f.sin_iso, "with_iso": f.con_iso, "oldest": _iso_local(f.mas_antiguo),
                "source": f.fuente}
    return f.extra if isinstance(f.extra, dict) else {}


def _lectura(app, f, rn=1):
    from .software import Lectura
    return Lectura(app_name=f.app_name, component_id=f.component_id, status_value=f.status_value,
                   metric_value=f.metric_value, extra_data=_extra(app, f), timestamp=a_local(f.ts), rn=rn)


def ultimas_lecturas(db, hospital_id, apps, n=1, por_id=False):
    from .software import _apps
    salida = []
    for app in sorted(_apps(apps)):
        _tabla, comp, sql = _SELECT[_clave(app)]
        orden = "id DESC" if por_id and _clave(app) == "sql" else "ts DESC"
        filas = db.execute(text(f"""
            WITH r AS (SELECT x.*, ROW_NUMBER() OVER (PARTITION BY component_id ORDER BY {orden}) AS rn
                       FROM ({sql}) x)
            SELECT * FROM r WHERE rn <= :n ORDER BY component_id COLLATE "C", rn
        """), {"h": hospital_id, "app": app, "n": n}).fetchall()
        salida += [_lectura(app, f, f.rn) for f in filas]
    return salida


def lecturas(db, hospital_id, apps, desde, limite=None, por_componente=False):
    from .software import _apps
    salida = []
    for app in _apps(apps):
        _tabla, _comp, sql = _SELECT[_clave(app)]
        filtro_limite = " LIMIT :limite" if limite else ""
        filas = db.execute(text(f"SELECT * FROM ({sql}) x WHERE ts >= :desde ORDER BY ts{filtro_limite}"),
                           {"h": hospital_id, "app": app, "desde": a_pg(desde), "limite": limite}).fetchall()
        salida += [_lectura(app, f) for f in filas]
    if por_componente:
        salida.sort(key=lambda l: (l.component_id, l.timestamp))
    else:
        salida.sort(key=lambda l: (l.timestamp, l.component_id, l.app_name))
    return salida[:limite] if limite else salida


def ultima_foto(db, hospital_id, app):
    _tabla, _comp, sql = _SELECT[_clave(app)]
    ultimo = db.execute(text(f"SELECT max(ts) FROM ({sql}) x"), {"h": hospital_id, "app": app}).scalar()
    if ultimo is None:
        return None, []
    filas = db.execute(text(f'SELECT * FROM ({sql}) x WHERE ts = :ts ORDER BY component_id COLLATE "C"'),
                       {"h": hospital_id, "app": app, "ts": ultimo}).fetchall()
    return a_local(ultimo), [_lectura(app, f) for f in filas]


__all__ = ["ultimo_timestamp", "ultimo_reporte", "ultimos_reportes", "valores_recientes", "serie_infra",
           "reconstruir", "reportes_uso", "reportes_uso_por_evento", "ultimas_lecturas", "lecturas",
           "ultima_foto"]
