"""
Descarga compartida de GeoTIFF diarios CHIRPS v3.0 desde
data.chc.ucsb.edu, reusada por extractors/chirps.py (rama prelim/sat,
evento actual) y extractors/chirps_climatology.py (rama final/sat,
climatologia historica): mismo servidor, mismo formato de archivo y
misma logica de recorte, solo cambia la rama y el "infix" del nombre
de archivo. Antes de este modulo cada extractor traia su propia copia
de _crop_day -- duplicacion real (no solo estructural, como el patron
_stations_in_bbox de dga/dmc/agromet, que si difiere por fuente) sobre
la misma familia de datos, corregida a pedido del usuario.
"""

from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds

from extractors._retry import retry

CHC_DAILY_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily"


def day_url(branch: str, infix: str, year: int, month: int, day: int) -> str:
    """
    branch: subcarpeta bajo ".../daily/" (ej "prelim/sat", "final/sat").
    infix: token que el CHC usa en el nombre de archivo para esa rama
    (ej "prelim", "sat"), verificado en vivo el 2026-07-19 contra
    data.chc.ucsb.edu para ambas ramas.
    """
    return f"{CHC_DAILY_BASE_URL}/{branch}/{year}/chirps-v3.0.{infix}.{year}.{month:02d}.{day:02d}.tif"


@retry(times=3, backoff_seconds=2.0, exceptions=(rasterio.errors.RasterioIOError,))
def crop_day(url: str, bbox: tuple, dest_path: Path) -> None:
    """
    Lee del GeoTIFF global remoto solo la ventana del bbox (lectura
    por rango HTTP via GDAL/vsicurl) y la guarda como GeoTIFF local.
    El nodata de CHIRPS (-9999) se convierte a NaN para que el overlay
    PNG y los calculos de anomalia normalicen solo valores reales.
    """
    xmin, ymin, xmax, ymax = bbox
    with rasterio.open(url) as src:
        window = from_bounds(xmin, ymin, xmax, ymax, src.transform)
        data = src.read(1, window=window).astype("float32")
        nodata = src.nodata if src.nodata is not None else -9999.0
        data[data == nodata] = np.nan
        profile = src.profile.copy()
        profile.update(
            width=data.shape[1],
            height=data.shape[0],
            transform=src.window_transform(window),
            dtype="float32",
            nodata=np.nan,
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest_path, "w", **profile) as dst:
        dst.write(data, 1)
