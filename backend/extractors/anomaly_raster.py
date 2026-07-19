"""
Plomeria de grilla y raster para el calculo de anomalias: separada de
anomaly.py (matematica pura, IDW de residuos) y de
extractors/chirps_climatology.py (descarga de la climatologia
historica) para que las tres piezas se puedan trabajar como tareas
independientes (ver docs/superpowers/plans/2026-07-19-anomalia-climatica-v1.md).

Combina el fondo CHIRPS prelim del dia (GeoTIFF ya en disco, ver
extractors/chirps.py) con las observaciones de estacion del mismo dia
y la climatologia historica del mismo dia-del-anio, y produce el array
de anomalia listo para escribir a GeoTIFF.
"""

from pathlib import Path

import numpy as np
import rasterio

from anomaly import compute_anomaly


def grid_cell_centers(transform: "rasterio.Affine", shape: tuple[int, int]) -> list[tuple[float, float]]:
    """
    Coordenadas (lon, lat) del centro de cada pixel de una grilla con
    el "transform" y "shape" (height, width) dados, en el mismo orden
    fila-mayor que .ravel() de un array leido con rasterio.
    """
    height, width = shape
    cols = np.arange(width) + 0.5
    rows = np.arange(height) + 0.5
    xs = transform.c + cols * transform.a
    ys = transform.f + rows * transform.e
    grid_lon, grid_lat = np.meshgrid(xs, ys)
    return list(zip(grid_lon.ravel().tolist(), grid_lat.ravel().tolist()))


def sample_at_point(grid: np.ndarray, transform: "rasterio.Affine", lon: float, lat: float) -> float | None:
    """
    Valor del pixel de "grid" que contiene (lon, lat), o None si el
    punto cae fuera de la grilla o el pixel es nodata (NaN).
    """
    col, row = ~transform * (lon, lat)
    row, col = int(row), int(col)
    height, width = grid.shape
    if not (0 <= row < height and 0 <= col < width):
        return None
    value = float(grid[row, col])
    return None if np.isnan(value) else value


def compute_anomaly_grid(
    station_coords: list[tuple[float, float]],
    station_values: list[float],
    background_grid: np.ndarray,
    background_transform: "rasterio.Affine",
    climatology_grid: np.ndarray,
) -> np.ndarray:
    """
    Campo de anomalia = compute_anomaly (ajuste de residuos IDW sobre
    CHIRPS prelim, Ossa-Moreno et al. 2019) menos la climatologia
    historica del mismo dia-del-anio. Estaciones sin pixel de fondo
    valido (fuera de grilla o nodata) se descartan: no aportan residuo
    utilizable. climatology_grid debe compartir shape con
    background_grid (mismo bbox, misma resolucion CHIRPS nativa).

    Lanza ValueError si ninguna estacion cae dentro de la grilla: no
    tiene sentido interpolar con cero puntos.
    """
    background_at_stations = []
    coords_validos = []
    valores_validos = []
    for (lon, lat), valor in zip(station_coords, station_values):
        fondo = sample_at_point(background_grid, background_transform, lon, lat)
        if fondo is None:
            continue
        background_at_stations.append(fondo)
        coords_validos.append((lon, lat))
        valores_validos.append(valor)

    if not coords_validos:
        raise ValueError("ninguna estacion cae dentro de la grilla de fondo CHIRPS")

    grid_coords = grid_cell_centers(background_transform, background_grid.shape)
    corregido = compute_anomaly(
        valores_validos, background_at_stations, background_grid.ravel().tolist(),
        coords_validos, grid_coords,
    )
    corregido_grid = np.array(corregido, dtype=np.float64).reshape(background_grid.shape)
    return corregido_grid - climatology_grid


def write_geotiff(grid: np.ndarray, profile: dict, dest_path: Path) -> None:
    """
    Escribe "grid" como GeoTIFF de una banda float32 en dest_path,
    reusando el profile (crs/transform) de un raster fuente -- el
    GeoTIFF de CHIRPS prelim del mismo dia, con el que esta grilla
    comparte shape/transform.
    """
    profile = dict(profile)
    profile.update(count=1, dtype="float32", nodata=np.nan)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest_path, "w", **profile) as dst:
        dst.write(grid.astype("float32"), 1)
