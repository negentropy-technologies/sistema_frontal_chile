"""
Test plano (con assert, sin pytest) para extractors/anomaly_raster.py.

Correr con: .venv/bin/python backend/tests/test_anomaly_raster.py

Solo la plomeria pura de grilla (centros de pixel, muestreo puntual,
combinacion de arrays): sin red, sin BD, sin archivos GeoTIFF reales
en disco. El camino con archivos reales lo cubre la corrida real del
orquestador, igual que el resto de los extractores.
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from rasterio import Affine
from rasterio.io import MemoryFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors.anomaly_raster import (
    bucket_dias,
    compute_anomaly_grid_windowed,
    elevation_grid_for,
    grid_cell_centers,
    sample_at_point,
)

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


def test_compute_anomaly_grid_windowed_resta_climatologia_al_campo_corregido():
    # Fondo de bloque (ventana) uniforme = 10.0; el dia que se pide
    # desagregar es el mismo valor (razon 1, sin desagregar); una
    # estacion en (0.5, 2.5) acumulo 15.0 en el bloque (residuo +5);
    # climatologia del dia exacto uniforme = 6.0.
    background_window_grid = np.full((2, 2), 10.0)
    background_daily_grid = np.full((2, 2), 10.0)
    climatology_daily_grid = np.full((2, 2), 6.0)
    result = compute_anomaly_grid_windowed(
        station_coords=[(0.5, 2.5)],
        station_window_totals=[15.0],
        background_window_grid=background_window_grid,
        background_transform=TRANSFORM,
        background_daily_grid=background_daily_grid,
        climatology_daily_grid=climatology_daily_grid,
    )
    # campo corregido = 10 + 5 (residuo, mismo en toda la grilla porque
    # hay una sola estacion) = 15; anomalia = 15 - 6 = 9.
    assert np.allclose(result, 9.0)


def test_compute_anomaly_grid_windowed_descarta_estaciones_fuera_de_grilla():
    background_window_grid = np.full((2, 2), 10.0)
    background_daily_grid = np.full((2, 2), 10.0)
    climatology_daily_grid = np.full((2, 2), 6.0)
    result = compute_anomaly_grid_windowed(
        station_coords=[(0.5, 2.5), (-99.0, -99.0)],
        station_window_totals=[15.0, 999.0],
        background_window_grid=background_window_grid,
        background_transform=TRANSFORM,
        background_daily_grid=background_daily_grid,
        climatology_daily_grid=climatology_daily_grid,
    )
    assert np.allclose(result, 9.0)


def test_compute_anomaly_grid_windowed_umbral_seco_fuerza_a_cero_pixeles_sin_lluvia():
    # Fondo de bloque 0 en toda la grilla (CHIRPS no vio lluvia en todo
    # el bloque): sin forma diaria de la cual desagregar,
    # compute_anomaly_windowed fuerza el campo corregido a 0 -- la
    # anomalia debe salir como "0 menos climatologia", no NaN ni un
    # valor espurio del IDW.
    background_window_grid = np.zeros((2, 2))
    background_daily_grid = np.zeros((2, 2))
    climatology_daily_grid = np.full((2, 2), 6.0)
    result = compute_anomaly_grid_windowed(
        station_coords=[(0.5, 2.5)],
        station_window_totals=[15.0],
        background_window_grid=background_window_grid,
        background_transform=TRANSFORM,
        background_daily_grid=background_daily_grid,
        climatology_daily_grid=climatology_daily_grid,
    )
    assert np.allclose(result, -6.0)


def test_compute_anomaly_grid_windowed_valida_shape_de_elevation_grid():
    background_window_grid = np.full((2, 2), 10.0)
    background_daily_grid = np.full((2, 2), 10.0)
    climatology_daily_grid = np.full((2, 2), 6.0)
    try:
        compute_anomaly_grid_windowed(
            station_coords=[(0.5, 2.5)],
            station_window_totals=[15.0],
            background_window_grid=background_window_grid,
            background_transform=TRANSFORM,
            background_daily_grid=background_daily_grid,
            climatology_daily_grid=climatology_daily_grid,
            station_elevations=[100.0],
            elevation_grid=np.full((3, 3), 100.0),
        )
        assert False, "debio lanzar ValueError"
    except ValueError as exc:
        assert "elevation_grid" in str(exc)


def test_elevation_grid_for_remuestrea_a_la_grilla_destino():
    # DEM sintetico 4x4 a 0.5 grados/pixel, elevacion 0 en la mitad
    # norte y 10 en la mitad sur, remuestreado (promedio) a una grilla
    # destino de 2x2 a 1 grado/pixel: cada celda destino debe salir en
    # el promedio de las 2x2 celdas DEM que cubre.
    dem_transform = Affine(0.5, 0.0, 0.0, 0.0, -0.5, 2.0)
    dem = np.array([
        [0.0, 0.0, 0.0, 0.0],
        [0.0, 0.0, 0.0, 0.0],
        [10.0, 10.0, 10.0, 10.0],
        [10.0, 10.0, 10.0, 10.0],
    ])
    profile = {
        "driver": "GTiff", "height": 4, "width": 4, "count": 1,
        "dtype": "float64", "crs": "EPSG:4326", "transform": dem_transform,
    }
    with MemoryFile() as memfile:
        with memfile.open(**profile) as dst:
            dst.write(dem, 1)
        target_transform = Affine(1.0, 0.0, 0.0, 0.0, -1.0, 2.0)
        resultado = elevation_grid_for(memfile.name, target_transform, (2, 2))
    assert np.allclose(resultado, [[0.0, 0.0], [10.0, 10.0]])


def test_bucket_dias_no_corta_un_evento_de_menos_de_un_mes_en_el_limite_calendario():
    # 19 de junio a 15 de julio: 27 dias, cruza el limite de mes pero
    # cabe en un solo bloque de max_dias=31 -- no debe partirse en
    # "junio" + "julio".
    start = datetime(2026, 6, 19, tzinfo=timezone.utc)
    end = datetime(2026, 7, 15, tzinfo=timezone.utc)
    bloques = bucket_dias(start, end)
    assert len(bloques) == 1
    assert len(bloques[0]) == 27
    assert bloques[0][0] == start
    assert bloques[0][-1] == end


def test_bucket_dias_parte_una_ventana_larga_en_bloques_de_max_dias():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 3, 31, tzinfo=timezone.utc)  # 90 dias
    bloques = bucket_dias(start, end, max_dias=31)
    assert [len(b) for b in bloques] == [31, 31, 28]
    assert bloques[0][0] == start
    assert bloques[-1][-1] == end


if __name__ == "__main__":
    test_grid_cell_centers_orden_fila_mayor_igual_a_flatten_de_rasterio()
    test_sample_at_point_ubica_el_pixel_correcto()
    test_sample_at_point_fuera_de_grilla_devuelve_none()
    test_sample_at_point_nodata_devuelve_none()
    test_compute_anomaly_grid_windowed_resta_climatologia_al_campo_corregido()
    test_compute_anomaly_grid_windowed_descarta_estaciones_fuera_de_grilla()
    test_compute_anomaly_grid_windowed_umbral_seco_fuerza_a_cero_pixeles_sin_lluvia()
    test_compute_anomaly_grid_windowed_valida_shape_de_elevation_grid()
    test_elevation_grid_for_remuestrea_a_la_grilla_destino()
    test_bucket_dias_no_corta_un_evento_de_menos_de_un_mes_en_el_limite_calendario()
    test_bucket_dias_parte_una_ventana_larga_en_bloques_de_max_dias()
    print("OK: todos los tests de anomaly_raster pasaron")
