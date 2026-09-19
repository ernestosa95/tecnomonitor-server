"""
Detector de Mirth Connect: canales detenidos/en error sostenidos por 2 ticks
(filtra micro-cortes) y colas con encolado por encima del umbral -- desde el
mapa de integraciones, el umbral de cola ya no es único: depende de la
criticidad curada de cada canal (alta/media/baja), con un umbral por
defecto para canales todavía sin clasificar.

Ver docs/09-plan-refactor-alertas.md y docs/13-contrato-topologia-mirth.md.
"""
import json

from sqlalchemy import text

import database

from ..config import _followers_de
from ..estado import actualizar_estado_alerta


def _umbrales(config):
    """{'alta': {'warn':20,'crit':80}, 'media': {...}, 'baja': {...}}"""
    return {
        "alta":  {"warn": config.get("mirth_queue_warn_alta", 20),  "crit": config.get("mirth_queue_crit_alta", 80)},
        "media": {"warn": config.get("mirth_queue_warn_media", 60), "crit": config.get("mirth_queue_crit_media", 200)},
        "baja":  {"warn": config.get("mirth_queue_warn_baja", 150), "crit": config.get("mirth_queue_crit_baja", 400)},
    }


def _crit_por_hospital(db, hid):
    """
    Devuelve (mapa_crit, mapa_hum, mapa_component_a_channel) para un
    hospital: criticidad y nombre humano curados por channel_id
    (mirth_canales_meta), más el fallback component_id -> channel_id
    (mirth_channel_topology) para agentes que todavía no mandan
    `channel_id` en `extra_data`. Canales sin fila en mirth_canales_meta
    simplemente no aparecen en mapa_crit -- el caller aplica el default.
    """
    mapa_crit = {}
    mapa_hum = {}
    for fila in db.query(database.MirthCanalMeta).filter_by(hospital_id=hid).all():
        mapa_crit[fila.channel_id] = fila.crit
        if fila.hum:
            mapa_hum[fila.channel_id] = fila.hum

    mapa_component_a_channel = {}
    for fila in db.query(database.MirthChannelTopology).filter_by(hospital_id=hid).all():
        mapa_component_a_channel[fila.component_id] = fila.channel_id

    return mapa_crit, mapa_hum, mapa_component_a_channel


def verificar_mirth(db, config, hospitales_activos):
    umbrales = _umbrales(config)
    crit_default = config.get("mirth_crit_default", "media")
    warning_alert_enabled = config.get("mirth_queue_warning_alert_enabled", False)
    asana_followers = _followers_de(db, config, 'mirth_responsible_email')

    for hosp in hospitales_activos:
        mapa_crit, mapa_hum, mapa_component_a_channel = _crit_por_hospital(db, hosp.hospital_id)

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

            # Resolución de criticidad: extra_data.channel_id primero (agente
            # >= 4.5.1), si no está, fallback vía component_id -> channel_id
            # de la topología reportada. Canal sin classify -> mirth_crit_default.
            try:
                extra = json.loads(actual.extra_data) if actual.extra_data else {}
            except (TypeError, ValueError):
                extra = {}
            channel_id = extra.get("channel_id") or mapa_component_a_channel.get(cid)
            crit = mapa_crit.get(channel_id, crit_default) if channel_id else crit_default
            u = umbrales.get(crit, umbrales["media"])

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

            elif encolados >= u["crit"]:
                nivel = "CRITICAL"
                mensaje = f"Acumulación en canal: {encolados} mensajes encolados (criticidad {crit}, umbral {u['crit']})."

            elif warning_alert_enabled and encolados >= u["warn"]:
                nivel = "WARNING"
                mensaje = f"Cola en aumento: {encolados} mensajes encolados (criticidad {crit}, umbral {u['warn']})."

            else:
                mensaje = f"Operando normal. Encolados: {encolados}"

            # tipo_unico NO cambia (sigue siendo por component_id, no por
            # channel_id): cambiarlo dejaría huérfanas las alertas MIRTH_*
            # que ya estén abiertas -- se cierran por match exacto de
            # tipo_unico, y el detector dejaría de emitir el viejo.
            tipo_alerta = f"MIRTH_{cid[:35]}"
            titulo_visible = mapa_hum.get(channel_id) if channel_id else None

            actualizar_estado_alerta(
                db=db,
                hid=hosp.hospital_id,
                tipo_unico=tipo_alerta,
                nivel=nivel,
                mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers,
                titulo_visible=titulo_visible,
            )
