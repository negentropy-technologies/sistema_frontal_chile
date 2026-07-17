-- Retiro de NOAA NCEI del sistema (decision del 2026-07-17): GHCND
-- publica las estaciones chilenas con ~1 anio de retraso, inutil para
-- una aplicacion de monitoreo near real time; el contexto de
-- estaciones lo cubre la DMC (dmc_datos, latencia de minutos).
-- station_obs solo existia para esa fuente, asi que se elimina. Si un
-- producto futuro necesita climatologia historica, se retomara con un
-- plan y una migracion propios.

DROP TABLE IF EXISTS frontal_sur.station_obs;