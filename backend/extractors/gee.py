"""
Extractor de Google Earth Engine para frontal_sur.frames_raster
(GOES-19 nubosidad/vapor de agua, GPM IMERG precipitacion) y
frontal_sur.choropleth_stats (IMERG agregado por comuna).

Colecciones usadas:
- NOAA/GOES/19/MCMIPF: GOES-19 (GOES-East operativo desde 2025,
  reemplazo de GOES-16, que se verifico en vivo el 2026-07-16 con 0
  imagenes nuevas en 48 horas), Full Disk, cubre Sudamerica. Bandas
  CMI_C08/C09/C10 (vapor de agua alto/medio/bajo) y CMI_C13 (IR
  limpio, usado para el umbral de nubes altas del spec).
- NASA/GPM_L3/IMERG_V07: banda "precipitation" (mm/h).

Escalabilidad de la descarga: GOES emite un frame cada 10 minutos
(cientos por dia, varios MB cada uno). En vez de bajar todos, el
extractor trae la lista completa de timestamps en UNA sola llamada
(aggregate_array, no un getInfo por imagen) y elige client-side un
frame por hora. Ademas, si el GeoTIFF de un timestamp ya existe en
disco no se vuelve a descargar: las corridas de cron con ventanas
solapadas solo bajan lo nuevo.
"""

from datetime import datetime, timezone
from pathlib import Path

import ee
import numpy as np
import rasterio
import requests
from PIL import Image

from db import load_config
from extractors._retry import retry

GOES_COLLECTION = "NOAA/GOES/19/MCMIPF"
GOES_BANDS = ["CMI_C08", "CMI_C09", "CMI_C10", "CMI_C13"]
IMERG_COLLECTION = "NASA/GPM_L3/IMERG_V07"
IMERG_BAND = "precipitation"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "gee"

_initialized = False


def initialize(config: dict | None = None) -> None:
    """
    Inicializa Earth Engine con las credenciales OAuth ya cacheadas
    localmente por "earthengine authenticate" (fuente "Validado" del
    spec: ya se corrio una vez en este entorno). Idempotente dentro
    del proceso: si ya se inicializo, no vuelve a hacerlo.
    """
    global _initialized
    if _initialized:
        return
    if config is None:
        config = load_config()
    ee.Initialize(project=config["GEE_PROJECT"])
    _initialized = True


def _hourly_images(collection: "ee.ImageCollection") -> list[tuple["ee.Image", datetime]]:
    """
    Devuelve pares (imagen, valid_time) submuestreados a un frame por
    hora. Earth Engine es perezoso y server-side: iterar la coleccion
    con getInfo() por imagen seria una llamada de red por frame. Aca
    se trae la lista completa de timestamps en una sola llamada
    (aggregate_array conserva el orden de la coleccion, igual que
    toList), y la eleccion de que frames bajar se hace client-side:
    el primer frame de cada hora calendario.
    """
    times_ms = collection.aggregate_array("system:time_start").getInfo()
    if not times_ms:
        return []
    ee_list = collection.toList(len(times_ms))

    picked = []
    seen_hours = set()
    for index, time_ms in enumerate(times_ms):
        valid_time = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
        hour_key = valid_time.strftime("%Y%m%d%H")
        if hour_key in seen_hours:
            continue
        seen_hours.add(hour_key)
        picked.append((ee.Image(ee_list.get(index)), valid_time))
    return picked


@retry(times=3, backoff_seconds=2.0, exceptions=(ee.EEException, requests.RequestException))
def _download_geotiff(image: "ee.Image", region: "ee.Geometry", dest_path: Path, scale: int) -> None:
    """
    Pide a Earth Engine la URL de descarga de "image" recortada a
    "region" en formato GeoTIFF, y la descarga a dest_path. Envuelta
    en el decorador de reintentos: tanto la llamada a getDownloadURL
    (EEException si el token expiro y no se refresco a tiempo) como la
    descarga HTTP (RequestException) pueden fallar transitoriamente.

    Se fuerza crs EPSG:4326 porque la proyeccion nativa de GOES es
    geoestacionaria y el escritor de GeoTIFF de Earth Engine no la
    soporta (400 INVALID_ARGUMENT "Unable to write GeoTIFFs in
    projection", verificado en vivo); ademas deja los rasters ya en la
    misma proyeccion que usara el mapa web.
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


def _save_png_overlay(tif_path: Path, png_path: Path) -> None:
    """
    Genera un PNG de vista rapida a partir del GeoTIFF descargado: lee
    la primera banda, normaliza min-max a 0-255, y guarda en escala de
    grises. No es el renderizado final del mapa (eso lo decide el
    plan de webmapping); es un overlay de referencia para verificar
    visualmente que la descarga funciono.
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


def _frame_row(source_variable: str, region_name: str, valid_time: datetime,
               bbox: tuple, file_path: Path, png_path: Path) -> dict:
    return {
        "source": "gee",
        "variable": source_variable,
        "region": region_name,
        "valid_time": valid_time,
        "bbox": list(bbox),
        "file_path": str(file_path),
        "png_overlay_path": str(png_path),
        "created_at": datetime.now(timezone.utc),
    }


def _fetch_variable_frames(collection_id: str, bands, variable: str, scale: int,
                           start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Descarga los frames de una coleccion/variable dentro de la ventana
    y bbox dados (un frame por hora, ver _hourly_images), y devuelve
    una fila por frame para frontal_sur.frames_raster. Los archivos
    que ya existen en disco no se vuelven a descargar, pero si generan
    fila igual: el upsert del orquestador es idempotente y asi una
    corrida interrumpida antes de escribir a la BD se repara sola en
    la corrida siguiente.
    """
    region = ee.Geometry.Rectangle(list(bbox))
    collection = (
        ee.ImageCollection(collection_id)
        .filterDate(start.isoformat(), end.isoformat())
        .filterBounds(region)
        .select(bands)
    )
    rows = []
    for image, valid_time in _hourly_images(collection):
        stamp = valid_time.strftime("%Y%m%dT%H%M%S")
        tif_path = DATA_DIR / variable / f"{stamp}.tif"
        png_path = DATA_DIR / variable / f"{stamp}.png"
        if not tif_path.exists():
            _download_geotiff(image, region, tif_path, scale=scale)
        if not png_path.exists():
            _save_png_overlay(tif_path, png_path)
        rows.append(_frame_row(variable, "centro_sur", valid_time, bbox, tif_path, png_path))
    return rows


def fetch_frames(start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Descarga los frames GOES-19 (nubosidad/vapor de agua + IR) y GPM
    IMERG (precipitacion) dentro de la ventana [start, end] y el bbox
    dado, guarda cada uno como GeoTIFF + overlay PNG en
    data/frames/gee/<variable>/, y devuelve una fila por frame lista
    para frontal_sur.frames_raster.
    """
    initialize()
    rows = _fetch_variable_frames(GOES_COLLECTION, GOES_BANDS, "goes_cloud_moisture", 2000, start, end, bbox)
    rows += _fetch_variable_frames(IMERG_COLLECTION, IMERG_BAND, "imerg_precipitation", 10000, start, end, bbox)
    return rows