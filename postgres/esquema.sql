-- =============================================================================
-- Esquema PostgreSQL 16 + TimescaleDB de TecnoMonitor (docs/14 §4, Fase 3).
--
-- Borrador en iteración: se prueba en la PC contra la foto de producción antes
-- de pasarlo a migraciones con Alembic. Bloque 1: infraestructura (lo que hoy
-- es reportes_historicos). Software y KPIs de uso vienen después.
--
-- Criterio (medido en la Fase 0, docs/14 §9.2): el 89,5 % de cada reporte es
-- igual al anterior. Lo que cambia seguido va a tablas de métricas angostas y
-- tipadas (hypertables); lo demás es inventario y se guarda solo cuando cambia.
--
-- Horas: timestamptz en UTC. Los agentes mandan hora local sin zona; la carga
-- la interpreta con la zona del hospital (hoy todos America/Argentina/Buenos_Aires).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- -----------------------------------------------------------------------------
-- Inventario: una versión por cambio (vigente desde / hasta).
-- `datos` es el reporte del agente SIN las métricas (lo que quitan las tablas
-- metricas_*), así que inventario + métricas reconstruyen el reporte.
-- -----------------------------------------------------------------------------
CREATE TABLE inventario (
    hospital_id    text        NOT NULL,
    vigente_desde  timestamptz NOT NULL,
    vigente_hasta  timestamptz,                -- NULL = versión actual
    hash           text        NOT NULL,       -- sha256 de `datos` canónico
    datos          jsonb       NOT NULL,
    PRIMARY KEY (hospital_id, vigente_desde)
);
CREATE UNIQUE INDEX inventario_actual ON inventario (hospital_id) WHERE vigente_hasta IS NULL;

-- -----------------------------------------------------------------------------
-- Métricas (una fila por reporte, o por VM / disco / sensor / servicio y reporte)
-- -----------------------------------------------------------------------------
CREATE TABLE metricas_host (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    cpu_pct        real,
    ram_pct        real,
    ram_usada_gb   real,
    potencia_w     real,
    latencia_ms    real,
    subida_mbps    real,
    bajada_mbps    real,
    arranque       timestamptz,                -- ts - uptime: cambia solo al reiniciar
    host_status    text                        -- el que hoy calcula la ingesta (OK/WARNING/...)
);
SELECT create_hypertable('metricas_host', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON metricas_host (hospital_id, ts DESC);

CREATE TABLE metricas_sensor (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    tipo           text        NOT NULL,       -- 'temp' | 'fan' | 'psu'
    nombre         text        NOT NULL,
    valor          real
);
SELECT create_hypertable('metricas_sensor', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON metricas_sensor (hospital_id, ts DESC);

CREATE TABLE metricas_vm (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    vm             text        NOT NULL,
    cpu_pct        real,
    ram_pct        real,
    ram_usada_gb   real,
    arranque       timestamptz,
    estado         text,                       -- state: Online / Offline / ...
    motivo         text,                       -- state_reason
    error          text                        -- wmi_error
);
SELECT create_hypertable('metricas_vm', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON metricas_vm (hospital_id, vm, ts DESC);

CREATE TABLE metricas_disco (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    vm             text        NOT NULL,
    montaje        text        NOT NULL,
    uso_pct        real,
    libre_gb       real,
    latencia_ms    real
);
SELECT create_hypertable('metricas_disco', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON metricas_disco (hospital_id, vm, montaje, ts DESC);

CREATE TABLE metricas_servicio (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    vm             text        NOT NULL,
    servicio       text        NOT NULL,
    cpu_pct        real,
    ram_mb         real,
    hilos          integer,
    handles        integer
);
SELECT create_hypertable('metricas_servicio', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON metricas_servicio (hospital_id, vm, servicio, ts DESC);

-- collection_meta: qué recolectó el agente y con qué resultado (lo usan los
-- módulos dados de baja y el aviso de índice de autoenrute).
CREATE TABLE recoleccion (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    meta           jsonb       NOT NULL
);
SELECT create_hypertable('recoleccion', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON recoleccion (hospital_id, ts DESC);

-- -----------------------------------------------------------------------------
-- Estado actual: una fila por hospital, upsert en cada reporte. Reemplaza el
-- "MAX(timestamp) GROUP BY" sobre el histórico del motor de alertas.
-- -----------------------------------------------------------------------------
CREATE TABLE estado_actual_hospital (
    hospital_id    text        PRIMARY KEY,
    ts             timestamptz NOT NULL,
    host_status    text,
    datos          jsonb       NOT NULL        -- el reporte completo, como llegó
);

-- -----------------------------------------------------------------------------
-- Crudo: el JSON del agente tal cual (auditoría). 30 días en la base; después
-- se archiva comprimido fuera (decisión 3) y se borra el chunk.
-- -----------------------------------------------------------------------------
CREATE TABLE reporte_crudo (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    datos          jsonb       NOT NULL
);
SELECT create_hypertable('reporte_crudo', by_range('ts', INTERVAL '1 day'));
CREATE INDEX ON reporte_crudo (hospital_id, ts DESC);

-- -----------------------------------------------------------------------------
-- Compresión columnar (TimescaleDB): los chunks de más de 7 días se comprimen,
-- sin pérdida y transparente para las consultas.
-- -----------------------------------------------------------------------------
ALTER TABLE metricas_host     SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id',
                                   timescaledb.compress_orderby = 'ts');
ALTER TABLE metricas_sensor   SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, tipo, nombre',
                                   timescaledb.compress_orderby = 'ts');
ALTER TABLE metricas_vm       SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, vm',
                                   timescaledb.compress_orderby = 'ts');
ALTER TABLE metricas_disco    SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, vm, montaje',
                                   timescaledb.compress_orderby = 'ts');
ALTER TABLE metricas_servicio SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, vm, servicio',
                                   timescaledb.compress_orderby = 'ts');
ALTER TABLE recoleccion       SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id',
                                   timescaledb.compress_orderby = 'ts');
