"""
Ingesta sobre Postgres (datos.escritura). Necesita un Postgres con TimescaleDB:

    TM_PG_TEST=postgresql://postgres:local@127.0.0.1:5433/postgres python3 -m pytest tests/test_escritura_pg.py

Crea y borra una base `tm_pytest` en ese server. Sin TM_PG_TEST, se saltea.
"""
import os
import subprocess
import sys
from datetime import datetime, timedelta

import pytest

ADMIN = os.environ.get("TM_PG_TEST")
pytestmark = pytest.mark.skipif(not ADMIN, reason="sin TM_PG_TEST (Postgres de prueba)")

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def pg():
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    admin = create_engine(ADMIN, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS tm_pytest WITH (FORCE)"))
        c.execute(text("CREATE DATABASE tm_pytest"))
    dsn = ADMIN.rsplit("/", 1)[0] + "/tm_pytest"
    subprocess.run([sys.executable, os.path.join(RAIZ, "herramientas", "migracion_pg", "instalar_esquema.py"),
                    "--dsn", dsn], check=True, capture_output=True)
    eng = create_engine(dsn)
    yield sessionmaker(bind=eng)
    eng.dispose()
    with admin.connect() as c:
        c.execute(text("DROP DATABASE IF EXISTS tm_pytest WITH (FORCE)"))


def _reporte(cpu=10.0, disco="C:"):
    return {"envelope": {"hospital_id": "H03", "agent_version": "4.1"},
            "collection_meta": {"wmi": {"enabled": True, "status": "ok"}},
            "physical_layer": {"telemetry": {"cpu": {"usage_percent": cpu}}, "sensors": {"status": "OK"}},
            "virtual_layer": [{"id": "APPV", "state": "Online",
                               "storage": [{"mount_point": disco, "total_gb": 100, "free_gb": 50, "usage_percent": 50}]}]}


def test_reloj_corrido_usa_la_hora_del_server(pg):
    from datos import escritura
    db = pg()
    ahora = datetime(2026, 10, 1, 10, 0)
    assert escritura.hora_del_reporte(db, "H03", ahora + timedelta(hours=4), ahora) == ahora
    assert escritura.hora_del_reporte(db, "H03", ahora + timedelta(minutes=5), ahora) == ahora + timedelta(minutes=5)


def test_inventario_estado_actual_y_reporte_atrasado(pg):
    from sqlalchemy import text
    from datos import escritura, infra
    db = pg()
    t0 = datetime(2026, 10, 1, 10, 0)
    escritura.guardar_reporte(db, "H03", t0, "OK", 10, 1, 0, _reporte(cpu=10))
    escritura.guardar_reporte(db, "H03", t0 + timedelta(minutes=5), "OK", 20, 1, 0, _reporte(cpu=20))   # solo métricas
    escritura.guardar_reporte(db, "H03", t0 + timedelta(minutes=10), "OK", 30, 1, 0, _reporte(cpu=30, disco="D:"))
    escritura.guardar_reporte(db, "H03", t0 + timedelta(minutes=7), "OK", 99, 1, 0, _reporte(cpu=99, disco="Z:"))  # atrasado
    db.commit()
    versiones = db.execute(text("SELECT vigente_desde, vigente_hasta FROM inventario WHERE hospital_id = 'H03' "
                                "ORDER BY vigente_desde")).fetchall()
    assert len(versiones) == 2 and versiones[0].vigente_hasta == versiones[1].vigente_desde and versiones[1].vigente_hasta is None
    assert infra.ultimo_timestamp(db, "H03") == t0 + timedelta(minutes=10)          # el atrasado no pisa el estado actual
    assert infra.ultimo_reporte(db, "H03").data["physical_layer"]["telemetry"]["cpu"]["usage_percent"] == 30
    assert [p.cpu_host for p in infra.serie_infra(db, "H03", t0)] == [10, 20, 99, 30]   # pero sus métricas quedan


def test_software_deduplicacion_y_backups(pg):
    from datos import escritura, software as sw
    db = pg()
    t = datetime(2026, 10, 1, 9, 0)
    escritura.guardar_lectura(db, "H05", "patient_portal", "MPS:9", "BURNER", 8, {"origin": "MPS", "code": "9"}, t)
    escritura.guardar_lectura(db, "H05", "sql_integrity", "BD1", "OK", 0, {"detail": ""}, t)
    escritura.guardar_lectura(db, "H05", "sql_backup", "BD1", "BACKUP", 0, {"last_full": "x", "last_seen": "a"}, t)
    db.commit()
    assert escritura.existe_lectura(db, "H05", "patient_portal", t)
    assert not escritura.existe_lectura(db, "H05", "patient_portal", t + timedelta(minutes=5))
    assert escritura.existe_lectura(db, "H05", "sql_integrity", t, componente="BD1")
    assert not escritura.existe_lectura(db, "H05", "sql_integrity", t, componente="BD2")
    manija, extra = escritura.ultima_lectura_sql(db, "H05", "sql_backup", "BD1")
    escritura.actualizar_extra_sql(db, manija, {**extra, "last_seen": "b"})
    db.commit()
    assert sw.ultimas_lecturas(db, "H05", sw.SQL_BACKUP, por_id=True)[0].extra_data["last_seen"] == "b"
