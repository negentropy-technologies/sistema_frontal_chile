"""
Test plano (con assert, sin pytest) para extractors/anomaly_raster.py.

Correr con: .venv/bin/python backend/tests/test_anomaly_raster.py

Solo la plomeria pura de grilla (centros de pixel, muestreo puntual,
combinacion de arrays): sin red, sin BD, sin archivos GeoTIFF reales
en disco. El camino con archivos reales lo cubre la corrida real del
orquestador, igual que el resto de los extractores.
"""

import sys
from pathlib import Path

import numpy as np
from rasterio import Affine

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors.anomaly_raster import compute_anomaly_grid, grid_cell_centers, sample_at_point

# Grilla 2x2, pixel de 1x1 grado, esquina superior izquierda en (0, 3):
# fila 0 (y=2.5) cubre lat [2,3), fila 1 (y=1.5) cubre lat [1,2).
TRANSFORM = Affine(1.0, 0.0, 0.0, 0.0, -1.0, 3.0)


def test_grid_cell_centers_orden_fila_mayor_igual_a_flatten_de_rasterio():
    centros = grid_cell_centers(TRANSFORM, (2, 2))
    assert centros == [(0.5, 2.5), (1.5, 2.5), (0.5, 1.5), (1.5, 1.5)]


def test_sample_at_point_ubica_el_pixel_correcto():
    grid = np.array([[1.0, 2.0], [3.0, 4.0]])
    assert sample_at_point(grid, TRANSFORM, 0.6, 2.6) == 1.0
    assert sample_at_point(grid, TRANSFORM, 1.6, 1.6) == 4.0


def test_sample_at_point_fuera_de_grilla_devuelve_none():
    grid = np.array([[1.0, 2.0], [3.0, 4.0]])
    assert sample_at_point(grid, TRANSFORM, -5.0, -5.0) is None


def test_sample_at_point_nodata_devuelve_none():
    grid = np.array([[np.nan, 2.0], [3.0, 4.0]])
    assert sample_at_point(grid, TRANSFORM, 0.6, 2.6) is None


def test_compute_anomaly_grid_resta_la_climatologia_al_campo_ajustado():
    # Fondo CHIRPS prelim uniforme = 10.0 en toda la grilla; una
    # estacion en (0.5, 2.5) con observacion 15.0 (residuo +5); la
    # climatologia del mismo dia-del-anio es uniforme = 6.0.
    background_grid = np.full((2, 2), 10.0)
    climatology_grid = np.full((2, 2), 6.0)
    result = compute_anomaly_grid(
        station_coords=[(0.5, 2.5)],
        station_values=[15.0],
        background_grid=background_grid,
        background_transform=TRANSFORM,
        climatology_grid=climatology_grid,
    )
    # campo ajustado = 10 + 5 (residuo, mismo en toda la grilla porque
    # hay una sola estacion) = 15; anomalia = 15 - 6 = 9.
    assert np.allclose(result, 9.0)


def test_compute_anomaly_grid_descarta_estaciones_fuera_de_grilla():
    background_grid = np.full((2, 2), 10.0)
    climatology_grid = np.full((2, 2), 6.0)
    result = compute_anomaly_grid(
        station_coords=[(0.5, 2.5), (-99.0, -99.0)],
        station_values=[15.0, 999.0],
        background_grid=background_grid,
        background_transform=TRANSFORM,
        climatology_grid=climatology_grid,
    )
    assert np.allclose(result, 9.0)


if __name__ == "__main__":
    test_grid_cell_centers_orden_fila_mayor_igual_a_flatten_de_rasterio()
    test_sample_at_point_ubica_el_pixel_correcto()
    test_sample_at_point_fuera_de_grilla_devuelve_none()
    test_sample_at_point_nodata_devuelve_none()
    test_compute_anomaly_grid_resta_la_climatologia_al_campo_ajustado()
    test_compute_anomaly_grid_descarta_estaciones_fuera_de_grilla()
    print("OK: todos los tests de anomaly_raster pasaron")