ALTER TABLE reporte_crudo     SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id',
                                   timescaledb.compress_orderby = 'ts');

SELECT add_compression_policy(t, INTERVAL '7 days')
FROM unnest(ARRAY['metricas_host', 'metricas_sensor', 'metricas_vm', 'metricas_disco',
                  'metricas_servicio', 'recoleccion', 'reporte_crudo']::regclass[]) AS t;

-- =============================================================================
-- Bloque 2: software (hoy software_monitoring) y KPIs de uso (hoy reportes_uso).
-- `componente` conserva el component_id de hoy (p. ej. "[MIRTH_SE] IN"): es la
-- identidad con la que el motor de alertas arma sus tipo_unico.
-- =============================================================================

-- Mirth: una fila por canal y lectura (~14 mil/día).
CREATE TABLE mirth_canal_metricas (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    componente     text        NOT NULL,       -- "[INSTANCIA] canal" (o "canal" en la instancia Default)
    instancia      text,
    channel_id     text,
    estado         text,                       -- STARTED / STOPPED / ERROR / ...
    encolados      integer,
    recibidos      bigint,
    enviados       bigint,
    errores        bigint,
    ultimo_error   text
);
SELECT create_hypertable('mirth_canal_metricas', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON mirth_canal_metricas (hospital_id, componente, ts DESC);

-- Autoenrute DICOM: pendientes por regla (~12 mil/día) + catálogo de reglas.
CREATE TABLE cola_dicom_metricas (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    regla          text        NOT NULL,
    pendientes     integer
);
SELECT create_hypertable('cola_dicom_metricas', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON cola_dicom_metricas (hospital_id, regla, ts DESC);

CREATE TABLE dicom_reglas (                    -- nodos de cada regla: casi nunca cambian
    hospital_id    text        NOT NULL,
    regla          text        NOT NULL,
    etiqueta       text,
    origen_key     integer,
    origen_nick    text,
    origen_host    text,
    destino_key    integer,
    destino_nick   text,
    destino_host   text,
    visto          timestamptz NOT NULL,       -- última lectura con estos datos
    PRIMARY KEY (hospital_id, regla)
);

-- Portal paciente: un estado por fila, todas las filas de una lectura comparten ts.
CREATE TABLE portal_estado_metricas (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    componente     text        NOT NULL,       -- "MPS:9"
    origen         text,
    codigo         text,
    estado         text,
    total          integer,
    ultimas_24h    integer,
    sin_iso        integer,
    con_iso        integer,
    mas_antiguo    timestamptz,
    fuente         text
);
SELECT create_hypertable('portal_estado_metricas', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON portal_estado_metricas (hospital_id, ts DESC);

-- SSL y logs de Elastic: poco volumen o formato variable, genérica con jsonb.
CREATE TABLE software_eventos (
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    app            text        NOT NULL,       -- 'ssl_certificate' | 'elasticsearch'
    componente     text        NOT NULL,
    estado         text,
    valor          integer,
    extra          jsonb
);
SELECT create_hypertable('software_eventos', by_range('ts', INTERVAL '7 days'));
CREATE INDEX ON software_eventos (hospital_id, app, componente, ts DESC);

-- CHECKDB y backups SQL: pocas filas y la ingesta renueva la última de backups
-- en el lugar (last_seen), así que es tabla común (no hypertable comprimida).
-- El orden de inserción importa (backups): id.
CREATE TABLE sql_eventos (
    id             bigserial   PRIMARY KEY,
    ts             timestamptz NOT NULL,
    hospital_id    text        NOT NULL,
    app            text        NOT NULL,       -- 'sql_integrity' | 'sql_backup'
    base           text        NOT NULL,
    estado         text,
    valor          integer,
    extra          jsonb
);
CREATE INDEX ON sql_eventos (hospital_id, app, base, id DESC);

-- KPIs de uso: un reporte (application_metrics) por hospital y período, con sus
-- ítems de RIS, PACS y usuarios. `desde` es la fecha del evento
-- (start_time_extraction), que es la que usa todo lo que agrupa por fecha.
CREATE TABLE kpi_reporte (
    id             bigserial   PRIMARY KEY,
    hospital_id    text        NOT NULL,
    insertado      timestamptz NOT NULL,
    desde          timestamptz,
    hasta          timestamptz,
    intervalo_horas real
);
CREATE INDEX ON kpi_reporte (hospital_id, desde);
CREATE INDEX ON kpi_reporte (hospital_id, insertado);

CREATE TABLE kpi_ris (
    reporte_id     bigint      NOT NULL REFERENCES kpi_reporte (id) ON DELETE CASCADE,
    orden          smallint    NOT NULL,       -- posición en la lista del agente
    equipo         text,
    aet            text,
    modalidad      text,
    totales        integer,
    citados        integer,
    admitidos      integer,
    ejecutados     integer,
    con_imagen     integer,
    borradores     integer,
    definitivos    integer,
    suspendidos    integer,
    PRIMARY KEY (reporte_id, orden)
);

CREATE TABLE kpi_pacs (
    reporte_id     bigint      NOT NULL REFERENCES kpi_reporte (id) ON DELETE CASCADE,
    orden          smallint    NOT NULL,
    aet            text,
    modalidad      text,
    almacenados    integer,
    PRIMARY KEY (reporte_id, orden)
);

CREATE TABLE kpi_usuarios (
    reporte_id     bigint      NOT NULL REFERENCES kpi_reporte (id) ON DELETE CASCADE,
    orden          smallint    NOT NULL,
    rol            text,
    usuarios_unicos integer,
    inicios_sesion integer,
    PRIMARY KEY (reporte_id, orden)
);

ALTER TABLE mirth_canal_metricas   SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, componente',
                                        timescaledb.compress_orderby = 'ts');
ALTER TABLE cola_dicom_metricas    SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, regla',
                                        timescaledb.compress_orderby = 'ts');
ALTER TABLE portal_estado_metricas SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, componente',
                                        timescaledb.compress_orderby = 'ts');
ALTER TABLE software_eventos       SET (timescaledb.compress, timescaledb.compress_segmentby = 'hospital_id, app, componente',
                                        timescaledb.compress_orderby = 'ts');
SELECT add_compression_policy(t, INTERVAL '7 days')
FROM unnest(ARRAY['mirth_canal_metricas', 'cola_dicom_metricas', 'portal_estado_metricas',
                  'software_eventos']::regclass[]) AS t;
