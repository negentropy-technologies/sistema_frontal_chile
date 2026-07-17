-- Tablon ancho de datos DMC, siguiendo la arquitectura legada del
-- usuario (datos_crudos.ema_datos): una fila por (estacion, momento)
-- con una columna por variable del endpoint getDatosRecientesEma, y
-- FK ema_id hacia dmc_stations.id (la geometria vive SOLO en el
-- catalogo, no se repite por observacion). station_obs queda para
-- fuentes multi-origen en formato largo (NOAA NCEI).
--
-- Columnas = los 26 campos climaticos verificados en vivo el
-- 2026-07-17 contra la API, en snake_case. La UNIQUE (ema_id,
-- momento) es la clave del upsert: relanzar la ingesta actualiza en
-- vez de duplicar.

CREATE TABLE IF NOT EXISTS frontal_sur.dmc_datos (
    id SERIAL PRIMARY KEY,
    ema_id INTEGER NOT NULL REFERENCES frontal_sur.dmc_stations(id),
    momento TIMESTAMPTZ NOT NULL,
    temperatura DOUBLE PRECISION,
    temperatura_02_mts DOUBLE PRECISION,
    temperatura_10_mts DOUBLE PRECISION,
    temperatura_30_mts DOUBLE PRECISION,
    punto_de_rocio DOUBLE PRECISION,
    temperatura_minima_12_horas DOUBLE PRECISION,
    temperatura_maxima_12_horas DOUBLE PRECISION,
    humedad_relativa DOUBLE PRECISION,
    radiacion_global_inst DOUBLE PRECISION,
    presion_estacion DOUBLE PRECISION,
    presion_nivel_del_mar DOUBLE PRECISION,
    presion_nivel_estandar DOUBLE PRECISION,
    agua_caida_del_minuto DOUBLE PRECISION,
    agua_caida_6_horas DOUBLE PRECISION,
    agua_caida_24_horas DOUBLE PRECISION,
    direccion_del_viento DOUBLE PRECISION,
    fuerza_del_viento DOUBLE PRECISION,
    direccion_del_viento_promedio_2_minutos DOUBLE PRECISION,
    fuerza_del_viento_promedio_2_minutos DOUBLE PRECISION,
    direccion_del_viento_promedio_10_minutos DOUBLE PRECISION,
    fuerza_del_viento_promedio_10_minutos DOUBLE PRECISION,
    direccion_del_viento_02_minutos_max DOUBLE PRECISION,
    fuerza_del_viento_02_minutos_max DOUBLE PRECISION,
    direccion_del_viento_10_minutos_max DOUBLE PRECISION,
    fuerza_del_viento_10_minutos_max DOUBLE PRECISION,
    UNIQUE (ema_id, momento)
);
CREATE INDEX IF NOT EXISTS dmc_datos_momento_idx ON frontal_sur.dmc_datos (momento);