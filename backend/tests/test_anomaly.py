"""
Test plano (con assert, sin pytest) para anomaly.py.

Correr con: .venv/bin/python backend/tests/test_anomaly.py

Solo la matematica pura (IDW de residuos, Eq. 9 de Ossa-Moreno et al.
2019): no toca red ni BD. El camino de grilla real (raster CHIRPS,
GeoTIFF de salida) lo cubre extractors/anomaly_raster.py con sus
propios tests puros.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from anomaly import compute_anomaly, idw_residuals


def test_idw_residuals_en_la_estacion_misma_devuelve_su_propio_residuo():
    # Un punto de grilla que coincide EXACTO con una estacion: d=0,
    # division por cero se evita devolviendo el residuo de esa
    # estacion sin promediar con las demas (peso infinito en d=0).
    station_coords = [(0.0, 0.0), (1.0, 0.0)]
    residuals = [5.0, -3.0]
    grid_coords = [(0.0, 0.0)]
    result = idw_residuals(station_coords, residuals, grid_coords)
    assert result == [5.0]


def test_idw_residuals_punto_medio_es_promedio_simple():
    # Eq. 9: y(sj,t) = sum(y(si,t) / d(sj,si)) / sum(1 / d(sj,si)).
    # A igual distancia de dos estaciones, el resultado es el
    # promedio simple de sus residuos.
    station_coords = [(0.0, 0.0), (2.0, 0.0)]
    residuals = [10.0, 20.0]
    grid_coords = [(1.0, 0.0)]
    result = idw_residuals(station_coords, residuals, grid_coords)
    assert result == [15.0]


def test_idw_residuals_mas_cerca_pesa_mas():
    station_coords = [(0.0, 0.0), (10.0, 0.0)]
    residuals = [0.0, 100.0]
    grid_coords = [(1.0, 0.0)]  # mucho mas cerca de la estacion 0
    result = idw_residuals(station_coords, residuals, grid_coords)
    assert result[0] < 50.0


def test_idw_residuals_multiples_puntos_de_grilla_a_la_vez():
    # El calculo esta vectorizado (broadcasting numpy, sin loop python
    # por punto de grilla) para escalar a grillas de cientos de miles
    # de pixeles; este test confirma que procesar varios puntos de
    # grilla en una sola llamada da el mismo resultado que procesarlos
    # uno por uno.
    station_coords = [(0.0, 0.0), (2.0, 0.0)]
    residuals = [10.0, 20.0]
    grid_coords = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    result = idw_residuals(station_coords, residuals, grid_coords)
    assert result == [10.0, 15.0, 20.0]


def test_idw_residuals_respeta_batch_size_pequeno():
    # batch_size acota la memoria pico (matriz de distancias G x S) en
    # vez de materializarla completa; con un batch_size menor a la
    # cantidad de puntos de grilla el resultado debe ser identico.
    station_coords = [(0.0, 0.0), (2.0, 0.0)]
    residuals = [10.0, 20.0]
    grid_coords = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)]
    result = idw_residuals(station_coords, residuals, grid_coords, batch_size=1)
    assert result == [10.0, 15.0, 20.0]


def test_compute_anomaly_suma_residuo_interpolado_al_fondo():
    # compute_anomaly = idw_residuals(obs - fondo_en_estacion) +
    # fondo_en_grilla, tal como describe la seccion 3.2 de
    # Ossa-Moreno et al. 2019: "this interpolated surface is added
    # back to the original WC-CHIRPS values".
    station_values = [15.0]
    background_at_stations = [10.0]  # CHIRPS en la estacion: residuo = 5.0
    background_grid = [8.0]
    station_coords = [(0.0, 0.0)]
    grid_coords = [(0.0, 0.0)]  # mismo punto: residuo interpolado = 5.0
    result = compute_anomaly(
        station_values, background_at_stations, background_grid,
        station_coords, grid_coords,
    )
    assert result == [13.0]  # 8.0 (fondo en la grilla) + 5.0 (residuo)


if __name__ == "__main__":
    test_idw_residuals_en_la_estacion_misma_devuelve_su_propio_residuo()
    test_idw_residuals_punto_medio_es_promedio_simple()
    test_idw_residuals_mas_cerca_pesa_mas()
    test_idw_residuals_multiples_puntos_de_grilla_a_la_vez()
    test_idw_residuals_respeta_batch_size_pequeno()
    test_compute_anomaly_suma_residuo_interpolado_al_fondo()
    print("OK: todos los tests de anomaly pasaron")
