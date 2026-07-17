-- Catalogo de estaciones EMA (estacion meteorologica automatica) de la
-- DMC, poblado por backend/dmc_stations.py desde getEstacionesRedEma.
-- Es una tabla de referencia (geometrias + metadatos), separada de las
-- observaciones: station_obs guarda una fila por (estacion, variable,
-- momento) y toma la geometria de aqui al momento de la ingesta.

-- Convencion del proyecto: la columna de geometria se llama
-- "geometria" y va al final de la tabla.
CREATE TABLE IF NOT EXISTS frontal_sur.dmc_stations (
    station_id TEXT PRIMARY KEY,
    station_name TEXT,
    elevation DOUBLE PRECISION,
    region TEXT,
    geometria geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS dmc_stations_geometria_idx ON frontal_sur.dmc_stations USING GIST (geometria);