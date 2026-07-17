-- Tabla postgis_raster para los recortes diarios de CHIRPS v3
-- (pedida en revision del 2026-07-17): ademas del GeoTIFF en disco
-- (que alimenta el overlay PNG del mapa), el raster queda en la BD
-- para poder hacer estadisticas zonales por comuna en SQL puro
-- (ST_Clip + ST_SummaryStats contra dpa_comuna_subdere) sin depender
-- de Earth Engine. Los recortes son livianos (~0.3 MB por dia).

CREATE EXTENSION IF NOT EXISTS postgis_raster;

-- geometria = ST_Envelope(rast): la huella de la cuadricula del
-- raster como poligono 4326 (equivalente al constraint de extension
-- que genera raster2pgsql), poblada por el loader en el mismo INSERT.
-- Permite filtrar por bbox e indexar espacialmente sin tocar el tipo
-- raster, y sigue la convencion del proyecto (columna "geometria" al
-- final).
CREATE TABLE IF NOT EXISTS frontal_sur.chirps_raster (
    id SERIAL PRIMARY KEY,
    valid_time TIMESTAMPTZ NOT NULL UNIQUE,
    rast raster NOT NULL,
    geometria geometry(Polygon, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS chirps_raster_geometria_idx ON frontal_sur.chirps_raster USING GIST (geometria);