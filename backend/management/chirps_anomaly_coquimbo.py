"""
Anomalia DIARIA de precipitacion ACOTADA a la region de Coquimbo:
reusa anomaly_raster.bucket_dias/background_mensual/background_dia/
climatologia_dia/compute_anomaly_grid_windowed y dem.fetch_elevation
(compartidas con ingest.py::_fetch_anomaly, ver ese docstring y el de
anomaly_raster.py para el residuo a resolucion de bloque desagregado a
mapas diarios, y el gradiente altitudinal liviano por DEM).

Diferencia con la fuente chirps_anomaly de ingest.py: acota TODO el
calculo al bbox real de Coquimbo (no REGION_BBOX, el bbox nacional del
proyecto) y usa SOLO estaciones dga/dmc/agromet dentro del poligono
real de Coquimbo (ST_Contains, no bbox) -- CHIRPS de fondo, climatologia
y DEM se leen/recortan por ventana de los datos ya en disco, sin
descargar nada nuevo salvo que falten. No escribe en la BD: es un
script standalone, no una fuente de ingest.py.

Uso:
    .venv/bin/python backend/management/chirps_anomaly_coquimbo.py <start YYYY-MM-DD> <end YYYY-MM-DD> <carpeta salida>
"""

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from db import app_role_config, get_engine
from extractors import anomaly_raster, dem
from extractors._raster import save_png_overlay
from logutil import log

REGION_ID_COQUIMBO = 4

ESTACIONES_COQUIMBO_SQL = """
WITH region AS (
    SELECT ST_Union(geometria) AS geom FROM dpa_limites.dpa_region_subdere WHERE region_id = :region_id
),
dga_diario AS (
    SELECT s.geometria AS geom, d.momento::date AS dia,
           SUM(d.precipitacion_instantanea) AS valor
    FROM frontal_sur.dga_datos d
    JOIN frontal_sur.dga_stations s ON s.id = d.estacion_id, region r
    WHERE d.momento BETWEEN :start AND :end AND d.precipitacion_instantanea IS NOT NULL
      AND ST_Contains(r.geom, s.geometria)
    GROUP BY s.geometria, d.momento::date
),
dmc_diario AS (
    SELECT s.geometria AS geom, d.momento::date AS dia,
           MAX(d.agua_caida_24_horas) AS valor
    FROM frontal_sur.dmc_datos d
    JOIN frontal_sur.dmc_stations s ON s.id = d.ema_id, region r
    WHERE d.momento BETWEEN :start AND :end AND d.agua_caida_24_horas IS NOT NULL
      AND ST_Contains(r.geom, s.geometria)
    GROUP BY s.geometria, d.momento::date
),
agromet_diario AS (
    SELECT s.geometria AS geom, d.momento::date AS dia,
           SUM(d.precipitacion_horaria) AS valor
    FROM frontal_sur.agromet_datos d
    JOIN frontal_sur.agromet_stations s ON s.id = d.ema_id, region r
    WHERE d.momento BETWEEN :start AND :end AND d.precipitacion_horaria IS NOT NULL
      AND ST_Contains(r.geom, s.geometria)
    GROUP BY s.geometria, d.momento::date
)
SELECT dia, ST_X(geom) AS lon, ST_Y(geom) AS lat, valor FROM dga_diario
UNION ALL
SELECT dia, ST_X(geom) AS lon, ST_Y(geom) AS lat, valor FROM dmc_diario
UNION ALL
SELECT dia, ST_X(geom) AS lon, ST_Y(geom) AS lat, valor FROM agromet_diario
ORDER BY dia
"""


