-- Migracion inicial del esquema frontal_sur: crea el esquema, la
-- extension PostGIS (si no existe ya en esta base de datos compartida)
-- y las 5 tablas del modelo de datos del spec.

CREATE SCHEMA IF NOT EXISTS frontal_sur;

CREATE EXTENSION IF NOT EXISTS postgis;

-- Registra cada corrida del pipeline de ingesta: cuando empezo, cuando
-- termino, de que fuente, y si fallo (con el mensaje de error).
CREATE TABLE IF NOT EXISTS frontal_sur.ingest_runs (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT
);

-- Un frame raster (imagen/rejilla) para una variable, region y momento
-- especifico. file_path apunta al raster original (ej GeoTIFF) y
-- png_overlay_path a una version PNG ya reproyectada para overlay en
-- el mapa web. La UNIQUE evita duplicar el mismo frame si el pipeline
-- de ingesta se corre mas de una vez sobre el mismo periodo.
CREATE TABLE IF NOT EXISTS frontal_sur.frames_raster (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    variable TEXT NOT NULL,
    region TEXT NOT NULL,
    valid_time TIMESTAMPTZ NOT NULL,
    bbox DOUBLE PRECISION[] NOT NULL,
    file_path TEXT,
    png_overlay_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source, variable, region, valid_time)
);

-- station_id es TEXT: los codigos de estacion de DMC y NOAA/NCEI son
-- alfanumericos (ejemplo: ids GHCN tipo "CIM00085766"), no enteros.
CREATE TABLE IF NOT EXISTS frontal_sur.station_obs (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    station_id TEXT NOT NULL,
    station_name TEXT,
    geom geometry(Point, 4326) NOT NULL,
    valid_time TIMESTAMPTZ NOT NULL,
    variable TEXT NOT NULL,
    value DOUBLE PRECISION,
    unit TEXT,
    UNIQUE (source, variable, station_id, valid_time)
);
CREATE INDEX IF NOT EXISTS station_obs_geom_idx ON frontal_sur.station_obs USING GIST (geom);

-- inundation_polygon es nullable: no todos los gauges de Flood Hub
-- traen mapa de inundacion (floodStatus.inundationMapSet es opcional
-- en la API de Flood Hub).
CREATE TABLE IF NOT EXISTS frontal_sur.flood_status (
    id SERIAL PRIMARY KEY,
    gauge_id TEXT NOT NULL,
    geom_point geometry(Point, 4326) NOT NULL,
    inundation_polygon geometry(MultiPolygon, 4326),
    severity TEXT,
    issued_time TIMESTAMPTZ NOT NULL,
    forecast_trend TEXT,
    forecast_change TEXT,
    source TEXT NOT NULL,
    UNIQUE (gauge_id, issued_time)
);
CREATE INDEX IF NOT EXISTS flood_status_point_idx ON frontal_sur.flood_status USING GIST (geom_point);
CREATE INDEX IF NOT EXISTS flood_status_polygon_idx ON frontal_sur.flood_status USING GIST (inundation_polygon);

-- comuna_id referencia la tabla de comunas SUBDERE ya existente en
-- esta base de datos compartida (dpa_limites.dpa_comuna_subdere, con
-- primary key confirmada sobre comuna_id, 345 filas, columna de
-- geometria(MultiPolygon,4326) NOT NULL desde la correccion aplicada
-- el 2026-07-16).
CREATE TABLE IF NOT EXISTS frontal_sur.choropleth_stats (
    id SERIAL PRIMARY KEY,
    comuna_id INTEGER NOT NULL REFERENCES dpa_limites.dpa_comuna_subdere(comuna_id),
    variable TEXT NOT NULL,
    agg TEXT NOT NULL,
    value DOUBLE PRECISION,
    valid_time TIMESTAMPTZ NOT NULL,
    UNIQUE (comuna_id, variable, agg, valid_time)
);
