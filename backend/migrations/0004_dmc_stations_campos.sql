-- Ajuste del catalogo de estaciones DMC (revision del 2026-07-17):
-- nombres de columna segun el modelo del ETL legado del usuario
-- (cod_estacion, nombre, altura, zona) y campo zonaGeografica que
-- faltaba. latitud/longitud viven en geometria (ultima columna, por
-- la convencion del proyecto). La tabla solo contiene lo que puebla
-- backend/dmc_stations.py, asi que se recrea y se repuebla.

DROP TABLE IF EXISTS frontal_sur.dmc_stations;
CREATE TABLE frontal_sur.dmc_stations (
    cod_estacion TEXT PRIMARY KEY,
    nombre TEXT,
    altura DOUBLE PRECISION,
    zona TEXT,
    region TEXT,
    geometria geometry(Point, 4326) NOT NULL
);
CREATE INDEX dmc_stations_geometria_idx ON frontal_sur.dmc_stations USING GIST (geometria);