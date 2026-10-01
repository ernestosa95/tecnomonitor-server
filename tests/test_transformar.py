import copy
from datetime import datetime, timedelta, timezone

from datos.transformar import transformar

TS = datetime(2026, 10, 1, 13, 0, tzinfo=timezone.utc)


def _reporte(cpu=13.8, ram=82.5, vm_cpu=69.0, libre=268.7, handles=1305, uptime=1000, error_raid=None, estado="Online"):
    return {
        "envelope": {"agent_version": "4.5.4", "hospital_id": "P03", "timestamp": "2026-10-01T10:00:00"},
        "collection_meta": {"wmi": {"enabled": True, "status": "ok"}},
        "physical_layer": {
            "host_info": {"hostname": "pve", "type": "proxmox", "uptime_seconds": 3600},
            "telemetry": {"cpu": {"usage_percent": cpu}, "ram": {"total_gb": 125.5, "used_gb": 103.6, "usage_percent": ram}},
            "sensors": {"status": "OK",
                        "temperatures": [{"name": "CPU1 Temp", "value": 44, "unit": "C", "status": "OK"}],
                        "fans": [{"name": "Fan1A", "value": 6120, "unit": "RPM", "status": "OK"}],
                        "power": {"watts_current": 295, "supplies": [{"name": "PS1", "watts": 264.0, "status": "OK"}]}},
            "storage_layer": {"controllers": [{"name": "RAID.Slot.3-1", "status": "OK"}],
                              **({"error": error_raid} if error_raid else {})},
            "network_health": {"cloud_latency_ms": 43.6, "upload_usage_mbps": 0.1, "download_usage_mbps": 2.0,
                               "last_check": "2026-10-01T10:00:05", "cloud_status": "conectado"},
        },
        "virtual_layer": [{
            "id": "APPV", "type": "vm", "state": estado, "state_reason": "ok",
            "telemetry": {"cpu": {"usage_percent": vm_cpu}, "ram": {"total_gb": 48, "used_gb": 43.7, "usage_percent": 91.1},
                          "uptime_seconds": uptime},
            "storage": [{"mount_point": "C:", "total_gb": 449.9, "free_gb": libre, "usage_percent": 40.3,
                         "performance": {"latency_ms": 0.0, "status": "OK"}}],
            "application_layer": {"services": [{"name": "MSSQLSERVER", "state": "Running",
                                                "vital_signs": {"pid": 1, "health": "OK", "cpu_percent": 0.5,
                                                                "ram_mb": 5655.4, "threads": 176, "handles": handles}}]},
        }],
        "application_metrics": None,
    }


def test_extrae_metricas():
    original = _reporte()
    f = transformar(TS, original, "OK")
    assert original == _reporte()                                  # no modifica el reporte
    assert f.host["cpu_pct"] == 13.8 and f.host["ram_pct"] == 82.5 and f.host["potencia_w"] == 295
    assert f.host["latencia_ms"] == 43.6 and f.host["host_status"] == "OK"
    assert f.host["arranque"] == TS - timedelta(hours=1)
    assert {(s["tipo"], s["nombre"], s["orden"], s["valor"]) for s in f.sensores} == {
        ("temp", "CPU1 Temp", 0, 44.0), ("fan", "Fan1A", 0, 6120.0), ("psu", "PS1", 0, 264.0)}
    assert f.vms == [{"vm": "APPV", "origen": "virtual_layer", "orden": 0, "cpu_pct": 69.0, "ram_pct": 91.1, "ram_usada_gb": 43.7,
                      "arranque": datetime(2026, 10, 1, 12, 43, tzinfo=timezone.utc),   # 12:43:20 -> 12:43
                      "estado": "Online", "motivo": "ok", "error": None}]
    assert f.discos == [{"vm": "APPV", "montaje": "C:", "uso_pct": 40.3, "libre_gb": 268.7, "latencia_ms": 0.0}]
    assert f.servicios[0]["handles"] == 1305 and f.servicios[0]["hilos"] == 176
    assert f.meta == {"wmi": {"enabled": True, "status": "ok"}}


def test_inventario_estable_si_solo_cambian_metricas():
    a = transformar(TS, _reporte(), "OK")
    b = transformar(TS + timedelta(minutes=5), _reporte(cpu=55, ram=10, vm_cpu=1, libre=100, handles=9, uptime=1300), "OK")
    assert a.inventario_hash == b.inventario_hash
    inv = a.inventario
    assert "collection_meta" not in inv and "timestamp" not in inv["envelope"]
    assert inv["physical_layer"]["telemetry"] == {"ram": {"total_gb": 125.5}}
    assert inv["virtual_layer"][0]["storage"][0] == {"mount_point": "C:", "total_gb": 449.9,
                                                     "performance": {"status": "OK"}}
    assert "state" not in inv["virtual_layer"][0]


