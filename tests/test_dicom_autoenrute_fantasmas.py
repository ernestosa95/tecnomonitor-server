"""Reglas de autoenrute que dejan de reportarse e índice desactualizado (caso P03, 2026-10-01)."""
from datetime import timedelta

import database
from alerts_engine.software import dicom_autoenrute as d

CFG = {"dicom_stall_warning_minutes": 30, "dicom_stall_critical_minutes": 180,
       "dicom_min_instances": 50, "dicom_drain_percent": 70, "dicom_baseline_enabled": False}
STALE0 = {"enabled": True, "status": "stale", "total": 0, "errors": 1}
STALE1 = {"enabled": True, "status": "stale", "total": 1, "errors": 1}
ERR = {"enabled": True, "status": "error", "total": 0, "errors": 1}


def _escenario(db, ahora):
    def hosp(hid, hace_min, meta):
        db.add(database.HospitalMetadata(hospital_id=hid, nombre=hid, is_visible=True, alerts_enabled=True))
        for i in range(60):
            db.add(database.ReporteModel(hospital_id=hid, timestamp=ahora - timedelta(minutes=hace_min + i * 6),
                                         full_json_data={"collection_meta": {"dicom_routing": meta}}))

    def regla(hid, rid, hasta, desde, valor=lambda i: 19000 + i):
        for i, m in enumerate(range(hasta, desde + 1, 6)):
            db.add(database.SoftwareMonitoring(
                hospital_id=hid, app_name="dicom_routing", component_id=str(rid), metric_value=valor(i),
                extra_data={"label": "TODOS → ENTP", "to_nickname": "ENTP", "from_key": None},
                timestamp=ahora - timedelta(minutes=m)))

    def alerta(hid, rid):
        db.add(database.AlertaModel(hospital_id=hid, tipo=f"DICOM_ROUTE_{rid}", mensaje="[CRITICAL] x",
                                    start_time=ahora - timedelta(days=2), is_active=1))

    hosp("P03", 1, STALE0); regla("P03", 1, 300, 600); alerta("P03", 1)          # única regla borrada
    hosp("H02", 1, STALE1); regla("H02", 7, 200, 600); alerta("H02", 7)          # una de dos borrada
    regla("H02", 8, 1, 400, valor=lambda i: 20000 - i); alerta("H02", 8)         # la otra, trabada
    hosp("H03", 120, STALE0); regla("H03", 3, 300, 600); alerta("H03", 3)        # hospital OFFLINE
    hosp("H04", 1, ERR); regla("H04", 4, 300, 600); alerta("H04", 4)             # Elastic inaccesible
    db.commit()


def test_reglas_fantasma_e_indice(db, ahora, sin_asana):
    _escenario(db, ahora)
    d.verificar_autoenrute_dicom(db, CFG, db.query(database.HospitalMetadata).all())
    res = {(a.hospital_id, a.tipo): a for a in db.query(database.AlertaModel).all()}

    p03 = res[("P03", "DICOM_ROUTE_1")]
    assert p03.is_active == 0 and p03.mensaje.startswith("[BAJA]")
    assert res[("P03", d.TIPO_INDICE)].is_active == 1

    h02_borrada = res[("H02", "DICOM_ROUTE_7")]
    assert h02_borrada.is_active == 0 and "sin lecturas desde" in h02_borrada.mensaje
    assert res[("H02", "DICOM_ROUTE_8")].is_active == 1
    assert ("H02", d.TIPO_INDICE) not in res

    assert res[("H03", "DICOM_ROUTE_3")].is_active == 1
    assert res[("H04", "DICOM_ROUTE_4")].is_active == 1
    assert ("H04", d.TIPO_INDICE) not in res


def test_evaluar_indice():
    assert d.evaluar_indice([]) is None
    assert d.evaluar_indice([STALE0, STALE0]) is None              # pocos reportes
    assert d.evaluar_indice([STALE0, STALE0, STALE0]) == "WARNING"
    assert d.evaluar_indice([STALE0, STALE0, STALE1]) == "OK"      # el último tiene reglas vigentes
