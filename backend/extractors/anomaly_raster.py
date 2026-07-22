"""
Plomeria de grilla y raster para el calculo de anomalias: separada de
anomaly.py (matematica pura, IDW de residuos) y de
extractors/chirps_climatology.py (descarga de la climatologia
historica) para que las tres piezas se puedan trabajar como tareas
independientes (ver docs/superpowers/plans/2026-07-19-anomalia-climatica-v1.md).

Combina el fondo CHIRPS prelim (GeoTIFF ya en disco, ver
extractors/chirps.py) con las observaciones de estacion y la
climatologia historica, y produce el array de anomalia listo para
escribir a GeoTIFF.

Correccion de resolucion temporal (2026-07-22): Ossa-Moreno et al. 2019
(Eq. 9) valida el ajuste de residuos a resolucion MENSUAL para
precipitacion (precipitacion diaria es demasiado ruidosa/con demasiados
ceros para que el residuo de un solo dia sea espacialmente coherente,
verificado en vivo). Este modulo adapta eso sin perder resolucion de
mapa: bucket_dias parte la ventana pedida en bloques de a lo mas ~31
dias SIN alinear al 1 del mes calendario (un evento de ~un mes que
cruza un limite de mes, ej. 19 de junio a 15 de julio, es UN bloque);
background_mensual suma el CHIRPS prelim de ese bloque completo (el
"fondo de ventana" que exige compute_anomaly_windowed en anomaly.py);
y compute_anomaly_grid_windowed calcula el residuo a esa resolucion de
bloque pero devuelve un mapa DIARIO, desagregado por la forma real de
CHIRPS dentro del bloque (mismo principio que usa CHIRPS v3 para pasar
de pentadas a diario con IMERG de referencia). La climatologia, en
cambio, se compara dia exacto contra dia exacto (climatologia_dia): la
agregacion que hace falta para no ser ruidosa ya la aporta el residuo
de bloque, no hace falta agregar tambien la climatologia.

Gradiente altitudinal, version liviana (2026-07-22, ver
anomaly.py::elevation_trend): CHIRPS subestima precipitacion en
terreno complejo/orografico (documentado en la literatura) y el IDW de
residuos no tiene ninguna nocion de altura, asi que en un dominio con
fuerte gradiente costa-cordillera (ej. Coquimbo) el campo corregido
salia sin estructura orografica -- verificado en vivo con un plot real.
elevation_grid_for remuestrea el DEM (extractors/dem.py) a la grilla
CHIRPS; compute_anomaly_grid_windowed lo usa (si se le pasa) para
destendenciar el residuo por elevacion antes del IDW. v2 completo (DEM
+ regresion con mas covariables + kriging de esos residuos) sigue
pospuesto, ver el plan.
"""

from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import rasterio
from rasterio.warp import Resampling, reproject
from rasterio.windows import from_bounds

from anomaly import apply_dry_threshold, compute_anomaly_windowed
from extractors import chirps, chirps_climatology


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


def compute_anomaly_grid_windowed(
    station_coords: list[tuple[float, float]],
    station_window_totals: list[float],
    background_window_grid: np.ndarray,
    background_transform: "rasterio.Affine",
    background_daily_grid: np.ndarray,
    climatology_daily_grid: np.ndarray,
    station_elevations: list[float] | None = None,
    elevation_grid: np.ndarray | None = None,
    dry_threshold: float = 1.0,
) -> np.ndarray:
    """
    Anomalia DIARIA de precipitacion: ajuste de residuos IDW a
    resolucion de BLOQUE (Ossa-Moreno et al. 2019, Eq. 9, ver
    anomaly.py::compute_anomaly_windowed) desagregado al dia por la
    forma real de CHIRPS, con umbral de lluvia espuria
    (apply_dry_threshold) y, si se dan station_elevations +
    elevation_grid, un detrend lineal residuo~elevacion ANTES del IDW
    (gradiente altitudinal, version liviana -- ver
    anomaly.py::elevation_trend).

    Estaciones sin pixel de fondo valido en la grilla de bloque (fuera
    de grilla o nodata) se descartan: no aportan residuo utilizable.
    Lanza ValueError si ninguna estacion cae dentro de la grilla.
    background_daily_grid y climatology_daily_grid deben compartir
    shape con background_window_grid (mismo bbox, misma resolucion
    CHIRPS nativa).
    """
    usar_elevacion = station_elevations is not None and elevation_grid is not None
    if usar_elevacion and elevation_grid.shape != background_window_grid.shape:
        raise ValueError("elevation_grid debe compartir shape con background_window_grid")

    background_window_at_stations = []
    coords_validos = []
    valores_validos = []
    elevaciones_validas = []
    for i, ((lon, lat), valor) in enumerate(zip(station_coords, station_window_totals)):
        fondo = sample_at_point(background_window_grid, background_transform, lon, lat)
        if fondo is None:
            continue
        background_window_at_stations.append(fondo)
        coords_validos.append((lon, lat))
        valores_validos.append(valor)
        if usar_elevacion:
            elevaciones_validas.append(station_elevations[i])

    if not coords_validos:
        raise ValueError("ninguna estacion cae dentro de la grilla de fondo CHIRPS")

    grid_coords = grid_cell_centers(background_transform, background_window_grid.shape)
    corregido = compute_anomaly_windowed(
        valores_validos, background_window_at_stations, background_window_grid.ravel().tolist(),
        background_daily_grid.ravel().tolist(), coords_validos, grid_coords,
        station_elevations=elevaciones_validas if usar_elevacion else None,
        elevation_grid=elevation_grid.ravel().tolist() if usar_elevacion else None,
    )
    corregido = apply_dry_threshold(corregido, threshold=dry_threshold)
    corregido_grid = np.array(corregido, dtype=np.float64).reshape(background_window_grid.shape)
    return corregido_grid - climatology_daily_grid


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