def test_inventario_cambia_con_el_hardware_y_normaliza_errores():
    a = transformar(TS, _reporte(error_raid="timeout (<Conn at 0x7f00aa>)"), "OK")
    b = transformar(TS, _reporte(error_raid="timeout (<Conn at 0x7f99ff>)"), "OK")
    assert a.inventario_hash == b.inventario_hash                    # misma falla, otra dirección de memoria
    c = _reporte()
    c["virtual_layer"][0]["storage"].append({"mount_point": "D:", "total_gb": 900, "free_gb": 1, "usage_percent": 99})
    assert transformar(TS, c, "OK").inventario_hash != transformar(TS, _reporte(), "OK").inventario_hash


def test_variantes_viejas_del_agente():
    d = _reporte()
    d["physical_layer"]["storage"] = d["physical_layer"].pop("storage_layer")
    d["physical_layer"]["storage"]["error"] = "x at 0xabc"
    d["physical_layer"]["vms"] = [{"id": "ESX-VM1", "type": "vm", "state": "on",
                                   "telemetry": {"cpu": {"usage_percent": 7}, "ram": {"usage_percent": 30, "used_gb": 2}}}]
    f = transformar(TS, copy.deepcopy(d), None)
    assert f.inventario["physical_layer"]["storage"]["error"] == "x"
    assert {(v["vm"], v["origen"]) for v in f.vms} == {("APPV", "virtual_layer"), ("ESX-VM1", "hipervisor")}
    assert transformar(TS, {}, None).host["cpu_pct"] is None


def test_software_por_app():
    from zoneinfo import ZoneInfo

    from datos.transformar import software
    z = ZoneInfo("America/Argentina/Buenos_Aires")
    t, f = software("mirth", "[SE] IN", "STARTED", 3, {"instancia": "SE", "recibidos": 912, "enviados": 84,
                                                     "errored": 2, "channel_id": "a9e", "last_error": "x"}, z)
    assert t == "mirth_canal_metricas" and f["encolados"] == 3 and f["errores"] == 2 and f["componente"] == "[SE] IN"
    t, f = software("dicom_routing", "51", "OK", 7, {"label": "A → B", "from_key": None, "to_key": 51,
                                                     "to_nickname": "B"}, z)
    assert t == "cola_dicom_metricas" and f["pendientes"] == 7 and f["_regla"]["destino_key"] == 51
    t, f = software("patient_portal", "MPS:9", "BURNER", 8, {"origin": "MPS", "code": 9, "oldest": "2026-09-02T08:45:37"}, z)
    assert t == "portal_estado_metricas" and f["codigo"] == "9"
    assert f["mas_antiguo"] == datetime(2026, 9, 2, 11, 45, 37, tzinfo=timezone.utc)   # -03 -> UTC
    assert software("sql_backup", "BD1", "BACKUP", 0, {"last_full": "x"}, z)[0] == "sql_eventos"
    assert software("elasticsearch", "ERR-1", "LOW", 1, {"titulo": "t"}, z)[0] == "software_eventos"


def test_kpis_conserva_orden_y_fechas():
    from zoneinfo import ZoneInfo

    from datos.transformar import kpis
    m = {"extraction_interval_hours": 24.0, "start_time_extraction": "2026-09-30T00:00:00",
         "end_time_extraction": "2026-10-01T00:00:00",
         "ris": [{"equipo": "TOMO", "aet": "CT1", "mod": "CT", "totales": 5, "admitidos": 4}],
         "pacs": [{"aet": "B", "mod": "CR", "almacenados": 1}, {"aet": "A", "mod": "CT", "almacenados": 20}],
         "users": [{"rol": "Nurse", "usuarios_unicos": 9, "inicios_sesion": 16}]}
    cab, ris, pacs, usuarios = kpis(m, ZoneInfo("America/Argentina/Buenos_Aires"))
    assert cab["desde"] == datetime(2026, 9, 30, 3, 0, tzinfo=timezone.utc) and cab["intervalo_horas"] == 24.0
    assert ris[0]["modalidad"] == "CT" and ris[0]["admitidos"] == 4 and ris[0]["citados"] is None
    assert [(p["orden"], p["aet"]) for p in pacs] == [(0, "B"), (1, "A")]
    assert usuarios[0]["inicios_sesion"] == 16
