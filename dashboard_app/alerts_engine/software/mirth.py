"""
Detector de Mirth Connect: canales detenidos/en error sostenidos por 2 ticks
(filtra micro-cortes) y colas con encolado por encima del umbral.

Ver docs/09-plan-refactor-alertas.md.
"""
from sqlalchemy import text

from ..config import _followers_de
from ..estado import actualizar_estado_alerta


def verificar_mirth(db, config, hospitales_activos):
    umbral_encolados = config.get('mirth_queued_threshold', 100)
    asana_followers = _followers_de(db, config, 'mirth_responsible_email')

    for hosp in hospitales_activos:
        # CORRECCIÓN 1 y 2: LIKE insensible a mayúsculas y ORDER BY explícito
        query = text("""
            WITH RankedData AS (
                SELECT component_id, status_value, metric_value, extra_data,
                       ROW_NUMBER() OVER(PARTITION BY component_id ORDER BY timestamp DESC) as rn
                FROM software_monitoring
                WHERE hospital_id = :hid AND LOWER(app_name) LIKE '%mirth%'
            )
            SELECT component_id, status_value, metric_value, extra_data, rn
            FROM RankedData
            WHERE rn <= 2
            ORDER BY component_id, rn ASC
        """)
        registros = db.execute(query, {"hid": hosp.hospital_id}).fetchall()

        historial_canales = {}
        for reg in registros:
            cid = reg.component_id
            if cid not in historial_canales:
                historial_canales[cid] = []
            historial_canales[cid].append(reg)

        for cid, historia in historial_canales.items():
            # CORRECCIÓN 3: Re-aseguramos en Python que [0] es siempre el último reporte (rn=1)
            historia.sort(key=lambda x: x.rn)

            actual = historia[0]
            estado_canal = (actual.status_value or '').upper()

            # CORRECCIÓN 4: Parseo seguro a número entero para evitar el TypeError
            try:
                encolados = int(actual.metric_value or 0)
            except (ValueError, TypeError):
                encolados = 0

            estado_anterior = (historia[1].status_value or '').upper() if len(historia) > 1 else estado_canal

            nivel = "OK"
            mensaje = ""

            if estado_canal in ['STOPPED', 'ERROR', 'PAUSED']:
                if estado_anterior in ['STOPPED', 'ERROR', 'PAUSED']:
                    nivel = "CRITICAL"
                    # Nota: Quitamos el "[CRITICAL]" redundante porque la función actualizar_estado_alerta se lo agrega sola
                    mensaje = f"Canal inoperativo de forma sostenida ({estado_canal})."
                else:
                    # Micro-corte detectado: Esperamos al próximo ciclo
                    continue

            elif encolados >= umbral_encolados:
                nivel = "CRITICAL"
                mensaje = f"Acumulación en canal: {encolados} mensajes encolados (Umbral: {umbral_encolados})."

            else:
                mensaje = f"Operando normal. Encolados: {encolados}"

            tipo_alerta = f"MIRTH_{cid[:35]}"

            actualizar_estado_alerta(
                db=db,
                hid=hosp.hospital_id,
                tipo_unico=tipo_alerta,
                nivel=nivel,
                mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers
            )
