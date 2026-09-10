"""
KPI de negocio: cero admisiones de Mamografía en una ventana de días. Usa el
runner compartido -- ver _runner.py.
"""
from ..config import cargar_config
from ._runner import verificar_kpi_inactividad


def verificar(db):
    config = cargar_config(db)
    dias_umbral = config.get('kpi_mamo_threshold_days', 7)

    verificar_kpi_inactividad(
        db,
        tipo_unico="KPI_INACT_MAMO",
        kpi_settings_key="KPI_INACT_MAMO",
        enabled=config.get('kpi_mamo_alert_enabled'),
        umbral=dias_umbral,
        unidad="dias",
        modalidades_target=['MG', 'MAMO'],
        responsable_config_key="kpi_mamo_responsible_email",
        config=config,
        construir_mensaje=lambda modalidades, umbral: (
            f"Sin admisiones de Mamografía (MG) en los últimos {umbral} días."
        ),
    )
