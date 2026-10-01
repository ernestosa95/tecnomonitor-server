"""
Reproduce un tramo de ingesta real contra el endpoint /v1/hospital-status, para probar la
ingesta en un motor nuevo (docs/14 §9.3, A2 y el ensayo B4).

Arma cada reporte del agente desde una copia SQLite de producción: el JSON de
reportes_historicos + application_metrics (reportes_uso) + software_monitoring rearmado con
el formato de cada app. Lo manda al endpoint con la hora congelada en la del reporte.

    # destino SQLite (archivo nuevo; copia sola las tablas chicas que necesita)
    python3 reproducir_ingesta.py /ruta/monitor_copia.db --destino sqlite:////tmp/replay.db --desde ... --hasta ...
    # destino Postgres (antes: instalar_esquema.py + copiar_tablas_chicas.py)
    python3 reproducir_ingesta.py /ruta/monitor_copia.db --destino postgresql://... --desde ... --hasta ...

Solo lee la copia. El token de ingesta (agentes 4.5+) no se valida: la copia no tiene los tokens.
"""
import argparse
import bisect
import json
import os
import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta

RAIZ = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TABLAS_CHICAS_SQLITE = ["hospitales_metadata", "log_dictionary", "configuracion", "monitoreo_modulos",
                        "mirth_channel_topology"]


def _ts(v):
    return datetime.fromisoformat(str(v).replace("Z", ""))


def _payloads(src, desde, hasta):
    """Reportes del tramo, en orden de llegada, con su software y KPIs."""
    reportes = src.execute(
        "SELECT hospital_id, timestamp, full_json_data FROM reportes_historicos "
        "WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp", (desde, hasta)).fetchall()
    por_hosp = defaultdict(list)
    for hid, ts, _ in reportes:
        por_hosp[hid].append(_ts(ts))
    for lst in por_hosp.values():
        lst.sort()

    def reporte_siguiente(hid, t):
        lst = por_hosp.get(hid) or []
        i = bisect.bisect_left(lst, t)
        return lst[i] if i < len(lst) else None

    uso = {(h, _ts(t)): json.loads(k) for h, t, k in src.execute(
        "SELECT hospital_id, timestamp, kpi_json_data FROM reportes_uso WHERE timestamp >= ? AND timestamp < ?",
        (desde, hasta))}

    soft = defaultdict(lambda: defaultdict(list))      # (hid, ts del reporte) -> app -> filas
    for hid, app, comp, est, val, extra, ts in src.execute(
            "SELECT hospital_id, app_name, component_id, status_value, metric_value, extra_data, timestamp "
            "FROM software_monitoring WHERE timestamp >= ? AND timestamp < ? ORDER BY id", (desde, hasta)):
        t = _ts(ts)
        destino = t if app in ("mirth", "dicom_routing", "ssl_certificate") else reporte_siguiente(hid, t)
        if destino is not None:
            soft[(hid, destino)][app].append((comp, est, val, json.loads(extra) if isinstance(extra, str) else extra, t))

    for hid, ts, js in reportes:
        t = _ts(ts)
        p = json.loads(js)
        if (hid, t) in uso:
            p["application_metrics"] = uso[(hid, t)]
        apps = soft.get((hid, t))
        if apps:
            p["software_monitoring"] = _software(apps)
        yield hid, t, p


