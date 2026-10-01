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

Piso habitual por regla: las reglas con un residuo constante evalúan solo el
exceso sobre su piso (ver dicom_baseline.py y docs/12 §3quater).

Reglas que dejan de reportarse: Logstash indexa por IDRULE con upsert y nunca
borra, así que una regla eliminada en el PACS queda en el índice con su último
valor; el agente la descarta por antigüedad y deja de mandarla. Sin filas no
hay evaluación ni OK, y su alerta quedaba abierta para siempre. Igual que los
canales fantasma de Mirth, se cierra (con [BAJA]) cuando el hospital sigue
reportando y la regla no aparece hace más de la ventana crítica.

Índice desactualizado: si TODO el índice está vencido (`collection_meta.
dicom_routing.status == "stale"` sin reglas vigentes), desde el server no se
distingue "pipeline de Logstash caído" de "se borró la única regla". Las colas
se cierran igual (arriba) y queda una alerta liviana por hospital
(DICOM_INDICE_DESACTUALIZADO) para que un pipeline caído no pase inadvertido.

Nota de dependencia externa: `evaluar_cola`, `_serie_de` y `_ventana` también
los usa dashboard_app/routers/hospital_detalle.py para mostrar el mismo estado
de salud en el panel de detalle del hospital (mismo criterio, no una copia
local). Se re-exportan desde alerts_engine/__init__.py por eso.

