"""
Elevacion (DEM) para la "version liviana" del gradiente altitudinal en
anomalias de precipitacion (ver anomaly.py::elevation_trend y
docs/superpowers/plans/2026-07-19-anomalia-climatica-v1.md, seccion
v2: DEM + regresion + KRIGING completo de los residuos de esa
regresion, pospuesto -- esto es solo DEM + regresion lineal simple,
sin kriging).

Fuente: Copernicus DEM GLO30 (COPERNICUS/DEM/GLO30 en GEE), banda
"DEM", 30 m nativos -- elegido sobre SRTM (USGS/SRTMGL1_003) porque
SRTM no cubre al sur de 56 S y el bbox nacional del proyecto llega a
Magallanes (-56.538, ver REGION_BBOX en ingest.py); Copernicus DEM
tiene cobertura global casi completa. Es un ImageCollection de tiles,
por eso hace falta .mosaic() antes de recortar al bbox.

A diferencia del resto de los extractores, la elevacion no cambia en
el tiempo: se descarga UNA VEZ por bbox (sin fecha en el nombre de
archivo) y se cachea indefinidamente.
"""

from pathlib import Path

import ee

from extractors._raster import download_geotiff
from extractors.gee import initialize
from logutil import log

DEM_ASSET = "COPERNICUS/DEM/GLO30"
DEM_BAND = "DEM"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "dem"


def _cache_path(bbox: tuple) -> Path:
    slug = "_".join(f"{v:.2f}" for v in bbox)
    return DATA_DIR / f"{slug}.tif"


def fetch_elevation(bbox: tuple, scale: int = 500) -> Path:
    """
    Devuelve la ruta al GeoTIFF de elevacion (banda unica, metros)
    para "bbox", descargandolo de Copernicus DEM GLO30 la primera vez
    que se pide ese bbox exacto (los siguientes llamados reusan el
    archivo en disco). scale=500, no la resolucion nativa de 30 m: el
    fondo CHIRPS con el que se compara es de 0.05 grados (~5 km), asi
    que 500 m ya sobra de detalle para una regresion lineal simple
    (no hace falta pendiente/microrelieve), y a 30 m sobre un bbox
    nacional se supera el limite de 48 MB de getDownloadURL.
    """
    dest_path = _cache_path(bbox)
    if dest_path.exists():
        return dest_path
    initialize()
    region = ee.Geometry.Rectangle(list(bbox))
    image = ee.ImageCollection(DEM_ASSET).select(DEM_BAND).mosaic().clip(region)
    download_geotiff(image, region, dest_path, scale=scale)
    log(f"dem: elevacion descargada para bbox {bbox}")
    return dest_path
