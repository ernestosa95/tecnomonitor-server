"""
Mapeo entre el tipo interno de una alerta (`tipo_unico`, la clave estable con
la que `estado.py` la rastrea) y el protocolo de atención correspondiente en
`docs/runbooks/<slug>.md`. El link resultante apunta al propio dashboard
(`/runbooks/<slug>`, ver routers/runbooks.py) y no al repositorio -- los
colaboradores que reciben la tarea de Asana no necesariamente tienen acceso
al repo, pero sí son usuarios de TecnoMonitor.

Agregar un protocolo nuevo es una línea acá + el .md en docs/runbooks/. Un
tipo de alerta sin entrada simplemente no lleva link todavía (no es
obligatorio tener el protocolo escrito para que la alerta funcione).
"""
import os

PUBLIC_BASE_URL = os.environ.get(
    "PUBLIC_BASE_URL", "https://tecnomonitor.tecnoimagen.com.ar"
).rstrip("/")

# Prefijo de tipo_unico -> slug del runbook.
_RUNBOOKS_POR_PREFIJO = {
    "DICOM_ROUTE_": "dicom-autoenrute",
}


def runbook_url_para(tipo_unico):
    """Devuelve el link al protocolo de atención, o None si todavía no hay uno."""
    if not tipo_unico:
        return None
    for prefijo, slug in _RUNBOOKS_POR_PREFIJO.items():
        if tipo_unico.startswith(prefijo):
            return f"{PUBLIC_BASE_URL}/runbooks/{slug}"
    return None