Ver docs/09-plan-refactor-alertas.md.
"""
import re
from datetime import datetime, timedelta


import database
from datos import infra as datos_infra
from datos import software as datos_sw

from .. import modulos
from ..config import _followers_de
from ..estado import _parsear_timestamp, actualizar_estado_alerta
from .dicom_baseline import cargar_baselines, refrescar_baselines

PREFIJO_TIPO = "DICOM_ROUTE_"
TIPO_INDICE = "DICOM_INDICE_DESACTUALIZADO"


def _sanear_nodo(valor):
    """Deja un nombre de nodo apto para un identificador (MAYUS-CON-GUIONES)."""
    if not valor:
        return None
    limpio = re.sub(r"[^A-Za-z0-9]+", "-", str(valor)).strip("-").upper()
    return limpio or None


def _identificador_nodos(extra):
    """
    Arma NODOORIGEN_TO_NODODESTINO a partir de los nombres de nodo que manda
    el agente (from_nickname/to_nickname, con fallback a hostname -- ver
    main.py, sección de ingesta de dicom_routing_queues). Devuelve None si no
    hay datos suficientes para armarlo (agente viejo, o nodo sin nombre).
    """
    if not isinstance(extra, dict):
        return None
    origen = _sanear_nodo(extra.get("from_nickname") or extra.get("from_hostname"))
    # Regla sin nodo de origen (FROMNODE nulo en el PACS) = toma todos los
    # equipos que envían al PACS de origen. No es un error: se nombra "TODOS"
    # en vez de dejar el título del ticket en el ID numérico.
    if not origen and "from_key" in extra and extra.get("from_key") is None:
        origen = "TODOS"
    destino = _sanear_nodo(extra.get("to_nickname") or extra.get("to_hostname"))
    if origen and destino:
        return f"{origen}_TO_{destino}"
    return None


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


def _crecimiento_permitido_critica(valores, min_inst):
    """
    En la ventana crítica, el crecimiento solo cuenta como "ingreso de un
    estudio masivo" si la cola TOCÓ el piso de ruido dentro de esa ventana
    (o sea, venía vacía y recién se llenó). Una cola que estuvo toda la
    ventana por encima del piso sin bajar de ahí no está recibiendo un
    estudio: está trabada y acumulando.
    """
    return bool(valores) and min(valores) < max(min_inst, 1)


def _caida_maxima(valores):
    """Mayor caída (absoluta, relativa) desde un pico a un valor POSTERIOR dentro de la serie."""
    pico = valores[0]
    caida_abs = 0
    caida_rel = 0.0
    for x in valores:
        if x > pico:
            pico = x
        caida_abs = max(caida_abs, pico - x)
        if pico > 0:
            caida_rel = max(caida_rel, (pico - x) / pico)
    return caida_abs, caida_rel


def _drena(valores, drain_percent, permitir_crecimiento=True):
    """
    Verifica si la cola tiene actividad saludable (no está estancada).
    Está viva si DRENÓ en algún momento de la ventana (mayor caída pico ->
    valor posterior >= 300 instancias o >= (100 - drain_percent)% del pico),
    O -- solo cuando `permitir_crecimiento` -- si está en pleno ingreso de un
    estudio masivo (creció >= 300 desde el piso de la ventana).

    La caída se mide dentro de toda la ventana y no contra el valor actual:
    comparar contra el pico "ahora" declaraba trabada a una cola sana justo
    cuando llega una tanda nueva (el actual ES el pico), y reabría el ticket
    en cada flanco de subida del serrucho.

    `permitir_crecimiento` es siempre True para la ventana corta (warn). Para
    la ventana crítica lo decide `_crecimiento_permitido_critica`: sin esa
    restricción el crecimiento volvía "sana" a cualquier cola que subiera
    más de 300 instancias, y una regla que acumuló 1,4 M de pendientes en 4
    días nunca alertó.
    """
    if len(valores) < 2:
        return True  # Sin evidencia suficiente, no acusamos

    if max(valores) <= 0:
        return True

    caida_abs, caida_rel = _caida_maxima(valores)

    # Drenó al menos 300 imágenes en algún momento: está despachando.
    if caida_abs >= 300:
        return True

    # Drenó el % esperado de su pico (colas chicas, donde 300 sería mucho).
    if caida_rel >= (100 - drain_percent) / 100.0:
        return True

    # Crecimiento activo: recibe un estudio masivo, el buffer haciendo su trabajo.
    if permitir_crecimiento and (valores[-1] - min(valores)) >= 300:
        return True

    # Ni bajó ni subió significativamente: línea plana o crecimiento sin drenaje.
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


def evaluar_cola(valores, timestamps, ahora, win_warn, win_crit, min_inst, drain_pct, baseline=None):
    """
    Clasifica una cola. Lo usan el detector y el panel de detalle del hospital
    (mismo criterio, una sola copia). Devuelve un dict con:
      nivel:  "OK" | "WARNING" | "CRITICAL" | None (sin historia suficiente)
      motivo: "bajo_piso_ruido" | "piso_habitual" | "sin_historia" | "drenaje"
      v_crit, v_warn: ventanas evaluadas (solo cuando motivo == "drenaje")

    `baseline` es el piso habitual de la regla ({piso, tolerancia, activa}, ver
    dicom_baseline.py). Si está activo y la cola no lo supera por más que la
    tolerancia, la regla está en su piso habitual: OK. Si lo supera, el permiso
    de crecimiento de la ventana crítica se mide contra ese piso propio (la
    cola "toca el piso" cuando vuelve a su residuo habitual, no solo si baja
    de `min_inst`).
    """
    actual = valores[-1]
    if actual <= 0 or actual < min_inst:
        return {"nivel": "OK", "motivo": "bajo_piso_ruido"}

    piso_crecimiento = min_inst
    if baseline and baseline.get("activa"):
        if actual - baseline["piso"] < baseline["tolerancia"]:
            return {"nivel": "OK", "motivo": "piso_habitual"}
        piso_crecimiento = max(min_inst, baseline["piso"] + baseline["tolerancia"])

    v_crit, cubre_crit = _ventana(valores, timestamps, win_crit, ahora)
    v_warn, cubre_warn = _ventana(valores, timestamps, win_warn, ahora)
    if not cubre_warn:
        return {"nivel": None, "motivo": "sin_historia"}

    drena_warn = _drena(v_warn, drain_pct)
    drena_crit = _drena(
        v_crit, drain_pct,
        permitir_crecimiento=_crecimiento_permitido_critica(v_crit, piso_crecimiento),
    ) if cubre_crit else True

    nivel = "CRITICAL" if not drena_crit else "WARNING" if not drena_warn else "OK"
    return {"nivel": nivel, "motivo": "drenaje", "v_crit": v_crit, "v_warn": v_warn}


def _metas_recientes(db, hid, desde):
    """
    (último reporte del hospital, [collection_meta.dicom_routing de los reportes
    desde `desde`], cronológico; None donde el reporte no lo trae).
    """
    metas = datos_infra.valores_recientes(db, hid, "$.collection_meta.dicom_routing", desde)
    return (datos_infra.ultimo_timestamp(db, hid),
            [m if isinstance(m, dict) else None for m in metas])


def _indice_vencido(meta):
    """El agente leyó el índice y TODAS sus reglas estaban vencidas."""
    return (isinstance(meta, dict) and meta.get("enabled") is True
            and meta.get("status") == "stale" and not meta.get("total"))


def evaluar_indice(metas):
    """
    Nivel de DICOM_INDICE_DESACTUALIZADO a partir de los collection_meta de la
    ventana corta. WARNING solo si todos los reportes (al menos 3) vieron el
    índice entero vencido; OK si el último no lo vio; None si no alcanza para
    decidir (pocos reportes, agente viejo sin collection_meta).
    """
    conocidas = [m for m in metas if m is not None]
    if not conocidas:
        return None
    if not _indice_vencido(conocidas[-1]):
        return "OK"
    if len(conocidas) >= 3 and all(_indice_vencido(m) for m in conocidas):
        return "WARNING"
    return None


def _cerrar_fantasmas(db, hosp, vigentes, motivos):
    """
    Cierra las alertas DICOM_ROUTE_* abiertas del hospital cuya regla no se
    evaluó este tick por haber dejado de reportarse. Con [BAJA]
    (modulos.cerrar_alertas), como Mirth: si la regla vuelve, arranca de cero.
    """
    abiertas = db.query(database.AlertaModel).filter(
        database.AlertaModel.hospital_id == hosp.hospital_id,
        database.AlertaModel.is_active == 1,
        database.AlertaModel.tipo.like("DICOM\\_ROUTE\\_%", escape="\\"),
    ).all()
    n = 0
    for a in abiertas:
        if a.tipo in vigentes:
            continue
        motivo = motivos.get(
            a.tipo, "Regla de autoenrute sin lecturas: eliminada o desactivada en el PACS")
        print(f"✅ NORMALIZADO (regla sin lecturas): {hosp.hospital_id} -> {a.tipo}")
        n += modulos.cerrar_alertas(db, [a], motivo)
    if n:
        db.commit()
        modulos._avisar_ws()


def verificar_autoenrute_dicom(db, config, hospitales_activos):
    win_warn = int(config.get('dicom_stall_warning_minutes', 30) or 30)
    win_crit = int(config.get('dicom_stall_critical_minutes', 120) or 120)
    min_inst = int(config.get('dicom_min_instances', 50) or 0)
    drain_pct = int(config.get('dicom_drain_percent', 70) or 70)

    asana_followers = _followers_de(db, config, 'dicom_responsible_email')
    ahora = datetime.now()

    usar_baseline = bool(config.get('dicom_baseline_enabled', True))
    if usar_baseline:
        try:
            refrescar_baselines(db, hospitales_activos)
        except Exception as e:
            db.rollback()
            print(f"⚠️ [DICOM baseline] Falló el refresco de pisos: {repr(e)}")

    # Traemos con margen: la ventana crítica más un 50% para tolerar huecos.
    desde = ahora - timedelta(minutes=win_crit * 1.5)

    for hosp in hospitales_activos:
        if modulos.baja_para(hosp.hospital_id, modulo="dicom_routing"):
            continue  # módulo dado de baja (REQ-03): sus alertas ya se cerraron en aplicar_bajas()

        ultimo_reporte, metas = _metas_recientes(
            db, hosp.hospital_id, ahora - timedelta(minutes=win_warn))
        # Solo se cierran reglas fantasma si el hospital reporta (si está
        # OFFLINE la falta de filas no dice nada) y el agente pudo leer el
        # índice: con status "error" (Elastic inaccesible) faltan TODAS las
        # reglas por la falla, no porque las hayan borrado.
        ultima_meta = next((m for m in reversed(metas) if m is not None), None)
        hospital_reporta = (
            bool(ultimo_reporte)
            and ahora - ultimo_reporte <= timedelta(minutes=win_warn)
            and not (ultima_meta and ultima_meta.get("status") == "error")
        )

        nivel_indice = evaluar_indice(metas)
        if nivel_indice:
            actualizar_estado_alerta(
                db=db, hid=hosp.hospital_id, tipo_unico=TIPO_INDICE, nivel=nivel_indice,
                mensaje=(
                    "El índice de autoenrute (Elastic) no tiene ninguna regla con datos "
                    "recientes: el pipeline de Logstash no está actualizando, o se "
                    "eliminaron las reglas y quedaron sus documentos viejos en el "
                    "índice (borrarlos de Elastic). Mientras tanto no se evalúan las colas."
                    if nivel_indice == "WARNING" else
                    "Índice de autoenrute actualizado."
                ),
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers,
                titulo_visible="DICOM_INDICE_DESACTUALIZADO",
            )

        vigentes = set()
        motivos = {}

        registros = datos_sw.lecturas(db, hosp.hospital_id, datos_sw.DICOM_ROUTING, desde,
                                      por_componente=True)

        if not registros:
            if hospital_reporta:
                _cerrar_fantasmas(db, hosp, vigentes, motivos)
            continue

        baselines = cargar_baselines(db, hosp.hospital_id) if usar_baseline else {}

        por_regla = {}
        for reg in registros:
            por_regla.setdefault(reg.component_id, []).append(reg)

        for id_rule, historia in por_regla.items():
            valores, timestamps = _serie_de(historia)
            if not valores:
                continue

            actual = valores[-1]
            tipo_alerta = f"{PREFIJO_TIPO}{str(id_rule)[:30]}"

            # Regla fantasma: su última lectura quedó más de la ventana crítica
            # detrás del último reporte del hospital (la borraron o la
            # desactivaron en el PACS). No se evalúa; _cerrar_fantasmas cierra
            # su alerta si quedó abierta.
            if ultimo_reporte and ultimo_reporte - timestamps[-1] > timedelta(minutes=win_crit):
                motivos[tipo_alerta] = (
                    f"Regla de autoenrute sin lecturas desde {timestamps[-1]:%Y-%m-%d %H:%M} "
                    f"mientras el hospital sigue reportando: eliminada o desactivada en el PACS"
                )
                continue
            vigentes.add(tipo_alerta)

            # Etiquetas legibles. El extra_data lo escribe main.py:
            # "label" es para leer en el mensaje ("NODO-A → NODO-B"), los
            # nicknames/hostnames sueltos arman el identificador del título.
            ruta = f"regla #{id_rule}"
            nodos = None
            try:
                extra = historia[-1].extra_data
                if isinstance(extra, dict) and extra.get("label"):
                    ruta = extra["label"]
                nodos = _identificador_nodos(extra)
            except Exception:
                pass

            # Título del ticket: mismo formato de siempre (DICOM_ROUTE_<algo>),
            # pero con los nodos que conecta la ruta en vez del ID numérico
            # cuando el agente los informó.
            titulo_visible = f"DICOM_ROUTE_{nodos}" if nodos else tipo_alerta

            bl = baselines.get(str(id_rule))
            res = evaluar_cola(valores, timestamps, ahora, win_warn, win_crit,
                               min_inst, drain_pct, bl)
            nivel = res["nivel"]
            motivo = res["motivo"]

            # Sin historia suficiente para juzgar: no emitimos nada (ni alerta,
            # que sería falso positivo, ni OK, que cerraría un incidente real
            # por falta de datos). Pasa con agentes recién instalados o tras un
            # corte de reporte.
            if nivel is None:
                continue

            nota_piso = (f" | Piso habitual de la regla: {bl['piso']}"
                         if bl and bl.get("activa") else "")

            if motivo == "bajo_piso_ruido":
                # OK explícito para que se cierre el incidente y el ticket.
                mensaje = (
                    f"Cola de autoenrute {ruta} normalizada "
                    f"({actual} instancias pendientes, piso configurado: {min_inst})."
                )
            elif motivo == "piso_habitual":
                mensaje = (
                    f"Cola de autoenrute {ruta} en su piso habitual "
                    f"({actual} instancias pendientes, piso de la regla: {bl['piso']})."
                )
            else:
                v_crit, v_warn = res["v_crit"], res["v_warn"]
                if nivel == "CRITICAL":
                    tendencia = "creciendo" if actual >= v_crit[0] else "sin drenar"
                    mensaje = (
                        f"Cola de autoenrute {ruta} {tendencia} hace más de {win_crit} min "
                        f"sin descender. Revisar el nodo destino.\n"
                        f"Pendientes actuales: {actual} | Pico ventana: {max(v_crit)} | "
                        f"Mínimo ventana: {min(v_crit)} | "
                        f"Umbral de drenaje configurado: {drain_pct}%.{nota_piso}"
                    )
                elif nivel == "WARNING":
                    tendencia = "creciendo" if actual >= v_warn[0] else "estancada"
                    mensaje = (
                        f"Cola de autoenrute {ruta} {tendencia} hace {win_warn} min.\n"
                        f"Pendientes actuales: {actual} | Pico ventana: {max(v_warn)} | "
                        f"Mínimo ventana: {min(v_warn)} | "
                        f"Umbral de drenaje configurado: {drain_pct}%.{nota_piso}"
                    )
                else:
                    mensaje = (
                        f"Cola de autoenrute {ruta} drenando con normalidad "
                        f"({actual} instancias pendientes, umbral de drenaje: {drain_pct}%)."
                    )

            actualizar_estado_alerta(
                db=db, hid=hosp.hospital_id, tipo_unico=tipo_alerta,
                nivel=nivel, mensaje=mensaje,
                asana_proj_id=hosp.asana_project_id,
                asana_followers=asana_followers,
                titulo_visible=titulo_visible,
            )

        if hospital_reporta:
            _cerrar_fantasmas(db, hosp, vigentes, motivos)
