-- Direccion Meteorologica de Chile (DMC): catalogo de estaciones EMA
-- + tablon ancho de datos + diccionario de variables, siguiendo la
-- arquitectura legada del usuario (datos_crudos.ema_dmc/ema_datos).

-- Catalogo de estaciones EMA (estacion meteorologica automatica),
-- poblado por backend/dmc_stations.py desde getEstacionesRedEma:
-- clave subrogada id SERIAL + clave natural cod_estacion UNIQUE (el
-- upsert del catalogo conflictea por cod_estacion). latitud/longitud
-- viven en geometria (ultima columna, por la convencion del
-- proyecto).
CREATE TABLE IF NOT EXISTS frontal_sur.dmc_stations (
    id SERIAL PRIMARY KEY,
    cod_estacion TEXT NOT NULL UNIQUE,
    nombre TEXT,
    altura DOUBLE PRECISION,
    zona TEXT,
    region TEXT,
    geometria geometry(Point, 4326) NOT NULL
);
CREATE INDEX IF NOT EXISTS dmc_stations_geometria_idx ON frontal_sur.dmc_stations USING GIST (geometria);

-- Tablon ancho de datos DMC: una fila por (estacion, momento) con una
-- columna por variable del endpoint getDatosRecientesEma, y FK ema_id
-- hacia dmc_stations.id (la geometria vive SOLO en el catalogo, no se
-- repite por observacion).
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

-- Diccionario de variables del tablon dmc_datos: la unidad es una
-- propiedad de la variable (no de cada observacion), asi que vive
-- aqui una sola vez en lugar de repetirse por fila como en un formato
-- largo. "variable" coincide con el nombre de columna de dmc_datos
-- (join natural) y "campo_endpoint" con el nombre original del campo
-- en getDatosRecientesEma. Unidades verificadas en vivo contra la API
-- el 2026-07-17 (ej: "9.8 grados C", "947.6 hPas.", "1.1 kt").
CREATE TABLE IF NOT EXISTS frontal_sur.dmc_variables (
    id SERIAL PRIMARY KEY,
    variable TEXT NOT NULL UNIQUE,
    unidad TEXT NOT NULL,
    campo_endpoint TEXT NOT NULL
);

INSERT INTO frontal_sur.dmc_variables (variable, unidad, campo_endpoint) VALUES
    ('temperatura', '°C', 'temperatura'),
    ('temperatura_02_mts', '°C', 'temperatura02Mts'),
    ('temperatura_10_mts', '°C', 'temperatura10Mts'),
    ('temperatura_30_mts', '°C', 'temperatura30Mts'),
    ('punto_de_rocio', '°C', 'puntoDeRocio'),
    ('temperatura_minima_12_horas', '°C', 'temperaturaMinima12Horas'),
    ('temperatura_maxima_12_horas', '°C', 'temperaturaMaxima12Horas'),
    ('humedad_relativa', '%', 'humedadRelativa'),
    ('radiacion_global_inst', 'Watt/m2', 'radiacionGlobalInst'),
    ('presion_estacion', 'hPa', 'presionEstacion'),
    ('presion_nivel_del_mar', 'hPa', 'presionNivelDelMar'),
    ('presion_nivel_estandar', 'hPa', 'presionNivelEstandar'),
    ('agua_caida_del_minuto', 'mm', 'aguaCaidaDelMinuto'),
    ('agua_caida_6_horas', 'mm', 'aguaCaida6Horas'),
    ('agua_caida_24_horas', 'mm', 'aguaCaida24Horas'),
    ('direccion_del_viento', '°', 'direccionDelViento'),
    ('fuerza_del_viento', 'kt', 'fuerzaDelViento'),
    ('direccion_del_viento_promedio_2_minutos', '°', 'direccionDelVientoPromedio2Minutos'),
    ('fuerza_del_viento_promedio_2_minutos', 'kt', 'fuerzaDelVientoPromedio2Minutos'),
    ('direccion_del_viento_promedio_10_minutos', '°', 'direccionDelVientoPromedio10Minutos'),
    ('fuerza_del_viento_promedio_10_minutos', 'kt', 'fuerzaDelVientoPromedio10Minutos'),
    ('direccion_del_viento_02_minutos_max', '°', 'direccionDelViento02MinutosMax'),
    ('fuerza_del_viento_02_minutos_max', 'kt', 'fuerzaDelViento02MinutosMax'),
    ('direccion_del_viento_10_minutos_max', '°', 'direccionDelViento10MinutosMax'),
    ('fuerza_del_viento_10_minutos_max', 'kt', 'fuerzaDelViento10MinutosMax')
ON CONFLICT (variable) DO UPDATE SET unidad = EXCLUDED.unidad, campo_endpoint = EXCLUDED.campo_endpoint;