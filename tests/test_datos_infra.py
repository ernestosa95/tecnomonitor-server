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
