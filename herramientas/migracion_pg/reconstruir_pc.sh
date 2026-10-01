#!/usr/bin/env bash
# Reconstruye desde cero la base Postgres de la PC (contenedor tm-pg) con un rango de la foto.
#   herramientas/migracion_pg/reconstruir_pc.sh ../backup-db/monitor_copia.db 2026-09-22 2026-10-02 [2000-01-01]
# El 4.º parámetro (opcional) carga software y KPIs desde antes que la infraestructura (son chicos).
set -euo pipefail
SQLITE=${1:?ruta a la copia SQLite}; DESDE=${2:?desde}; HASTA=${3:?hasta}; SW_DESDE=${4:-$DESDE}
DSN=${DSN:-postgresql://postgres:local@127.0.0.1:5433/tecnomonitor}
AQUI=$(dirname "$0")
docker exec tm-pg psql -U postgres -qc "DROP DATABASE IF EXISTS tecnomonitor WITH (FORCE);" -c "CREATE DATABASE tecnomonitor;"
python3 "$AQUI/instalar_esquema.py" --dsn "$DSN"
python3 "$AQUI/copiar_tablas_chicas.py" "$SQLITE" --dsn "$DSN" | tail -1
python3 "$AQUI/cargar_infra.py" "$SQLITE" --dsn "$DSN" --desde "$DESDE" --hasta "$HASTA" | grep Listo
python3 "$AQUI/cargar_software.py" "$SQLITE" --dsn "$DSN" --desde "$SW_DESDE" --hasta "$HASTA" | grep Listo
for v in host_1h vm_1h sensor_1h cola_dicom_1h mirth_1h portal_1h host_1d vm_1d sensor_1d; do
    docker exec tm-pg psql -U postgres -d tecnomonitor -qc "CALL refresh_continuous_aggregate('$v', NULL, NULL);"
done
echo "Base de la PC reconstruida ($DESDE a $HASTA)."
