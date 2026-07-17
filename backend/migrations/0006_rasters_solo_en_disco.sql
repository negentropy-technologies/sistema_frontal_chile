-- Decision de arquitectura definitiva (2026-07-17): TODOS los rasters
-- viven en disco (data/frames/) y se sirven con un tiler u overlays
-- PNG; la base de datos solo guarda metadatos (frames_raster) y
-- vectores. Se elimina la tabla postgis_raster de CHIRPS creada en
-- 0005: un solo camino de almacenamiento para todas las fuentes
-- raster (GOES, IMERG, CHIRPS). La extension postgis_raster queda
-- instalada (es compartida y otro esquema podria usarla).

DROP TABLE IF EXISTS frontal_sur.chirps_raster;