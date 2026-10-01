"""
Detector de Mirth Connect: canales detenidos/en error sostenidos por 2 ticks
(filtra micro-cortes) y colas con encolado por encima del umbral -- desde el
mapa de integraciones, el umbral de cola ya no es único: depende de la
criticidad curada de cada canal (alta/media/baja), con un umbral por
defecto para canales todavía sin clasificar.

Un canal sin lecturas en las últimas `mirth_alert_gracia_horas` (6 h por
defecto) no se evalúa y su alerta abierta se cierra: es un canal fantasma
(monitoreo de Mirth apagado en el agente, servidor quitado, canal borrado o
desactivado en Mirth). La antigüedad se mide contra el último reporte *del
hospital*, no contra el reloj: un hospital offline no cierra nada (eso lo
cubre la alerta OFFLINE). Ver docs/16 (REQ-03).

`SYSTEM_ERROR` no es un canal: el agente lo manda en lugar de los canales
cuando no pudo hablar con la API de esa instancia de Mirth (caída, reinicio,
login). Al recuperarse no vuelve con OK, simplemente deja de llegar; por eso
se da por resuelto apenas llegan canales reales MÁS NUEVOS de la misma
instancia, sin esperar la gracia de 6 h.

Ver docs/09-plan-refactor-alertas.md y docs/13-contrato-topologia-mirth.md.
"""
from datetime import timedelta


import database
from datos import infra as datos_infra
from datos import software as datos_sw

from .. import modulos
from ..config import _followers_de
from ..estado import _parsear_timestamp, actualizar_estado_alerta


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


CANAL_SYSTEM_ERROR = "SYSTEM_ERROR"


def _instancia_de(cid):
    """'[MIRTH_SE] IN' -> 'MIRTH_SE'; sin prefijo (instancia 'Default' en main.py) -> ''."""
    if cid.startswith("[") and "] " in cid:
        return cid[1:cid.index("] ")]
    return ""


def _es_system_error(cid):
    return cid == CANAL_SYSTEM_ERROR or cid.endswith(f"] {CANAL_SYSTEM_ERROR}")


def _cerrar_fantasmas(db, hosp, vigentes, motivos):
    """
    Cierra las alertas MIRTH_* abiertas del hospital cuyo canal no se
    evaluó este tick (sin lecturas recientes o sin filas). `motivos` trae el
    mensaje por tipo_unico cuando se conoce la última lectura. Se cierran con
    [BAJA] (modulos.cerrar_alertas): si el canal vuelve, su alerta arranca de
    cero en vez de contar como reincidencia (REQ-03, decisión 6).
    """
    abiertas = db.query(database.AlertaModel).filter(
        database.AlertaModel.hospital_id == hosp.hospital_id,
        database.AlertaModel.is_active == 1,
        database.AlertaModel.tipo.like("MIRTH\\_%", escape="\\"),
    ).all()
    n = 0
    for a in abiertas:
        if a.tipo in vigentes:
            continue
        motivo = motivos.get(a.tipo, "Canal sin lecturas de Mirth: monitoreo desactivado o canal quitado")
        print(f"✅ NORMALIZADO (canal sin lecturas): {hosp.hospital_id} -> {a.tipo}")
        n += modulos.cerrar_alertas(db, [a], motivo)
    if n:
        db.commit()
        modulos._avisar_ws()


def verificar_mirth(db, config, hospitales_activos):
    umbrales = _umbrales(config)
    crit_default = config.get("mirth_crit_default", "media")
    warning_alert_enabled = config.get("mirth_queue_warning_alert_enabled", False)
    asana_followers = _followers_de(db, config, 'mirth_responsible_email')
    try:
        gracia = timedelta(hours=max(1, float(config.get("mirth_alert_gracia_horas", 6) or 6)))
    except (TypeError, ValueError):
        gracia = timedelta(hours=6)

    for hosp in hospitales_activos:
        if modulos.baja_para(hosp.hospital_id, modulo="mirth"):
            continue  # módulo dado de baja (REQ-03): sus alertas ya se cerraron en aplicar_bajas()
        mapa_crit, mapa_hum, mapa_component_a_channel = _crit_por_hospital(db, hosp.hospital_id)
        ultimo_reporte = datos_infra.ultimo_timestamp(db, hosp.hospital_id)
        vigentes = set()
        motivos = {}

        # Las 2 últimas lecturas de cada canal (la segunda filtra micro-cortes).
        registros = datos_sw.ultimas_lecturas(db, hosp.hospital_id, datos_sw.MIRTH, n=2)

        historial_canales = {}
        for reg in registros:
            cid = reg.component_id
            if cid not in historial_canales:
                historial_canales[cid] = []
            historial_canales[cid].append(reg)

        # Última lectura de un canal REAL por instancia: si es más nueva que el
        # SYSTEM_ERROR de esa instancia, el agente ya se pudo conectar.
        ultimo_real = {}
        for cid, historia in historial_canales.items():
            if _es_system_error(cid):
                continue
            ts = max((t for t in (_parsear_timestamp(r.timestamp) for r in historia) if t),
                     default=None)
            inst = _instancia_de(cid)
            if ts and (inst not in ultimo_real or ts > ultimo_real[inst]):
                ultimo_real[inst] = ts

        for cid, historia in historial_canales.items():
            # CORRECCIÓN 3: Re-aseguramos en Python que [0] es siempre el último reporte (rn=1)
            historia.sort(key=lambda x: x.rn)

            actual = historia[0]
            tipo_alerta = f"MIRTH_{cid[:35]}"

            # Canal fantasma: su última lectura quedó más de `gracia` detrás
            # del último reporte del hospital. No se evalúa; _cerrar_fantasmas
            # cierra su alerta si quedó abierta.
            ts_canal = _parsear_timestamp(actual.timestamp)
            if ultimo_reporte and ts_canal and ultimo_reporte - ts_canal > gracia:
                horas = (ultimo_reporte - ts_canal).total_seconds() / 3600
                motivos[tipo_alerta] = (
                    f"Canal sin lecturas de Mirth hace {horas:.0f} h (última: "
                    f"{ts_canal:%Y-%m-%d %H:%M}): monitoreo desactivado o canal quitado"
                )
                continue
            if _es_system_error(cid):
                real = ultimo_real.get(_instancia_de(cid))
                if real and ts_canal and real > ts_canal:
                    instancia = _instancia_de(cid) or "Mirth"
                    motivos[tipo_alerta] = (
                        f"{instancia} volvió a responder: canales reales desde "
                        f"{real:%Y-%m-%d %H:%M} (último error de conexión: {ts_canal:%H:%M})"
                    )
                    continue
            vigentes.add(tipo_alerta)

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
            extra = actual.extra_data
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

        _cerrar_fantasmas(db, hosp, vigentes, motivos)
