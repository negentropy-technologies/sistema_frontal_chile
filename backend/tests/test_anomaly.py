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

from anomaly import apply_dry_threshold, compute_anomaly_windowed, elevation_trend, idw_residuals


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


def test_compute_anomaly_windowed_desagrega_proporcional_al_chirps_diario():
    # Ventana de 30 dias: estacion acumulo 150mm, CHIRPS acumulo 100mm
    # en esa misma estacion (residuo de ventana = +50). Grilla y
    # estacion coinciden, asi que el residuo interpolado es +50 en la
    # grilla: campo corregido de la ventana = 100 + 50 = 150. El dia de
    # hoy CHIRPS crudo vale 5mm de los 100mm de la ventana (5% de la
    # forma diaria) => desagregado = 150 * (5/100) = 7.5.
    result = compute_anomaly_windowed(
        station_window_totals=[150.0],
        background_window_at_stations=[100.0],
        background_window_grid=[100.0],
        background_daily_grid=[5.0],
        station_coords=[(0.0, 0.0)],
        grid_coords=[(0.0, 0.0)],
    )
    assert result == [7.5]


def test_compute_anomaly_windowed_ventana_seca_da_cero():
    # CHIRPS no vio lluvia en toda la ventana en este pixel: no hay
    # forma diaria de la cual desagregar, se fuerza a 0 en vez de
    # dividir por cero.
    result = compute_anomaly_windowed(
        station_window_totals=[150.0],
        background_window_at_stations=[0.0],
        background_window_grid=[0.0],
        background_daily_grid=[0.0],
        station_coords=[(0.0, 0.0)],
        grid_coords=[(0.0, 0.0)],
    )
    assert result == [0.0]


def test_apply_dry_threshold_fuerza_a_cero_bajo_el_umbral():
    result = apply_dry_threshold([0.3, 0.9, 1.0, 5.0], threshold=1.0)
    assert result == [0.0, 0.0, 1.0, 5.0]


def test_elevation_trend_ajusta_pendiente_lineal():
    # Residuos que siguen EXACTO una recta residuo = 0.1 * elevacion.
    pendiente, intercepto = elevation_trend([0.0, 10.0, 20.0], [0.0, 100.0, 200.0])
    assert abs(pendiente - 0.1) < 1e-9
    assert abs(intercepto) < 1e-9


def test_elevation_trend_sin_informacion_suficiente_devuelve_pendiente_cero():
    # Una sola estacion: no hay como estimar una pendiente.
    pendiente, intercepto = elevation_trend([5.0], [100.0])
    assert pendiente == 0.0
    assert intercepto == 5.0
    # Elevacion constante entre estaciones: tampoco hay pendiente que
    # estimar (division por cero en el ajuste si se intentara).
    pendiente, intercepto = elevation_trend([5.0, -3.0], [100.0, 100.0])
    assert pendiente == 0.0
    assert intercepto == 1.0


def test_compute_anomaly_windowed_elevacion_agrega_tendencia_a_la_grilla():
    # 2 estaciones cuyos residuos (obs - fondo) caen EXACTO sobre la
    # recta residuo = 0.1 * elevacion (0.0 a elevacion 0, 10.0 a
    # elevacion 100): una vez destendenciados por elevacion, el
    # residuo IDW-interpolable es 0 en las dos, asi que TODA la senal
    # en el punto de grilla viene de la tendencia de elevacion, no de
    # un residuo de estacion "prestado" por cercania horizontal.
    result = compute_anomaly_windowed(
        station_window_totals=[100.0, 100.0],
        background_window_at_stations=[100.0, 90.0],  # residuos: 0.0 y 10.0
        background_window_grid=[100.0],
        background_daily_grid=[100.0],  # igual al de ventana: sin desagregar (razon 1)
        station_coords=[(0.0, 0.0), (10.0, 0.0)],
        grid_coords=[(5.0, 0.0)],
        station_elevations=[0.0, 100.0],
        elevation_grid=[50.0],  # elevacion del punto de grilla: recta predice residuo 5.0
    )
    # window_corrected = 0.0 (idw del residuo sin tendencia) + 5.0
    # (tendencia en la grilla) + 100.0 (fondo) = 105.0.
    assert abs(result[0] - 105.0) < 1e-9


if __name__ == "__main__":
    test_idw_residuals_en_la_estacion_misma_devuelve_su_propio_residuo()
    test_idw_residuals_punto_medio_es_promedio_simple()
    test_idw_residuals_mas_cerca_pesa_mas()
    test_idw_residuals_multiples_puntos_de_grilla_a_la_vez()
    test_idw_residuals_respeta_batch_size_pequeno()
    test_compute_anomaly_windowed_desagrega_proporcional_al_chirps_diario()
    test_compute_anomaly_windowed_ventana_seca_da_cero()
    test_apply_dry_threshold_fuerza_a_cero_bajo_el_umbral()
    test_elevation_trend_ajusta_pendiente_lineal()
    test_elevation_trend_sin_informacion_suficiente_devuelve_pendiente_cero()
    test_compute_anomaly_windowed_elevacion_agrega_tendencia_a_la_grilla()
    print("OK: todos los tests de anomaly pasaron")
