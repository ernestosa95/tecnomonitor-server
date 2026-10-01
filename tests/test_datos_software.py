import json
from datetime import timedelta

from sqlalchemy import text

import database
from datos import software as sw


def _lect(db, hid, app, comp, minutos, ahora, valor=0, estado="OK", extra=None, como_texto=False):
    e = extra if extra is not None else {"n": minutos}
    ts = ahora - timedelta(minutes=minutos)
    if como_texto:   # texto crudo en la columna, sin pasar por el tipo JSON del ORM
        db.execute(text("INSERT INTO software_monitoring (hospital_id, app_name, component_id, status_value, "
                        "metric_value, extra_data, timestamp) VALUES (:h, :a, :c, :s, :v, :e, :t)"),
                   {"h": hid, "a": app, "c": comp, "s": estado, "v": valor, "e": json.dumps(e),
                    "t": ts.strftime("%Y-%m-%d %H:%M:%S.%f")})
        return
    db.add(database.SoftwareMonitoring(hospital_id=hid, app_name=app, component_id=comp, status_value=estado,
                                       metric_value=valor, extra_data=e, timestamp=ts))


def test_ultimas_lecturas(db, ahora):
    for m in (30, 20, 10):
        _lect(db, "H01", sw.MIRTH, "IN", m, ahora, valor=m)
    _lect(db, "H01", sw.MIRTH, "OUT", 5, ahora, como_texto=True)          # extra_data guardado como texto
    _lect(db, "H01", sw.SSL, "web", 50, ahora)
    _lect(db, "H02", sw.MIRTH, "IN", 1, ahora)
    db.commit()

    una = sw.ultimas_lecturas(db, "H01", sw.MIRTH)
    assert [(l.component_id, l.metric_value, l.rn) for l in una] == [("IN", 10, 1), ("OUT", 0, 1)]
    assert una[1].extra_data == {"n": 5} and una[1].timestamp == ahora - timedelta(minutes=5)

    dos = sw.ultimas_lecturas(db, "H01", sw.MIRTH, n=2)
    assert [(l.component_id, l.metric_value, l.rn) for l in dos] == [("IN", 10, 1), ("IN", 20, 2), ("OUT", 0, 1)]

    varias = sw.ultimas_lecturas(db, "H01", (sw.MIRTH, sw.SSL))
    assert {(l.app_name, l.component_id) for l in varias} == {(sw.MIRTH, "IN"), (sw.MIRTH, "OUT"), (sw.SSL, "web")}


def test_ultimas_lecturas_por_id(db, ahora):
    """Backups SQL: la ingesta renueva la última fila en el lugar; manda el orden de inserción."""
    _lect(db, "H01", sw.SQL_BACKUP, "BD1", 1, ahora, extra={"orden": "vieja"})
    _lect(db, "H01", sw.SQL_BACKUP, "BD1", 60, ahora, extra={"orden": "nueva"})
    db.commit()
    assert sw.ultimas_lecturas(db, "H01", sw.SQL_BACKUP, por_id=True)[0].extra_data == {"orden": "nueva"}
    assert sw.ultimas_lecturas(db, "H01", sw.SQL_BACKUP)[0].extra_data == {"orden": "vieja"}


def test_lecturas_rango_orden_y_limite(db, ahora):
    for m in (50, 40, 30):
        _lect(db, "H01", sw.DICOM_ROUTING, "2", m, ahora)
        _lect(db, "H01", sw.DICOM_ROUTING, "1", m + 1, ahora)
    db.commit()
    desde = ahora - timedelta(minutes=45)
    cron = sw.lecturas(db, "H01", sw.DICOM_ROUTING, desde)
    assert [(l.component_id, l.extra_data["n"]) for l in cron] == [("1", 41), ("2", 40), ("1", 31), ("2", 30)]
    por_comp = sw.lecturas(db, "H01", sw.DICOM_ROUTING, desde, por_componente=True)
    assert [(l.component_id, l.extra_data["n"]) for l in por_comp] == [("1", 41), ("1", 31), ("2", 40), ("2", 30)]
    assert len(sw.lecturas(db, "H01", sw.DICOM_ROUTING, desde, limite=3)) == 3


def test_ultima_foto(db, ahora):
    for comp in ("RIS:1", "MPS:2"):
        _lect(db, "H05", sw.PORTAL, comp, 10, ahora)
        _lect(db, "H05", sw.PORTAL, comp, 5, ahora)
    db.commit()
    hora, foto = sw.ultima_foto(db, "H05", sw.PORTAL)
    assert hora == ahora - timedelta(minutes=5)
    assert sorted(l.component_id for l in foto) == ["MPS:2", "RIS:1"]
    assert sw.ultima_foto(db, "NUNCA", sw.PORTAL) == (None, [])
