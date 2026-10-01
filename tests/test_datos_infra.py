from datetime import timedelta

import database
from datos import infra
from datos.tiempo import parsear_ts


def _reporte(db, hid, ts, data, status="Online"):
    db.add(database.ReporteModel(hospital_id=hid, timestamp=ts, host_status=status, full_json_data=data))


def test_parsear_ts_formatos():
    assert parsear_ts("2026-10-01 09:00:28.361456").second == 28
    assert parsear_ts("2026-10-01 09:00:28").minute == 0
    assert parsear_ts("2026-10-01T09:00:28Z").hour == 9
    assert parsear_ts("basura") is None
    assert parsear_ts(None) is None


def test_ultimo_reporte_y_timestamp(db, ahora):
    _reporte(db, "H01", ahora - timedelta(minutes=10), {"a": 1})
    _reporte(db, "H01", ahora - timedelta(minutes=5), {"a": 2}, status="Warning")
    _reporte(db, "H02", ahora - timedelta(minutes=1), {"b": 1})
    db.commit()

    r = infra.ultimo_reporte(db, "H01")
    assert r.data == {"a": 2} and r.host_status == "Warning"
    assert r.timestamp == ahora - timedelta(minutes=5)
    assert infra.ultimo_timestamp(db, "H02") == ahora - timedelta(minutes=1)
    assert infra.ultimo_reporte(db, "NUNCA") is None
    assert infra.ultimo_timestamp(db, "NUNCA") is None


def test_json_roto_o_vacio_es_dict_vacio(db, ahora):
    db.execute(database.ReporteModel.__table__.insert().values(
        hospital_id="H01", timestamp=ahora, full_json_data=None))
    db.commit()
    assert infra.ultimo_reporte(db, "H01").data == {}
    assert infra.json_a_dict('{"x": 1}') == {"x": 1}
    assert infra.json_a_dict("{roto") == {}
    assert infra.json_a_dict("[1, 2]") == {}


def test_ultimos_reportes_uno_por_hospital(db, ahora):
    for m in (30, 20, 10):
        _reporte(db, "H01", ahora - timedelta(minutes=m), {"m": m})
    _reporte(db, "H02", ahora - timedelta(minutes=3), {"m": 3})
    db.commit()
    por_hosp = {r.hospital_id: r.data["m"] for r in infra.ultimos_reportes(db)}
    assert por_hosp == {"H01": 10, "H02": 3}


def test_valores_recientes(db, ahora):
    meta = {"enabled": True, "status": "ok", "total": 1}
    _reporte(db, "H01", ahora - timedelta(minutes=50), {"collection_meta": {"dicom_routing": {"status": "viejo"}}})
    _reporte(db, "H01", ahora - timedelta(minutes=10), {"collection_meta": {"dicom_routing": meta}})
    _reporte(db, "H01", ahora - timedelta(minutes=5), {"otra": 1})
    db.commit()
    vals = infra.valores_recientes(db, "H01", "$.collection_meta.dicom_routing", ahora - timedelta(minutes=30))
    assert vals == [meta, None]


def _json_agente(cpu, ram, temps, vms=()):
    return {
        "physical_layer": {
            "telemetry": {"cpu": {"usage_percent": cpu}, "ram": {"usage_percent": ram}},
            "sensors": {"temperatures": [{"name": n, "value": v, "unit": "C"} for n, v in temps]},
            "network_health": {"cloud_latency_ms": 40, "upload_usage_mbps": 1, "download_usage_mbps": 2},
        },
        "virtual_layer": [{"id": vid, "telemetry": {"cpu": {"usage_percent": c}, "ram": {"usage_percent": r}}}
                          for vid, c, r in vms],
    }


def test_metricas_de_formato_actual_y_viejo(ahora):
    p = infra.metricas_de(ahora, _json_agente(12.5, 60, [("Inlet Ambient", 22), ("CPU1", "41")], [("APPV", 5, 70)]))
    assert (p.cpu_host, p.ram_host, p.temp_amb) == (12.5, 60.0, 22.0)
    assert p.temperaturas == {"Inlet Ambient": 22.0, "CPU1": 41.0}
    assert p.vms == {"APPV": {"cpu": 5, "ram": 70}}
    assert p.red == {"lat": 40, "up": 1, "dw": 2}

    viejo = {"physical_host": {"telemetry": {}}, "environment": {"thermal": {
                 "ambient_temp_c": 24, "cpu_temps": [{"sensor": "CPU", "temp_c": 50}]}},
             "vms": {"VM1": {"metrics": {"cpu_load_percent": 9, "ram": {"percent": 33}}}}}
    p = infra.metricas_de(ahora, viejo)
    assert p.cpu_host is None and p.temp_amb == 24.0
    assert p.temperaturas == {"CPU": 50.0}
    assert p.vms == {"VM1": {"cpu": 9, "ram": 33}}


def test_serie_infra_rango_limite_y_submuestreo(db, ahora):
    for m in range(100):
        _reporte(db, "H01", ahora - timedelta(minutes=m), _json_agente(m, 50, [("CPU1", 40)]))
    db.commit()
    serie = infra.serie_infra(db, "H01", ahora - timedelta(minutes=49), ahora - timedelta(minutes=10))
    assert [p.cpu_host for p in serie] == [float(m) for m in range(49, 9, -1)]   # cronológico, bordes incluidos
    assert len(infra.serie_infra(db, "H01", ahora - timedelta(hours=2), limite=30)) == 30
    assert len(infra.serie_infra(db, "H01", ahora - timedelta(hours=2), max_puntos=25)) == 25
    assert infra.serie_infra(db, "NUNCA", ahora - timedelta(hours=2)) == []


def test_ultimo_reporte_hasta(db, ahora):
    _reporte(db, "H01", ahora - timedelta(days=2), {"v": "viejo"})
    _reporte(db, "H01", ahora, {"v": "nuevo"})
    db.commit()
    assert infra.ultimo_reporte(db, "H01", hasta=ahora - timedelta(days=1)).data == {"v": "viejo"}