def _read_window(tif_path: Path, bbox: tuple):
    with rasterio.open(tif_path) as src:
        window = from_bounds(*bbox, transform=src.transform).round_offsets().round_lengths()
        transform = src.window_transform(window)
        data = src.read(1, window=window).astype("float64")
        profile = src.profile.copy()
        profile.update(height=int(window.height), width=int(window.width), transform=transform)
    return data, transform, profile


def bucket_dias(start: "datetime", end: "datetime", max_dias: int = 31) -> list[list["datetime"]]:
    """
    Divide [start, end] (inclusive) en bloques consecutivos de a lo
    mas "max_dias" dias, contados desde start -- NO alineados al 1 del
    mes calendario. Un evento de ~un mes que cruza un limite de mes
    (ej. 19 de junio a 15 de julio, 27 dias) es UN bloque, no "junio" +
    "julio" partidos en dos buckets mas cortos y cada uno comparado
    contra una climatologia de menos dias: partir por calendario
    subestima/distorsiona el evento sin ninguna razon fisica, el limite
    de mes es una convencion administrativa, no algo que le importe al
    sistema frontal.
    """
    dias = []
    dia = start
    while dia <= end:
        dias.append(dia)
        dia += timedelta(days=1)
    return [dias[i:i + max_dias] for i in range(0, len(dias), max_dias)]


def background_mensual(dias: list["datetime"], bbox: tuple):
    """
    Suma el CHIRPS prelim (recortado a bbox por ventana, ya en disco)
    de cada dia en "dias" -- el "fondo de ventana"/"fondo de bloque"
    que exige compute_anomaly_windowed. Dias sin archivo en disco
    todavia se excluyen de la suma (no del calculo completo): un
    evento en curso normalmente tiene el CHIRPS mas reciente todavia
    sin publicar.
    """
    total = None
    transform = profile = None
    dias_usados = []
    for dia in dias:
        tif = chirps.DATA_DIR / "chirps_precipitation" / f"{dia:%Y%m%dT000000}.tif"
        if not tif.exists():
            continue
        data, t, p = _read_window(tif, bbox)
        if total is None:
            total = np.zeros_like(data)
            transform, profile = t, p
        total += np.nan_to_num(data, nan=0.0)
        dias_usados.append(dia)
    if total is None:
        raise ValueError("sin CHIRPS prelim en disco para ningun dia de este bloque")
    return total, transform, profile, dias_usados


def background_dia(dia: "datetime", bbox: tuple):
    """
    Lee el CHIRPS prelim (recortado a bbox por ventana, ya en disco)
    de un solo dia -- el "fondo diario" que exige
    compute_anomaly_grid_windowed para desagregar el bloque al dia.
    None si el archivo de ese dia todavia no esta en disco.
    """
    tif = chirps.DATA_DIR / "chirps_precipitation" / f"{dia:%Y%m%dT000000}.tif"
    if not tif.exists():
        return None
    return _read_window(tif, bbox)


def climatologia_dia(month: int, day: int, bbox: tuple,
                      first_year: int = chirps_climatology.CLIMATOLOGY_FIRST_YEAR,
                      last_year: int = chirps_climatology.CLIMATOLOGY_LAST_YEAR):
    """
    Climatologia CHIRPS final/sat de un dia-del-anio EXACTO (month,
    day), recortada por ventana a "bbox" desde el cache ya en disco
    (chirps_climatology.DATA_DIR): promedia los anios cacheados sin
    descargar nada nuevo. Si ningun anio esta cacheado todavia para
    ese dia, delega en chirps_climatology.climatology_for_dayofyear
    (que si descarga, recortado a "bbox"). Leer por ventana desde un
    archivo ya bajado a un bbox mas grande (ej. REGION_BBOX nacional)
    evita una segunda descarga cuando despues se pide un sub-bbox (ej.
    Coquimbo) del mismo dia-del-anio.
    """
    capas = []
    transform = None
    for year in range(first_year, last_year + 1):
        try:
            datetime(year, month, day)
        except ValueError:
            continue  # 29 feb en anio no bisiesto
        path = chirps_climatology.DATA_DIR / f"{month:02d}{day:02d}" / f"{year}.tif"
        if not path.exists():
            continue
        data, t, _ = _read_window(path, bbox)
        capas.append(data)
        transform = t
    if not capas:
        return chirps_climatology.climatology_for_dayofyear(bbox, month, day, first_year, last_year)
    return np.nanmean(np.stack(capas), axis=0), transform


def elevation_grid_for(dem_path: Path, target_transform: "rasterio.Affine", target_shape: tuple[int, int]) -> np.ndarray:
    """
    Remuestrea el DEM (extractors/dem.py, resolucion nativa mas fina)
    a la MISMA grilla (transform/shape) que el fondo CHIRPS,
    promediando la elevacion dentro de cada celda (resampling
    "average"): la regresion residuo~elevacion necesita que ambas
    grillas compartan exactamente los mismos pixeles.
    """
    with rasterio.open(dem_path) as src:
        destino = np.empty(target_shape, dtype="float64")
        reproject(
            source=rasterio.band(src, 1), destination=destino,
            src_transform=src.transform, src_crs=src.crs,
            dst_transform=target_transform, dst_crs="EPSG:4326",
            resampling=Resampling.average,
        )
    return destino
