"""SYSTEM_ERROR de Mirth se cierra apenas vuelven canales reales de la instancia (caso P19, 2026-10-01)."""
from datetime import timedelta

import database
from alerts_engine.software import mirth


def test_system_error_se_cierra_al_volver_canales(db, ahora, sin_asana):
    def hosp(hid):
        db.add(database.HospitalMetadata(hospital_id=hid, nombre=hid, is_visible=True, alerts_enabled=True))
        db.add(database.ReporteModel(hospital_id=hid, timestamp=ahora - timedelta(minutes=1), full_json_data={}))

    def lect(hid, cid, minutos, estado):
        for m in minutos:
            db.add(database.SoftwareMonitoring(hospital_id=hid, app_name="mirth", component_id=cid,
                   status_value=estado, metric_value=0, extra_data={}, timestamp=ahora - timedelta(minutes=m)))

    def alerta(hid, cid):
        db.add(database.AlertaModel(hospital_id=hid, tipo=f"MIRTH_{cid[:35]}", mensaje="[CRITICAL] x",
                                    start_time=ahora - timedelta(hours=1), is_active=1))

    hosp("P19"); lect("P19", "[MIRTH_SE] SYSTEM_ERROR", [40, 46], "ERROR"); alerta("P19", "[MIRTH_SE] SYSTEM_ERROR")
    lect("P19", "[MIRTH_SE] IN", [1, 7], "STARTED")
    hosp("H01"); lect("H01", "[MIRTH_A] SYSTEM_ERROR", [1, 7], "ERROR"); lect("H01", "[MIRTH_A] IN", [60, 66], "STARTED")
    hosp("H02"); lect("H02", "[B] SYSTEM_ERROR", [1, 7], "ERROR"); lect("H02", "[A] IN", [1, 7], "STARTED")
    hosp("H03"); lect("H03", "SYSTEM_ERROR", [40, 46], "ERROR"); alerta("H03", "SYSTEM_ERROR"); lect("H03", "IN", [1, 7], "STARTED")
    db.commit()

    mirth.verificar_mirth(db, {}, db.query(database.HospitalMetadata).all())
    res = {(a.hospital_id, a.tipo): a for a in db.query(database.AlertaModel).all()}

    assert res[("P19", "MIRTH_[MIRTH_SE] SYSTEM_ERROR")].is_active == 0
    assert "volvió a responder" in res[("P19", "MIRTH_[MIRTH_SE] SYSTEM_ERROR")].mensaje
    assert res[("H01", "MIRTH_[MIRTH_A] SYSTEM_ERROR")].is_active == 1    # Mirth sigue caído
    assert res[("H02", "MIRTH_[B] SYSTEM_ERROR")].is_active == 1          # otra instancia, no se mezcla
    assert res[("H03", "MIRTH_SYSTEM_ERROR")].is_active == 0              # instancia Default
