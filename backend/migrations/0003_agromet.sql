-- Red Agroclimatica Nacional (agromet.cl, INIA/Minagri): catalogo de
-- estaciones + tablon de datos horarios, mismo patron que la DMC
-- (catalogo con geometria al final, tablon ancho con FK ema_id y una
-- columna por variable). El mapeo campo del XML -> columna vive en
-- CAMPOS de extractors/agromet.py; las unidades, en el comentario de
-- cada columna de este DDL (no hay tabla de diccionario).

CREATE TABLE IF NOT EXISTS frontal_sur.agromet_stations (
    id SERIAL PRIMARY KEY,
    cod_estacion TEXT NOT NULL UNIQUE,
    institucion TEXT,
    nombre TEXT,
    -- FK a la tabla de comunas SUBDERE de esta base compartida (la
    -- misma que referencia choropleth_stats), en vez del texto libre
    -- de la fuente, para cruzar observaciones con acumulados por
    -- comuna sin joins espaciales. Nullable: hay estaciones costa
    -- afuera o fuera de todo poligono comunal. La puebla
    -- agromet_stations.py con ST_Contains despues de cada upsert.
    comuna_id INTEGER REFERENCES dpa_limites.dpa_comuna_subdere(comuna_id),
    region TEXT,
    geometria geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS agromet_stations_geometria_idx ON frontal_sur.agromet_stations USING GIST (geometria);

CREATE TABLE IF NOT EXISTS frontal_sur.agromet_datos (
    id SERIAL PRIMARY KEY,
    ema_id INTEGER NOT NULL REFERENCES frontal_sur.agromet_stations(id),
    momento TIMESTAMPTZ NOT NULL,
    temperatura DOUBLE PRECISION,
    precipitacion_horaria DOUBLE PRECISION,
    humedad_relativa DOUBLE PRECISION,
    presion_estacion DOUBLE PRECISION,
    radiacion_solar_max DOUBLE PRECISION,
    velocidad_maxima_viento DOUBLE PRECISION,
    temperatura_minima DOUBLE PRECISION,
    temperatura_maxima DOUBLE PRECISION,
    direccion_del_viento DOUBLE PRECISION,
    grados_dia_base10 DOUBLE PRECISION,
    horas_frio_base7 DOUBLE PRECISION,
    UNIQUE (ema_id, momento)
);
CREATE INDEX IF NOT EXISTS agromet_datos_momento_idx ON frontal_sur.agromet_datos (momento);
