"""
KPI de negocio: cero admisiones RIS en las modalidades configuradas, dentro
de una ventana de horas. Usa el runner compartido -- ver _runner.py.
"""
from ..config import cargar_config
from ._runner import verificar_kpi_inactividad


def verificar(db):
    config = cargar_config(db)
    umbral_horas = config.get('kpi_rad_threshold_hours', 24)
    modalidades_target = [m.strip().upper() for m in config.get('kpi_rad_modalities', 'DX,CR').split(',')]

    verificar_kpi_inactividad(
        db,
        tipo_unico="KPI_INACT_RAD",
        kpi_settings_key="KPI_INACT_RAD",
        enabled=config.get('kpi_rad_alert_enabled'),
        umbral=umbral_horas,
        unidad="horas",
        modalidades_target=modalidades_target,
        responsable_config_key="kpi_rad_responsible_email",
        config=config,
        construir_mensaje=lambda modalidades, umbral: (
            f"Cero (0) admisiones registradas en las modalidades {', '.join(modalidades)} "
            f"durante las últimas {umbral} horas."
        ),
    )
