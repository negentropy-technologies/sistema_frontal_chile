-- Migracion inicial del esquema frontal_sur: crea el esquema, la
-- extension PostGIS (si no existe ya en esta base de datos compartida)
-- y las tablas transversales del sistema. Las tablas por fuente de
-- estaciones (DMC, Agromet, DGA) viven en sus propias migraciones.
--
-- Convenciones del proyecto:
-- - Toda columna de geometria se llama "geometria" (o
--   "geometria_<rol>" si una tabla tiene mas de una) y va al final de
--   la tabla, igual que en dpa_limites.dpa_comuna_subdere.
-- - TODOS los rasters viven en disco (data/frames/) y se sirven con
--   un tiler u overlays PNG; la base de datos solo guarda metadatos
--   (frames_raster) y vectores.

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
-- de ingesta se corre mas de una vez sobre el mismo periodo. La
-- geometria es una columna generada desde el array bbox (xmin, ymin,
-- xmax, ymax): se calcula sola y el pipeline no necesita escribirla.
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
    geometria geometry(Polygon, 4326)
        GENERATED ALWAYS AS (ST_MakeEnvelope(bbox[1], bbox[2], bbox[3], bbox[4], 4326)) STORED,
    UNIQUE (source, variable, region, valid_time)
);
CREATE INDEX IF NOT EXISTS frames_raster_geometria_idx ON frontal_sur.frames_raster USING GIST (geometria);

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