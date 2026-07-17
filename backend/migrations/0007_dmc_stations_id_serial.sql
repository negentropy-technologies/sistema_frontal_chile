-- dmc_stations con clave subrogada id SERIAL como en el modelo legado
-- del usuario (datos_crudos.ema_dmc: id + cod_estacion), manteniendo
-- cod_estacion como clave natural UNIQUE (el upsert del catalogo
-- sigue conflicteando por cod_estacion) y la geometria al final. Se
-- recrea copiando las filas existentes para no perder la carga.

CREATE TABLE frontal_sur.dmc_stations_nueva (
    id SERIAL PRIMARY KEY,
    cod_estacion TEXT NOT NULL UNIQUE,
    nombre TEXT,
    altura DOUBLE PRECISION,
    zona TEXT,
    region TEXT,
    geometria geometry(Point, 4326) NOT NULL
);

INSERT INTO frontal_sur.dmc_stations_nueva (cod_estacion, nombre, altura, zona, region, geometria)
SELECT cod_estacion, nombre, altura, zona, region, geometria
FROM frontal_sur.dmc_stations
ORDER BY cod_estacion;

DROP TABLE frontal_sur.dmc_stations;
ALTER TABLE frontal_sur.dmc_stations_nueva RENAME TO dmc_stations;
ALTER INDEX frontal_sur.dmc_stations_nueva_pkey RENAME TO dmc_stations_pkey;
ALTER INDEX frontal_sur.dmc_stations_nueva_cod_estacion_key RENAME TO dmc_stations_cod_estacion_key;
ALTER SEQUENCE frontal_sur.dmc_stations_nueva_id_seq RENAME TO dmc_stations_id_seq;
CREATE INDEX dmc_stations_geometria_idx ON frontal_sur.dmc_stations USING GIST (geometria);