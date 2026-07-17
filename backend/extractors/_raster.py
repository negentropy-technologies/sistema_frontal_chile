"""
Utilidades raster compartidas por los extractores que descargan
GeoTIFF (GEE/GOES, CHIRPS, NASA IMERG). Separadas de gee.py para que
los extractores que no usan Earth Engine no dependan de ese modulo.
"""

from pathlib import Path

import numpy as np
import rasterio
from PIL import Image


def save_png_overlay(tif_path: Path, png_path: Path) -> None:
    """
    Genera un PNG de vista rapida a partir de un GeoTIFF: lee la
    primera banda, normaliza min-max a 0-255, y guarda en escala de
    grises. No es el renderizado final del mapa (eso lo decide el
    plan de webmapping); es un overlay de referencia para verificar
    visualmente que la descarga funciono. Los NaN (nodata) quedan
    fuera de la normalizacion.
    """
    with rasterio.open(tif_path) as src:
        band = src.read(1).astype("float64")
    finite = band[np.isfinite(band)]
    if finite.size == 0:
        normalized = np.zeros_like(band, dtype="uint8")
    else:
        band_min, band_max = finite.min(), finite.max()
        span = band_max - band_min or 1.0
        normalized = np.clip((band - band_min) / span * 255, 0, 255)
        normalized = np.nan_to_num(normalized).astype("uint8")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(normalized, mode="L").save(png_path)
