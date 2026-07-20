"""
Ajuste de residuos para anomalias climaticas: metodologia de
Ossa-Moreno, J., Keir, G., McIntyre, N., Cameletti, M., & Rivera, D.
(2019). "Comparison of approaches to interpolating climate
observations in steep terrain with low-density gauging networks".
Hydrology and Earth System Sciences, 23, 4763-4781.
https://doi.org/10.5194/hess-23-4763-2019

Seccion 3.2 del paper: el residuo entre la observacion de estacion y
un producto de fondo (WorldClim/CHIRPS) se calcula en la ubicacion de
cada estacion, se interpola con IDW (Eq. 9) a cada punto de interes, y
esa superficie interpolada se SUMA de vuelta al valor original del
producto de fondo. El paper usa esto a resolucion mensual para
precipitacion; aqui se aplica a resolucion diaria (evento de ~15 dias,
no meses) -- adaptacion documentada de la tecnica, no una replica
literal del paper (ver docs/superpowers/plans/2026-07-19-anomalia-climatica-v1.md).

IDW se implementa a mano con numpy en vez de agregar scipy (la formula,
Eq. 9, es una suma ponderada simple) y vectorizado por lotes de puntos
de grilla en vez de un loop Python por punto: el bbox del proyecto
(Coquimbo a Magallanes) produce grillas de cientos de miles de pixeles,
y un loop Python ahi es el cuello de botella real.

Ajuste metodologico 2026-07-19 (prompt maestro de correccion de
anomalias): el paper valida la correccion de residuos a resolucion
MENSUAL (r debil a diario, 0.81 a mensual, seccion 3.2 y figura 4c-d).
compute_anomaly_windowed (punto 1, prioridad alta) mueve el calculo del
residuo a una ventana de 30 dias y desagrega el resultado al dia
proporcionalmente al CHIRPS crudo diario, siguiendo el mismo principio
que usa CHIRPS v3 para pasar de totales pentadales a diarios con IMERG
como referencia de forma. apply_dry_threshold (punto 4) fuerza a cero
la precipitacion espuria que el IDW predice en pixeles sin lluvia
observada.

Pendiente, NO implementado en este archivo (requiere fuentes que el
checklist del prompt maestro marca como no extraidas todavia, o tocan
otros modulos):
- Punto 2 (climatologia por ventana movil, no dia calendario exacto):
  extractors/chirps_climatology.py.
- Punto 3 (periodo base alternativo 1991-2020 / dual): requiere CHIRPS
  1991-1997, no extraido.
- Punto 5 (covariable de elevacion / KED): requiere DEM, no extraido;
  escribir la formula sin datos reales para validarla es prematuro.
- Puntos 6-9 (metadatos de incertidumbre, mascara de Valparaiso, SPI,
  advertencias regionales): extractors/anomaly_raster.py o capa de
  presentacion, no calculo de residuos.
- Punto 10 (sustitucion CHIRPS/IMERG en ventana de rezago): pipeline de
  ingesta, no este archivo.
"""

import numpy as np

# Tamano de lote por defecto para idw_residuals: acota la matriz de
# distancias (batch x n_estaciones) que se materializa a la vez, para
# que la memoria pico no dependa del tamano total de la grilla.
DEFAULT_BATCH_SIZE = 50_000


