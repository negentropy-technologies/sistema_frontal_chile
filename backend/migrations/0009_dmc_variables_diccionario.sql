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