def region_bbox(engine, region_id: int) -> tuple[float, float, float, float]:
    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT ST_XMin(g), ST_YMin(g), ST_XMax(g), ST_YMax(g) FROM (
                SELECT ST_Union(geometria) AS g FROM dpa_limites.dpa_region_subdere WHERE region_id = :id
            ) t
        """), {"id": region_id}).fetchone()
    if row is None or row[0] is None:
        raise ValueError(f"region_id {region_id} no encontrada en dpa_limites.dpa_region_subdere")
    return tuple(row)


def run(start: datetime, end: datetime, out_dir: Path) -> None:
    config = app_role_config()
    with get_engine(config) as engine:
        bbox = region_bbox(engine, REGION_ID_COQUIMBO)
        with engine.connect() as conn:
            filas = conn.execute(text(ESTACIONES_COQUIMBO_SQL),
                                  {"start": start, "end": end, "region_id": REGION_ID_COQUIMBO}).fetchall()

    valores_diarios = defaultdict(dict)  # date -> {(lon, lat): [valores diarios]}
    for dia, lon, lat, valor in filas:
        valores_diarios[dia].setdefault((lon, lat), []).append(valor)

    try:
        dem_path = dem.fetch_elevation(bbox)
    except Exception as exc:
        log(f"chirps_anomaly_coquimbo: DEM no disponible ({exc}), se sigue sin ajuste de elevacion")
        dem_path = None

    out_dir.mkdir(parents=True, exist_ok=True)
    for bucket in anomaly_raster.bucket_dias(start, end):
        etiqueta = f"{bucket[0]:%Y-%m-%d} a {bucket[-1]:%Y-%m-%d}"
        estaciones_bucket = defaultdict(list)  # coord -> [total diario, uno por dia del bloque]
        for dia in bucket:
            for coord, vals in valores_diarios.get(dia.date(), {}).items():
                estaciones_bucket[coord].append(sum(vals))
        if not estaciones_bucket:
            log(f"chirps_anomaly_coquimbo {etiqueta}: sin estaciones de Coquimbo con dato, se salta")
            continue

        try:
            background_window_grid, transform, profile, dias_usados = anomaly_raster.background_mensual(bucket, bbox)
        except ValueError as exc:
            log(f"chirps_anomaly_coquimbo {etiqueta}: {exc}")
            continue

        coords = list(estaciones_bucket.keys())
        totales_ventana = [sum(vs) for vs in estaciones_bucket.values()]

        elevation_grid = station_elevations = None
        if dem_path is not None:
            try:
                elevation_grid = anomaly_raster.elevation_grid_for(dem_path, transform, background_window_grid.shape)
                station_elevations = [
                    anomaly_raster.sample_at_point(elevation_grid, transform, lon, lat) or 0.0
                    for lon, lat in coords
                ]
            except Exception as exc:
                log(f"chirps_anomaly_coquimbo {etiqueta}: elevacion no disponible ({exc}), sin ajuste de elevacion")
                elevation_grid = station_elevations = None

        for dia in dias_usados:
            diario = anomaly_raster.background_dia(dia, bbox)
            if diario is None:
                continue
            background_daily_grid, _, _ = diario

            try:
                climatology_daily_grid, _ = anomaly_raster.climatologia_dia(dia.month, dia.day, bbox)
            except ValueError as exc:
                log(f"chirps_anomaly_coquimbo {dia}: sin climatologia -> {exc}")
                continue
            if climatology_daily_grid.shape != background_window_grid.shape:
                log(f"chirps_anomaly_coquimbo {dia}: climatologia {climatology_daily_grid.shape} no calza "
                    f"con CHIRPS prelim {background_window_grid.shape}, se salta")
                continue

            try:
                anomaly_grid = anomaly_raster.compute_anomaly_grid_windowed(
                    coords, totales_ventana, background_window_grid, transform,
                    background_daily_grid, climatology_daily_grid,
                    station_elevations=station_elevations, elevation_grid=elevation_grid,
                )
            except ValueError as exc:
                log(f"chirps_anomaly_coquimbo {dia}: {exc}")
                continue

            tif_path = out_dir / f"{dia:%Y%m%dT000000}.tif"
            png_path = out_dir / f"{dia:%Y%m%dT000000}.png"
            anomaly_raster.write_geotiff(anomaly_grid, profile, tif_path)
            save_png_overlay(tif_path, png_path)
            log(f"chirps_anomaly_coquimbo {dia} (bloque {etiqueta}): {len(coords)} estaciones, "
                f"grilla {anomaly_grid.shape}")


def main() -> None:
    if len(sys.argv) != 4:
        sys.exit("uso: chirps_anomaly_coquimbo.py <start YYYY-MM-DD> <end YYYY-MM-DD> <carpeta salida>")
    start = datetime.strptime(sys.argv[1], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(sys.argv[2], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    run(start, end, Path(sys.argv[3]))


if __name__ == "__main__":
    main()
