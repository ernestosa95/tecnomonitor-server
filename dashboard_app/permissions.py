"""
permissions.py — Fuente única de verdad de permisos de TecnoMonitor.

No importa FastAPI a propósito: son datos + helpers puros, así se puede
reutilizar desde el backend, desde scripts y desde tests sin acoplar nada.

Regla de diseño:
- Agregar un rol nuevo (ej. "Cliente") = agregar UNA entrada en PERMISOS.
- El backend y el front nuevo derivan TODO de acá. No se hardcodea en otro lado.
- La UI vieja NO usa este módulo: queda intacta en producción.
"""


# --------------------------------------------------------------------------
# Vistas / capacidades canónicas de la plataforma
# --------------------------------------------------------------------------
class Vista:
    MONITOR          = "monitor"            # Monitor Global (tabla de toda la red)
    MAPA             = "mapa"               # Mapa nacional
    DETALLE_INFRA    = "detalle_infra"      # Detalle hospital · pestaña Infraestructura
    DETALLE_KPIS     = "detalle_kpis"       # Detalle hospital · pestaña KPIs Uso
    DETALLE_SOFTWARE = "detalle_software"   # Detalle hospital · pestaña Software / Mirth
    INCIDENTES       = "incidentes"         # Panel de incidentes / alertas
    REPORTES         = "reportes"           # Reportes PDF
    CONFIG           = "config"             # Configuración de umbrales
    HOSPITALES       = "hospitales"         # Gestión (ABM) de hospitales
    USUARIOS         = "usuarios"           # Gestión de usuarios
    HERRAMIENTAS     = "herramientas"       # HL7 / sandbox / RIS analytics


TODAS = (
    Vista.MONITOR, Vista.MAPA, Vista.DETALLE_INFRA, Vista.DETALLE_KPIS,
    Vista.DETALLE_SOFTWARE, Vista.INCIDENTES, Vista.REPORTES, Vista.CONFIG,
    Vista.HOSPITALES, Vista.USUARIOS, Vista.HERRAMIENTAS,
)

# Alcance de datos
SCOPE_RED        = "red"          # ve toda la red
SCOPE_HOSPITALES = "hospitales"   # ve solo hospitales asignados (Cliente, fase siguiente)


# --------------------------------------------------------------------------
# Matriz de permisos (la única cosa que se edita para cambiar roles)
# --------------------------------------------------------------------------
PERMISOS = {
    "Admin": {
        "vistas": "*",                       # todas
        "scope": SCOPE_RED,
        "solo_lectura": False,
    },
    "Ingenieria": {
        "vistas": [
            Vista.MONITOR, Vista.MAPA, Vista.DETALLE_INFRA, Vista.DETALLE_KPIS,
            Vista.DETALLE_SOFTWARE, Vista.INCIDENTES, Vista.REPORTES,
            Vista.CONFIG, Vista.HOSPITALES, Vista.HERRAMIENTAS,
        ],
        "scope": SCOPE_RED,
        "solo_lectura": False,
    },
    "Comercial": {
        "vistas": [Vista.DETALLE_KPIS, Vista.REPORTES],   # SIN incidentes (decisión cerrada)
        "scope": SCOPE_RED,
        "solo_lectura": False,
    },
    "Visor": {
        "vistas": [Vista.MONITOR, Vista.MAPA, Vista.DETALLE_INFRA],
        "scope": SCOPE_RED,
        "solo_lectura": True,
    },

    # ----------------------------------------------------------------------
    # Cliente: usuario EXTERNO (responsable de sistemas de un hospital).
    # - scope acotado a los hospitales que el Admin le asigne.
    # - "vistas" es el TECHO de pestañas que podría ver; la habilitación
    #   real por hospital vive en la tabla cliente_hospital_access.
    # - solo lectura: nunca configura ni edita nada.
    # ----------------------------------------------------------------------
    "Cliente": {
        "vistas": [Vista.DETALLE_INFRA, Vista.DETALLE_SOFTWARE, Vista.DETALLE_KPIS],
        "scope": SCOPE_HOSPITALES,
        "solo_lectura": True,
    },
}


# --------------------------------------------------------------------------
# Helpers puros
# --------------------------------------------------------------------------
def _vistas_de(role: str):
    cfg = PERMISOS.get(role)
    if not cfg:
        return []
    return list(TODAS) if cfg["vistas"] == "*" else list(cfg["vistas"])


def puede_ver(role: str, vista: str) -> bool:
    """True si el rol tiene acceso a la vista/capacidad indicada."""
    cfg = PERMISOS.get(role)
    if not cfg:
        return False
    return cfg["vistas"] == "*" or vista in cfg["vistas"]


def roles_con_acceso(vista: str) -> set:
    """Conjunto de roles que pueden acceder a una vista (útil para auditar)."""
    return {r for r in PERMISOS if puede_ver(r, vista)}


def scope_de(role: str) -> str:
    """Alcance de datos del rol ('red' o 'hospitales')."""
    return PERMISOS.get(role, {}).get("scope", SCOPE_RED)


def permisos_de_usuario(role: str) -> dict:
    """Payload que consume el front nuevo vía /api/me/permissions."""
    cfg = PERMISOS.get(role, {})
    return {
        "role": role,
        "vistas": _vistas_de(role),
        "scope": cfg.get("scope", SCOPE_RED),
        "solo_lectura": cfg.get("solo_lectura", False),
    }


# --------------------------------------------------------------------------
# Verificación rápida: `python permissions.py` imprime la matriz resuelta.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== Permisos por rol ===")
    for rol in PERMISOS:
        p = permisos_de_usuario(rol)
        print(f"\n{rol}  (scope={p['scope']}, solo_lectura={p['solo_lectura']})")
        for v in p["vistas"]:
            print(f"   - {v}")
    print("\n=== Roles con acceso a INCIDENTES ===")
    print("  ", sorted(roles_con_acceso(Vista.INCIDENTES)))
    print("=== Roles con acceso a CONFIG ===")
    print("  ", sorted(roles_con_acceso(Vista.CONFIG)))