def _software(apps):
    sm = {}
    if apps.get("mirth"):
        inst = defaultdict(list)
        for comp, est, val, ex, _ in apps["mirth"]:
            nombre, canal = (comp[1:].split("] ", 1) if comp.startswith("[") else ("Default", comp))
            inst[nombre].append({"channel": canal, "channel_id": ex.get("channel_id"), "status": est,
                                 "queued": val, "received": ex.get("recibidos", 0), "sent": ex.get("enviados", 0),
                                 "errored": ex.get("errored", 0), "last_error": ex.get("last_error", "")})
        sm["mirth"] = list(inst["Default"]) if list(inst) == ["Default"] else dict(inst)
    if apps.get("dicom_routing"):
        sm["dicom_routing_queues"] = [{
            "id_rule": int(comp) if str(comp).isdigit() else comp, "pending_instances": val,
            "from_node": {"key": ex.get("from_key"), "nickname": ex.get("from_nickname"), "hostname": ex.get("from_hostname")},
            "to_node": {"key": ex.get("to_key"), "nickname": ex.get("to_nickname"), "hostname": ex.get("to_hostname")},
        } for comp, est, val, ex, _ in apps["dicom_routing"]]
    if apps.get("ssl_certificate"):
        sm["ssl_certificates"] = [{"url": comp, "status": est, "days_remaining": val,
                                   "expiration_date": ex.get("expiration_date", ""), "issuer": ex.get("issuer", "")}
                                  for comp, est, val, ex, _ in apps["ssl_certificate"]]
    if apps.get("elasticsearch"):
        filas = apps["elasticsearch"]
        sm["suitestensa_logs"] = {"scan_time": filas[0][4].isoformat(),
                                  "events": [{"rule_id": comp, "count": val} for comp, est, val, ex, t in filas
                                             if t == filas[0][4]]}
    if apps.get("sql_integrity"):
        filas = apps["sql_integrity"]
        ex0 = filas[0][3]
        sm["sql_integrity"] = {"sqlserver_start_time": ex0.get("sqlserver_start_time"), "check_type": ex0.get("check_type"),
                               "source": ex0.get("source"),
                               "databases": [{"db": comp, "checked_at": t.isoformat(), "status": est, "error_count": val,
                                              "detail": ex.get("detail"), "duration_s": ex.get("duration_s")}
                                             for comp, est, val, ex, t in filas]}
    if apps.get("sql_backup"):
        filas = apps["sql_backup"]
        sm["sql_backups"] = {"collected_at": filas[-1][3].get("last_seen"), "source": filas[-1][3].get("source"),
                             "databases": [{"db": comp, "last_full": ex.get("last_full")} for comp, est, val, ex, _ in filas]}
    if apps.get("patient_portal"):
        filas = apps["patient_portal"]
        t0 = filas[0][4]
        sm["patient_portal"] = {"collected_at": t0.isoformat(), "source": filas[0][3].get("source"),
                                "states": [{"origin": ex.get("origin"), "code": ex.get("code"), "state": ex.get("state"),
                                            "total": val, "last_24h": ex.get("last_24h"), "pending_iso": ex.get("pending_iso"),
                                            "with_iso": ex.get("with_iso"), "oldest": ex.get("oldest")}
                                           for comp, est, val, ex, t in filas if t == t0]}
    return sm


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sqlite")
    ap.add_argument("--destino", required=True, help="DATABASE_URL del destino (sqlite:////ruta o postgresql://...)")
    ap.add_argument("--desde", required=True)
    ap.add_argument("--hasta", required=True)
    args = ap.parse_args()

    os.environ["DATABASE_URL"] = args.destino
    os.environ.setdefault("JWT_SECRET", "reproducir-ingesta")
    sys.path.insert(0, RAIZ)
    sys.path.insert(0, os.path.join(RAIZ, "dashboard_app"))
    import database
    import main as ingesta
    from fastapi.testclient import TestClient
    from freezegun import freeze_time

    src = sqlite3.connect(f"file:{args.sqlite}?mode=ro", uri=True)
    if database.ES_SQLITE:
        # Tablas chicas que la ingesta lee: hospitales, diccionario de logs, configuración, módulos.
        dst = sqlite3.connect(args.destino.replace("sqlite:///", ""))
        for t in TABLAS_CHICAS_SQLITE:
            cols = [c[1] for c in src.execute(f"PRAGMA table_info({t})")]
            dst.execute(f"DELETE FROM {t}")
            dst.executemany(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                            src.execute(f"SELECT {', '.join(cols)} FROM {t}").fetchall())
        dst.commit()
        dst.close()

    ingesta._validar_token_ingesta = lambda *a, **k: None
    cliente = TestClient(ingesta.app)
    t0, n, errores = time.time(), 0, 0
    for hid, ts, payload in _payloads(src, args.desde, args.hasta):
        with freeze_time(ts + timedelta(seconds=3), tick=True):
            r = cliente.post("/v1/hospital-status", json=payload)
        n += 1
        if r.status_code != 201:
            errores += 1
            if errores <= 5:
                print(f"  {hid} {ts}: {r.status_code} {r.text[:200]}")
        if n % 2000 == 0:
            print(f"  {n} reportes, {time.time() - t0:.0f} s", flush=True)
    print(f"Listo: {n} reportes reproducidos, {errores} con error, {time.time() - t0:.0f} s")


if __name__ == "__main__":
    main()
