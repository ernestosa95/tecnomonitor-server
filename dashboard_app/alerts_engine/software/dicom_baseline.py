"""
Piso habitual por regla de autoenrute DICOM (baseline adaptativo).

Hay reglas con un residuo constante e inmóvil (ej. 1164 pendientes durante
días) sobre el que llegan tandas que drenan en minutos. La cola plana en su
piso se veía como "sin drenaje" y abría el ticket; llegaba una tanda, drenaba,
cerraba, y volvía a abrir (cientos de reaperturas en 10 días). Con el piso de
cada regla, el detector evalúa solo el EXCESO por encima de su piso habitual.

Guardas para no "aprender" un incidente como normal:
  - Solo reglas con actividad demostrada (>= 5 bajadas de >= 300 en 7 días).
    Una regla que nunca drenó (plana en 15 000 durante días) no tiene piso.
  - El baseline se congela mientras la regla tiene una alerta abierta.
  - El piso solo puede SUBIR una vez cada 24 h y hasta un 25% por vez; bajar
    es inmediato.
  - `dicom_baseline_enabled = 0` en `configuracion` lo apaga por completo.

Ver docs/12-ultima-milla-alertas-asana.md §3quater.
"""
from datetime import datetime, timedelta

from sqlalchemy import func, text

import database

from ..estado import _parsear_timestamp

DIAS_HISTORIA = 7
MIN_HORAS_HISTORIA = 23
MIN_MUESTRAS = 150
PERCENTIL_PISO = 10
BAJADA_MIN = 300
MIN_BAJADAS = 5
TOLERANCIA_ABS = 300
TOLERANCIA_REL = 0.15

REFRESCO_HORAS = 6
MAX_HOSPITALES_POR_TICK = 3
TOPE_SUBIDA = 1.25
SUBIDA_MIN_ABS = 100
ESPERA_SUBIDA_HORAS = 24

_ultimo_calculo = {}   # hospital_id -> datetime del último recálculo (memoria del proceso)
_sembrado = False


def _percentil(ordenados, p):
    k = (len(ordenados) - 1) * p / 100.0
    f = int(k)
    c = min(f + 1, len(ordenados) - 1)
    return ordenados[f] + (ordenados[c] - ordenados[f]) * (k - f)


def calcular_baseline(valores, timestamps):
    """Función pura. Devuelve {piso, tolerancia, activa, muestras} o None si falta historia."""
    if len(valores) < MIN_MUESTRAS:
        return None
    if timestamps[-1] - timestamps[0] < timedelta(hours=MIN_HORAS_HISTORIA):
        return None
    piso = int(_percentil(sorted(valores), PERCENTIL_PISO))
    bajadas = sum(1 for a, b in zip(valores, valores[1:]) if a - b >= BAJADA_MIN)
    return {
        "piso": piso,
        "tolerancia": max(TOLERANCIA_ABS, int(TOLERANCIA_REL * piso)),
        "activa": bajadas >= MIN_BAJADAS,
        "muestras": len(valores),
    }


def cargar_baselines(db, hospital_id):
    """
    {component_id: {piso, tolerancia, activa}} de las reglas de un hospital.
    Si la tabla no se puede leer (deploy a medias, DB bloqueada) devuelve {}:
    el baseline es una mejora opcional y su falla no puede tumbar el detector
    ni el panel; se cae al criterio sin piso por regla.
    """
    try:
        filas = db.query(database.DicomReglaBaseline).filter(
            database.DicomReglaBaseline.hospital_id == hospital_id
        ).all()
    except Exception as e:
        db.rollback()
        print(f"⚠️ [DICOM baseline] No se pudieron leer los pisos de {hospital_id}: {repr(e)}")
        return {}
    return {
        f.component_id: {"piso": f.piso or 0, "tolerancia": f.tolerancia or TOLERANCIA_ABS,
                         "activa": bool(f.activa)}
        for f in filas
    }


