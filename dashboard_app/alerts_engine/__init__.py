"""
Motor de alertas -- reorganizado en un paquete por dominio (config, estado,
exclusiones, infra, kpis_negocio/, software/, orquestador). Ver
docs/09-plan-refactor-alertas.md para el detalle completo.

Este __init__ re-exporta todo lo que server.py y los routers usan hoy como
`alerts_engine.X`, para que ningún otro archivo del repo se entere de que
esto dejó de ser un único módulo -- mismo contrato externo, cero cambios de
comportamiento.
"""
from .config import cargar_config
from .exclusiones import _match_patron, cargar_exclusiones
from .orquestador import (
    procesar_offline,
    verificar_estado_software,
    verificar_kpis_programados,
)
from .software.dicom_autoenrute import _drena, _serie_de, _ventana
