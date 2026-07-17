-- Convencion de geometrias del proyecto (pedida en revision del
-- 2026-07-17): toda columna de geometria se llama "geometria" (o
-- "geometria_<rol>" cuando una tabla tiene mas de una) y va al final
-- de la tabla, igual que en dpa_limites.dpa_comuna_subdere.

-- station_obs esta vacia en este momento (NOAA NCEI llega con meses
-- de latencia y DMC recien se esta habilitando), asi que se recrea
-- con la columna al final en vez de hacer malabares con ALTER.
DROP TABLE IF EXISTS frontal_sur.station_obs;
CREATE TABLE frontal_sur.station_obs (
    id SERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    station_id TEXT NOT NULL,
    station_name TEXT,
    valid_time TIMESTAMPTZ NOT NULL,
    variable TEXT NOT NULL,
    value DOUBLE PRECISION,
    unit TEXT,
    geometria geometry(Point, 4326) NOT NULL,
    UNIQUE (source, variable, station_id, valid_time)
);
CREATE INDEX station_obs_geometria_idx ON frontal_sur.station_obs USING GIST (geometria);

-- frames_raster ya tiene filas: la geometria del bbox se agrega como
-- columna generada desde el array bbox existente (xmin, ymin, xmax,
-- ymax), asi se calcula sola para las filas actuales y futuras y el
-- pipeline no necesita escribirla.
ALTER TABLE frontal_sur.frames_raster
    ADD COLUMN IF NOT EXISTS geometria geometry(Polygon, 4326)
    GENERATED ALWAYS AS (ST_MakeEnvelope(bbox[1], bbox[2], bbox[3], bbox[4], 4326)) STORED;
CREATE INDEX IF NOT EXISTS frames_raster_geometria_idx ON frontal_sur.frames_raster USING GIST (geometria);

-- flood_status esta vacia y su fuente (Google Flood Hub) sigue
-- diferida por el waitlist: se recrea con las dos geometrias bajo la
-- convencion y al final de la tabla.
DROP TABLE IF EXISTS frontal_sur.flood_status;
CREATE TABLE frontal_sur.flood_status (
    id SERIAL PRIMARY KEY,
    gauge_id TEXT NOT NULL,
    severity TEXT,
    issued_time TIMESTAMPTZ NOT NULL,
    forecast_trend TEXT,
    forecast_change TEXT,
    source TEXT NOT NULL,
    geometria_punto geometry(Point, 4326) NOT NULL,
    geometria_inundacion geometry(MultiPolygon, 4326),
    UNIQUE (gauge_id, issued_time)
);
CREATE INDEX flood_status_geometria_punto_idx ON frontal_sur.flood_status USING GIST (geometria_punto);
CREATE INDEX flood_status_geometria_inundacion_idx ON frontal_sur.flood_status USING GIST (geometria_inundacion);
