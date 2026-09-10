"""
Detector de autoenrute DICOM — detección por falta de drenaje.

Por qué NO hay umbral absoluto de instancias:

pending_instances cuenta INSTANCIAS (imágenes), no estudios. Una TC de
1.500 cortes recién enviada dispara cualquier umbral razonable aunque el
enrute funcione perfecto. Además la línea de base varía por hospital y por
regla: vimos rutas sanas oscilando entre 500 y 2.000 y otras con picos de
14.000. Un número fijo o alerta siempre o no alerta nunca.

Lo que SÍ distingue una ruta sana de una trabada es la forma de la curva:
  - sana    -> serrucho: se llena y drena, se llena y drena
  - trabada -> sube y se queda arriba, o crece de forma monótona

La regla entonces mira si la cola BAJÓ en algún momento de la ventana.
Es relativa a cada regla, así que se autocalibra y sirve igual para el
hospital que mueve 500 instancias que para el que mueve 15.000.

Nota de dependencia externa: `_serie_de`, `_ventana` y `_drena` también los
usa dashboard_app/routers/hospital_detalle.py para mostrar el mismo estado
de salud en el panel de detalle del hospital (mismo criterio, no una copia
local). Se re-exportan desde alerts_engine/__init__.py por eso.

Ver docs/09-plan-refactor-alertas.md.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy import text

from ..config import _followers_de
from ..estado import _parsear_timestamp, actualizar_estado_alerta


def _serie_de(historia):
    """Convierte las filas de software_monitoring en (valores, timestamps) cronológicos."""
    puntos = []
    for row in historia:
        ts = _parsear_timestamp(row.timestamp)
        if ts is None:
            continue
        try:
            valor = int(row.metric_value or 0)
        except (ValueError, TypeError):
            valor = 0
        puntos.append((ts, valor))
    puntos.sort(key=lambda p: p[0])
    return [p[1] for p in puntos], [p[0] for p in puntos]


def _drena(valores, drain_percent):
    """
    Verifica si la cola tiene actividad saludable (no está estancada).
    Una cola es saludable si logra drenar desde su pico, O si está en pleno
    ingreso de un estudio masivo (creciendo activamente).
    """
    if len(valores) < 2:
        return True  # Sin evidencia suficiente, no acusamos

    pico = max(valores)
    minimo = min(valores)
    actual = valores[-1]

    if pico <= 0:
        return True

    # Criterio 1: Caída porcentual (logró bajar al % esperado desde el pico)
    if actual <= pico * (drain_percent / 100.0):
        return True

    # Criterio 2: Drenaje absoluto. Si despachó al menos 300 imágenes, está viva.
    if (pico - actual) >= 300:
        return True

    # Criterio 3: Crecimiento activo. Si subió al menos 300 imágenes desde
    # el piso de esta ventana, significa que está recibiendo un estudio masivo.
    # No es un estancamiento, es el buffer haciendo su trabajo natural.
    if (actual - minimo) >= 300:
        return True

    # Si no bajó ni subió significativamente, es una verdadera línea plana (estancada)
    return False


def _ventana(valores, timestamps, minutos, ahora):
    """Recorta la serie a los últimos N minutos. Devuelve (valores, cubre_ventana)."""
    limite = ahora - timedelta(minutes=minutos)

    # Filtramos asegurando que el timestamp pertenece estrictamente a la ventana
    pares_validos = [(v, ts) for v, ts in zip(valores, timestamps) if ts >= limite]
    recorte = [p[0] for p in pares_validos]

    cubre = False
    if pares_validos:
        # Ahora medimos la antigüedad real del PRIMER dato DENTRO de la ventana
        ts_mas_viejo_en_ventana = pares_validos[0][1]
        antiguedad = (ahora - ts_mas_viejo_en_ventana).total_seconds() / 60.0
        cubre = antiguedad >= minutos * 0.8 and len(recorte) >= 3

    return recorte, cubre


def verificar_autoenrute_dicom(db, config, hospitales_activos):
    win_warn = int(config.get('dicom_stall_warning_minutes', 30) or 30)
    win_crit = int(config.get('dicom_stall_critical_minutes', 120) or 120)
    min_inst = int(config.get('dicom_min_instances', 50) or 0)
    drain_pct = int(config.get('dicom_drain_percent', 70) or 70)

    asana_followers = _followers_de(db, config, 'dicom_responsible_email')
    ahora = datetime.now()

    # Traemos con margen: la ventana crítica más un 50% para tolerar huecos.
    desde = ahora - timedelta(minutes=win_crit * 1.5)

    for hosp in hospitales_activos:
        query = text("""
            SELECT component_id, metric_value, extra_data, timestamp
            FROM software_monitoring
            WHERE hospital_id = :hid
              AND app_name = 'dicom_routing'
              AND timestamp >= :desde
            ORDER BY component_id, timestamp ASC
        """)
        registros = db.execute(
            query, {"hid": hosp.hospital_id, "desde": desde}
        ).fetchall()

        if not registros:
            continue

        por_regla = {}
        for reg in registros:
            por_regla.setdefault(reg.component_id, []).append(reg)

        for id_rule, historia in por_regla.items():
            valores, timestamps = _serie_de(historia)
            if not valores:
                continue

            actual = valores[-1]

            # Etiqueta legible para el ticket. El extra_data lo escribe main.py.
            ruta = f"regla #{id_rule}"
            try:
                extra = historia[-1].extra_data
                if isinstance(extra, str):
                    extra = json.loads(extra)
                if isinstance(extra, dict) and extra.get("ruta"):
                    ruta = extra["ruta"]
            except Exception:
                pass

            tipo_alerta = f"DICOM_ROUTE_{str(id_rule)[:30]}"

            # --- Caso 1: cola vacía o por debajo del piso de ruido -> OK ---
            # Emitimos OK explícito para que se cierre el incidente y el ticket.
            if actual <= 0 or actual < min_inst:
                actualizar_estado_alerta(
                    db=db, hid=hosp.hospital_id, tipo_unico=tipo_alerta,
                    nivel="OK",
                    mensaje=f"Cola de autoenrute {ruta} normalizada ({actual} instancias pendientes).",
                    asana_proj_id=hosp.asana_project_id,
                    asana_followers=asana_followers
                )
                continue

            v_crit, cubre_crit = _ventana(valores, timestamps, win_crit, ahora)
            v_warn, cubre_warn = _ventana(valores, timestamps, win_warn, ahora)

            # --- Caso 2: sin historia suficiente para juzgar ---
            # No emitimos nada: ni alerta (sería falso positivo) ni OK (sería
            # cerrar un incidente real por falta de datos). Pasa con agentes
            # recién instalados o después de un corte de reporte.
            if not cubre_warn:
                continue

            drena_warn = _drena(v_warn, drain_pct)
            drena_crit = _drena(v_crit, drain_pct) if cubre_crit else True

            if not drena_crit:
                nivel = "CRITICAL"
                tendencia = "creciendo" if actual >= v_crit[0] else "sin drenar"
                mensaje = (
                    f"Cola de autoenrute {ruta} {tendencia} hace más de {win_crit} min "
                    f"sin descender: {actual} instancias pendientes "
                    f"(mínimo en la ventana: {min(v_crit)}). "
                    f"Revisar el nodo destino."
                )
            elif not drena_warn:
                nivel = "WARNING"
                tendencia = "creciendo" if actual >= v_warn[0] else "estancada"
                mensaje = (
                    f"Cola de autoenrute {ruta} {tendencia} hace {win_warn} min: "
                    f"{actual} instancias pendientes "
                    f"(mínimo en la ventana: {min(v_warn)})."
                )
            else:
                nivel = "OK"
                mensaje = (
                    f"Cola de autoenrute {ruta} drenando con normalidad "
                    f"({actual} instancias pendientes)."
                )

            actualizar_estado_alerta(
                db=db, hid=hosp.hospital_id, tipo_unico=tipo_alerta,
                nivel=nivel, mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers
            )
