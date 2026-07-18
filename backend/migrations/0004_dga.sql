-- Red Hidrometrica Nacional de la DGA (MOP): catalogo de estaciones +
-- tablon de datos instantaneos, mismo patron que DMC y Agromet
-- (catalogo con geometria al final, tablon ancho con FK estacion_id y
-- una columna por parametro). El catalogo viene del MapServer ArcGIS
-- DGA/Red_Hidrometrica de rest-sit.mop.gob.cl (seed
-- backend/seeds/dga_estaciones.csv, 4349 estaciones con coordenadas);
-- los datos, del portal satelital dgasat de snia.mop.gob.cl, que
-- publica las ~1770 estaciones telemetrizadas de todos los tipos
-- (fluviometricas, meteorologicas, pozos, embalses, nivometricas).
-- El mapeo id de parametro dgasat -> columna vive en PARAMETROS de
-- extractors/dga.py.

CREATE TABLE IF NOT EXISTS frontal_sur.dga_stations (
    id SERIAL PRIMARY KEY,
    -- Codigo BNA con digito verificador modulo 11 (ej 08317001-8),
    -- igual que lo exige dgasat; el ArcGIS lo publica sin DV y el
    -- generador del seed lo completa.
    cod_bna TEXT NOT NULL UNIQUE,
    nombre TEXT,
    tipo_estacion TEXT,
    vigencia TEXT,
    region TEXT,
    -- FK a la tabla de comunas SUBDERE de esta base compartida (la
    -- misma que referencia choropleth_stats), en vez del texto libre
    -- de la fuente; nullable y poblada por dga_stations.py con
    -- ST_Contains despues de cada upsert, igual que agromet_stations.
    comuna_id INTEGER REFERENCES dpa_limites.dpa_comuna_subdere(comuna_id),
    cuenca TEXT,
    altitud DOUBLE PRECISION,
    geometria geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS dga_stations_geometria_idx ON frontal_sur.dga_stations USING GIST (geometria);

-- Una columna por parametro instantaneo de dgasat (id dgasat entre
-- parentesis). Ningun tipo de estacion publica todos: fluviometricas
-- traen caudal/nivel, meteorologicas el bloque de tiempo atmosferico,
-- pozos y embalses sus niveles, nivometricas el bloque de nieve.
CREATE TABLE IF NOT EXISTS frontal_sur.dga_datos (
    id SERIAL PRIMARY KEY,
    estacion_id INTEGER NOT NULL REFERENCES frontal_sur.dga_stations(id),
    momento TIMESTAMPTZ NOT NULL,
    nivel_agua DOUBLE PRECISION,                -- m (1)
    temperatura_agua DOUBLE PRECISION,          -- grados C (2)
    precipitacion_acumulada DOUBLE PRECISION,   -- mm (3)
    temperatura_aire DOUBLE PRECISION,          -- grados C (5)
    humedad DOUBLE PRECISION,                   -- % (7)
    radiacion_solar DOUBLE PRECISION,           -- W/m2 (9)
    caudal DOUBLE PRECISION,                    -- m3/s (12)
    precipitacion_instantanea DOUBLE PRECISION, -- mm (13)
    nivel_pozo DOUBLE PRECISION,                -- m (65)
    direccion_del_viento DOUBLE PRECISION,      -- grados (66)
    presion_atmosferica DOUBLE PRECISION,       -- mb = hPa (67)
    altura_nieve DOUBLE PRECISION,              -- cm (72)
    velocidad_del_viento DOUBLE PRECISION,      -- m/s (82)
    nivel_embalse DOUBLE PRECISION,             -- m (93)
    equivalente_agua_nieve DOUBLE PRECISION,    -- mm (111)
    UNIQUE (estacion_id, momento)
);
CREATE INDEX IF NOT EXISTS dga_datos_momento_idx ON frontal_sur.dga_datos (momento);
