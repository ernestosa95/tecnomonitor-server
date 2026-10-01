"""Parseo único de los timestamps que devuelve la base (datetime o string)."""
from datetime import datetime


def parsear_ts(ts_val):
    """Convierte un timestamp de la DB a datetime. Devuelve None SOLO si es irrecuperable."""
    if isinstance(ts_val, datetime):
        return ts_val
    if not ts_val:
        return None
    s = str(ts_val).strip()
    if s.endswith("Z"):          # sufijo UTC que fromisoformat no traga en 3.10
        s = s[:-1]
    try:
        return datetime.fromisoformat(s)          # cubre 'T' y espacio, con/sin microsegundos
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    try:                          # último recurso: recortar zona/microsegundos sobrantes
        return datetime.strptime(s.replace("T", " ").split(".")[0], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