def idw_residuals(
    station_coords: list[tuple[float, float]],
    residuals: list[float],
    grid_coords: list[tuple[float, float]],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[float]:
    """
    Eq. 9 de Ossa-Moreno et al. (2019):
    y(sj,t) = sum_i[ y(si,t) / d(sj,si) ] / sum_i[ 1 / d(sj,si) ]

    Si un punto de grilla coincide exactamente con una estacion
    (d=0), se devuelve el residuo de esa estacion sin ponderar con las
    demas (evita division por cero y es el limite natural de IDW
    cuando d tiende a 0).
    """
    stations = np.asarray(station_coords, dtype=np.float64)  # (S, 2)
    values = np.asarray(residuals, dtype=np.float64)          # (S,)
    grid = np.asarray(grid_coords, dtype=np.float64)          # (G, 2)

    out = np.empty(len(grid), dtype=np.float64)
    for start in range(0, len(grid), max(batch_size, 1)):
        batch = grid[start:start + batch_size]                # (B, 2)
        diff = batch[:, np.newaxis, :] - stations[np.newaxis, :, :]  # (B, S, 2)
        dist = np.sqrt((diff ** 2).sum(axis=2))                # (B, S)

        exact = dist == 0
        safe_dist = np.where(exact, 1.0, dist)
        weights = np.where(exact, 0.0, 1.0 / safe_dist)
        weight_sum = weights.sum(axis=1)
        weighted_sum = (values[np.newaxis, :] * weights).sum(axis=1)

        result = np.divide(
            weighted_sum, weight_sum,
            out=np.zeros_like(weighted_sum), where=weight_sum != 0,
        )
        has_exact = exact.any(axis=1)
        if has_exact.any():
            exact_idx = np.argmax(exact, axis=1)
            result = np.where(has_exact, values[exact_idx], result)
        out[start:start + batch_size] = result
    return out.tolist()


def compute_anomaly(
    station_values: list[float],
    background_at_stations: list[float],
    background_grid: list[float],
    station_coords: list[tuple[float, float]],
    grid_coords: list[tuple[float, float]],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[float]:
    """
    Campo ajustado = fondo_en_grilla + IDW(observacion - fondo en cada
    estacion). "Fondo" es CHIRPS prelim (el evento actual). Para
    reportar una ANOMALIA respecto de la climatologia historica, el
    caller resta aparte la climatologia del mismo dia-del-anio al
    resultado de esta funcion (ver extractors/anomaly_raster.py).
    """
    residuals = (np.asarray(station_values) - np.asarray(background_at_stations)).tolist()
    interpolated = idw_residuals(station_coords, residuals, grid_coords, batch_size=batch_size)
    return (np.asarray(interpolated) + np.asarray(background_grid)).tolist()


def compute_anomaly_windowed(
    station_window_totals: list[float],
    background_window_at_stations: list[float],
    background_window_grid: list[float],
    background_daily_grid: list[float],
    station_coords: list[tuple[float, float]],
    grid_coords: list[tuple[float, float]],
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> list[float]:
    """
    Punto 1 (prioridad alta) del prompt maestro de ajuste
    metodologico: el residuo se calcula sobre la ventana de 30 dias
    (suma de observacion y de fondo CHIRPS en cada estacion, no el dia
    suelto) y se interpola con IDW igual que compute_anomaly. El campo
    corregido de la ventana se desagrega al dia multiplicando por la
    razon (CHIRPS crudo del dia / CHIRPS crudo de la ventana) en cada
    pixel: la forma diaria la sigue dando CHIRPS, solo el sesgo se
    corrige a la resolucion en que el metodo esta validado.

    Si el fondo de la ventana es 0 en un pixel (CHIRPS no vio lluvia en
    los 30 dias), no hay forma diaria de la cual repartir el ajuste y
    el resultado se fuerza a 0 en ese pixel.
    """
    window_residuals = (
        np.asarray(station_window_totals) - np.asarray(background_window_at_stations)
    ).tolist()
    interpolated = idw_residuals(station_coords, window_residuals, grid_coords, batch_size=batch_size)
    window_corrected = np.asarray(interpolated) + np.asarray(background_window_grid)

    window_background = np.asarray(background_window_grid)
    shape = np.divide(
        np.asarray(background_daily_grid), window_background,
        out=np.zeros_like(window_background, dtype=np.float64), where=window_background != 0,
    )
    return (window_corrected * shape).tolist()


def apply_dry_threshold(values: list[float], threshold: float = 1.0) -> list[float]:
    """
    Punto 4 del prompt maestro: Ossa-Moreno et al. (2019) fuerzan a
    cero, a escala mensual, los valores por debajo de 1 mm para bajar
    la tasa de falsa alarma del IDW en periodos secos. El umbral queda
    configurable porque el valor apropiado cambia con la resolucion
    temporal (mensual en el paper, diaria o de ventana aqui).
    """
    arr = np.asarray(values, dtype=np.float64)
    return np.where(arr < threshold, 0.0, arr).tolist()
