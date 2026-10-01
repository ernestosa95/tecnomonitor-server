"""
Capa de acceso a datos (docs/14, Fase 2).

Todas las lecturas del histórico pasan por acá: el resto del código pide
"el último reporte de un hospital" o "la serie de CPU de 30 días" y recibe
objetos de Python ya parseados, sin saber que hoy viven en SQLite como JSON
dentro de `reportes_historicos.full_json_data`. Cuando se migre a Postgres
(inventario + métricas tipadas + agregados, docs/14 §4) solo cambia este
paquete.

Reglas:
  - Fuera de `datos/` no se consulta `reportes_historicos` ni se hace
    `json.loads` de `full_json_data`.
  - Las funciones devuelven fechas como `datetime` (o None) y JSON como
    `dict` (vacío si no hay o no parsea), nunca strings crudos de la base.

Módulos:
  - tiempo: parseo único de timestamps de la base.
  - infra: reportes de infraestructura (último reporte, series de métricas).
  - uso: KPIs de uso del RIS/PACS (por inserción o por fecha del evento).
  - software: lecturas de Mirth, autoenrute, SSL, Elastic, CHECKDB, backups y portal.
"""
