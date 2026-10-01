import json
from datetime import datetime, timedelta

import database
from datos import uso


def _uso(db, hid, insertado, evento=None, ris=(), pacs=()):
    m = {"ris": list(ris), "pacs": list(pacs)}
    if evento:
        m["start_time_extraction"] = evento.strftime("%Y-%m-%dT%H:%M:%S")
    db.add(database.ReporteUso(hospital_id=hid, timestamp=insertado, kpi_json_data=json.dumps(m)))


def test_fecha_evento():
    ts = datetime(2026, 9, 10, 12, 0, 5)
    assert uso.fecha_evento({"start_time_extraction": "2026-09-01T00:00:00"}, ts) == datetime(2026, 9, 1)
    assert uso.fecha_evento({"start_time_extraction": "2026-09-01T00:00:00-03:00"}, ts) == datetime(2026, 9, 1)
    assert uso.fecha_evento({"start_time_extraction": "basura"}, ts) == ts
    assert uso.fecha_evento({}, "2026-09-10 12:00:05.123") == datetime(2026, 9, 10, 12, 0, 5, 123000)


def test_reportes_uso_y_por_evento(db):
    d = datetime(2026, 9, 1)
    _uso(db, "H01", d + timedelta(days=1), evento=d)                           # normal: un día de atraso
    _uso(db, "H01", d + timedelta(days=2), evento=d - timedelta(days=30))     # backfill de un mes viejo
    _uso(db, "H01", d - timedelta(days=2), evento=d - timedelta(days=2))      # antes del rango
    _uso(db, "H01", d + timedelta(days=11), evento=d + timedelta(days=10))
    db.add(database.ReporteUso(hospital_id="H01", timestamp=d - timedelta(days=10), kpi_json_data=None))
    db.commit()

    assert len(uso.reportes_uso(db, "H01")) == 5
    assert len(uso.reportes_uso(db, "H01", desde=d + timedelta(days=2))) == 2   # por inserción

    en_rango = uso.reportes_uso_por_evento(db, "H01", d, d + timedelta(days=5))
    assert [r.fecha_evento for r in en_rango] == [d]                            # sin el backfill viejo
    assert [r.fecha_evento for r in uso.reportes_uso_por_evento(db, "H01", d)] == [d, d + timedelta(days=10)]
    assert uso.reportes_uso(db, "NUNCA") == []

    sin_json = uso.reportes_uso(db, "H01")[0]                                   # sin JSON: fecha de inserción
    assert sin_json.metrics == {} and sin_json.fecha_evento == d - timedelta(days=10)


def test_pdf_clinico_aet_con_nombre_de_equipo_sin_importar_el_orden(db, monkeypatch):
    """El PACS de un AET se suma a su equipo del RIS aunque el reporte del RIS llegue después."""
    from types import SimpleNamespace

    import generator_report
    from reportlab.platypus import Table

    monkeypatch.setattr(generator_report, "asana_conector", SimpleNamespace(adjuntar_pdf_a_tarea=lambda *a, **k: None))
    d = datetime(2026, 9, 1)
    db.add(database.HospitalMetadata(hospital_id="H03", nombre="H03"))
    _uso(db, "H03", d + timedelta(days=1), evento=d, pacs=[{"aet": "CTRIVA01", "mod": "CT", "almacenados": 270}])
    _uso(db, "H03", d + timedelta(days=2), evento=d + timedelta(days=1),
         ris=[{"aet": "CTRIVA01", "equipo": "TOMO CANON", "mod": "CT", "totales": 5}],
         pacs=[{"aet": "CTRIVA01", "mod": "CT", "almacenados": 195}])
    db.commit()

    original = generator_report.datos_uso.reportes_uso_por_evento
    for invertir in (False, True):
        monkeypatch.setattr(generator_report.datos_uso, "reportes_uso_por_evento",
                            lambda *a, _inv=invertir, **k: original(*a, **k)[::-1] if _inv else original(*a, **k))
        tablas = []
        monkeypatch.setattr(generator_report, "Table", lambda data, *a, **k: tablas.append(data) or Table(data, *a, **k))
        r = generator_report.generar_pdf_clinico(
            SimpleNamespace(hospital_id="H03", fecha_desde="2026-09-01", fecha_hasta="2026-09-30",
                            asana_task_id=None, alcance="total"), db)
        assert "pdf_bytes" in r
        celdas = {str(c) for t in tablas for fila in t for c in fila}
        assert "TOMO CANON" in celdas and "CTRIVA01" not in celdas