def _recalcular_hospital(db, hospital_id, ahora):
    desde = ahora - timedelta(days=DIAS_HISTORIA)
    filas = db.execute(text("""
        SELECT component_id, metric_value, timestamp
        FROM software_monitoring
        WHERE hospital_id = :hid
          AND app_name = 'dicom_routing'
          AND timestamp >= :desde
        ORDER BY component_id, timestamp ASC
    """), {"hid": hospital_id, "desde": desde}).fetchall()

    por_regla = {}
    for f in filas:
        ts = _parsear_timestamp(f.timestamp)
        if ts is None:
            continue
        try:
            valor = int(f.metric_value or 0)
        except (ValueError, TypeError):
            valor = 0
        por_regla.setdefault(f.component_id, []).append((ts, valor))
    if not por_regla:
        return

    abiertas = {
        t for (t,) in db.query(database.AlertaModel.tipo).filter(
            database.AlertaModel.hospital_id == hospital_id,
            database.AlertaModel.is_active == 1,
            database.AlertaModel.tipo.like("DICOM_ROUTE_%"),
        ).all()
    }
    existentes = {
        f.component_id: f for f in db.query(database.DicomReglaBaseline).filter(
            database.DicomReglaBaseline.hospital_id == hospital_id
        ).all()
    }

    for cid, puntos in por_regla.items():
        # Congelado: una regla con alerta abierta no se re-aprende.
        if f"DICOM_ROUTE_{str(cid)[:30]}" in abiertas:
            continue
        puntos.sort(key=lambda p: p[0])
        bl = calcular_baseline([p[1] for p in puntos], [p[0] for p in puntos])
        if bl is None:
            continue

        fila = existentes.get(cid)
        piso = bl["piso"]
        subido_en = fila.piso_subido_en if fila else None
        if fila is not None and piso > (fila.piso or 0):
            viejo = fila.piso or 0
            if subido_en and ahora - subido_en < timedelta(hours=ESPERA_SUBIDA_HORAS):
                piso = viejo
            else:
                piso = min(piso, max(int(viejo * TOPE_SUBIDA), viejo + SUBIDA_MIN_ABS))
                subido_en = ahora

        if fila is None:
            fila = database.DicomReglaBaseline(hospital_id=hospital_id, component_id=cid)
            db.add(fila)
        fila.piso = piso
        fila.tolerancia = max(TOLERANCIA_ABS, int(TOLERANCIA_REL * piso))
        fila.activa = bl["activa"]
        fila.muestras = bl["muestras"]
        fila.calculado_en = ahora
        fila.piso_subido_en = subido_en


def refrescar_baselines(db, hospitales):
    """
    Recalcula, de a pocos hospitales por tick y cada REFRESCO_HORAS, los pisos
    de las reglas. Lo llama el detector de autoenrute antes de evaluar.
    """
    global _sembrado
    ahora = datetime.now()

    if not _sembrado:
        # Tras un reinicio no recalculamos todo de golpe: partimos de la última
        # vez que quedó grabado cada hospital.
        for hid, ultimo in db.query(
            database.DicomReglaBaseline.hospital_id,
            func.max(database.DicomReglaBaseline.calculado_en),
        ).group_by(database.DicomReglaBaseline.hospital_id).all():
            if ultimo:
                _ultimo_calculo[hid] = ultimo
        _sembrado = True

    limite = timedelta(hours=REFRESCO_HORAS)
    pendientes = [h for h in hospitales
                  if ahora - _ultimo_calculo.get(h.hospital_id, datetime.min) >= limite]
    pendientes.sort(key=lambda h: _ultimo_calculo.get(h.hospital_id, datetime.min))

    for hosp in pendientes[:MAX_HOSPITALES_POR_TICK]:
        try:
            _recalcular_hospital(db, hosp.hospital_id, ahora)
            db.commit()
            _ultimo_calculo[hosp.hospital_id] = ahora
        except Exception as e:
            db.rollback()
            # Reintento en ~1 h en vez de en el próximo tick.
            _ultimo_calculo[hosp.hospital_id] = ahora - limite + timedelta(hours=1)
            print(f"⚠️ [DICOM baseline] Falló el recálculo de {hosp.hospital_id}: {repr(e)}")
