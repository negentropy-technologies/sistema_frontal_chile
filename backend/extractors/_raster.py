"""
Utilidades raster compartidas por los extractores que descargan
GeoTIFF (GEE/GOES, CHIRPS, NASA IMERG, DEM). Separadas de gee.py para
que los extractores que no usan Earth Engine no dependan de ese
modulo.
"""

from pathlib import Path

import ee
import numpy as np
import rasterio
import requests
from PIL import Image

from extractors._retry import retry


@retry(times=3, backoff_seconds=2.0, exceptions=(ee.EEException, requests.RequestException))
def download_geotiff(image: "ee.Image", region: "ee.Geometry", dest_path: Path, scale: int) -> None:
    """
    Pide a Earth Engine la URL de descarga de "image" recortada a
    "region" en formato GeoTIFF, y la descarga a dest_path. Envuelta
    en el decorador de reintentos: tanto la llamada a getDownloadURL
    (EEException si el token expiro y no se refresco a tiempo) como la
    descarga HTTP (RequestException) pueden fallar transitoriamente.

    Se fuerza crs EPSG:4326: GOES lo necesita porque su proyeccion
    nativa es geoestacionaria y el escritor de GeoTIFF de Earth Engine
    no la soporta (400 INVALID_ARGUMENT "Unable to write GeoTIFFs in
    projection", verificado en vivo); para DEM (ya en 4326 nativo) no
    cambia nada, asi que forzarlo siempre es seguro para ambos casos.
    """
    url = image.getDownloadURL({
        "region": region,
        "scale": scale,
        "crs": "EPSG:4326",
        "format": "GEO_TIFF",
    })
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(response.content)


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
