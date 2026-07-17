"""
Extractor de GPM IMERG Early directo desde NASA GES DISC para
frontal_sur.frames_raster y frontal_sur.choropleth_stats. Reemplaza
al IMERG que antes llegaba via Google Earth Engine, por la politica
del proyecto de preferir la API del emisor original del dato: GES
DISC publica el Early media-horario con ~4 horas de latencia (contra
~24 horas del catalogo de GEE, medido el 2026-07-17).

Productos usados (auth Basic de Earthdata, credenciales
EARTHDATA_USER/EARTHDATA_PASSWORD de backend/.env):
- GPM_3IMERGDE.07: diario Early (mm/dia), un nc4 global por dia.
- GPM_3IMERGHHE.07: media-horario Early (mm/h), un HDF5 global cada
  30 minutos, en directorios por dia juliano.

Los archivos globales (1800x3600 en layout lon,lat SIN georreferencia,
verificado en vivo) se descargan completos porque GES DISC no permite
lectura por rango con esta auth; el extractor transpone, voltea a
norte-arriba, recorta al bbox con la grilla global conocida de 0.1
grados, guarda GeoTIFF + overlay PNG, y borra el archivo global para
no acumular ~8 MB por granulo en disco.

Las coropletas por comuna se calculan aqui mismo con rasterio
(mascara de geometria por comuna sobre la suma de los diarios de la
ventana), sin Earth Engine.
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import rasterio
import requests
from rasterio.features import geometry_mask
from rasterio.transform import from_origin

from db import load_config
from extractors._http import mount_retries
from extractors._raster import save_png_overlay

GESDISC_BASE = "https://gpm1.gesdisc.eosdis.nasa.gov/data/GPM_L3"
DAILY_PRODUCT = "GPM_3IMERGDE.07"
HHR_PRODUCT = "GPM_3IMERGHHE.07"

# Grilla global de IMERG: 0.1 grados, lon -180..180, lat -90..90.
GRID_RES = 0.1

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "nasa_imerg"

# Regiones (id_region de dpa_limites.dpa_region_subdere) que entran en
# el bbox del proyecto: Metropolitana a Los Lagos. Mismo listado del
# spec para que la agregacion por comuna cubra la misma zona que los
# rasters.
CHOROPLETH_REGION_IDS = (13, 6, 7, 16, 8, 9, 14, 10)

_comuna_cache = None


class EarthdataSession(requests.Session):
    """
    Session que conserva el header Authorization solo cuando la
    redireccion involucra a urs.earthdata.nasa.gov (el login central
    de NASA): requests lo elimina en cualquier cambio de host, y sin
    el, URS responde 401. Patron recomendado por la propia NASA.
    """

    AUTH_HOST = "urs.earthdata.nasa.gov"

    def rebuild_auth(self, prepared_request, response):
        headers = prepared_request.headers
        url = prepared_request.url
        if "Authorization" in headers:
            original = urlparse(response.request.url).hostname
            redirect = urlparse(url).hostname
            if original != redirect and self.AUTH_HOST not in (original, redirect):
                del headers["Authorization"]


def _session(config: dict) -> EarthdataSession:
    session = EarthdataSession()
    session.auth = (config["EARTHDATA_USER"], config["EARTHDATA_PASSWORD"])
    mount_retries(session)
    return session


def _bbox_slices(bbox: tuple) -> tuple[slice, slice]:
    """
    Indices de la grilla global 0.1 grados que cubren el bbox, como
    (filas de lat norte-arriba, columnas de lon). La malla de IMERG
    parte en (-180, -90); tras transponer y voltear el arreglo queda
    norte-arriba, por eso las filas se cuentan desde +90.
    """
    xmin, ymin, xmax, ymax = bbox
    col0 = int((xmin + 180) / GRID_RES)
    col1 = int((xmax + 180) / GRID_RES)
    row0 = int((90 - ymax) / GRID_RES)
    row1 = int((90 - ymin) / GRID_RES)
    return slice(row0, row1), slice(col0, col1)


def _crop_to_tif(global_file: Path, subdataset_prefix: str, bbox: tuple, dest_path: Path) -> None:
    """
    Lee la banda "precipitation" del archivo global de IMERG (nc4 o
    HDF5), la reorienta (viene como lon,lat sin georreferencia:
    transponer + voltear deja norte-arriba), recorta al bbox y guarda
    GeoTIFF EPSG:4326 con nodata NaN.
    """
    with rasterio.open(_subdataset_path(global_file, subdataset_prefix)) as src:
        raw = src.read(1).astype("float32")
    raw[raw < 0] = np.nan
    world = np.flipud(raw.transpose())
    rows, cols = _bbox_slices(bbox)
    data = world[rows, cols]

    xmin, _, _, ymax = bbox
    transform = from_origin(xmin, ymax, GRID_RES, GRID_RES)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        dest_path, "w", driver="GTiff", width=data.shape[1], height=data.shape[0],
        count=1, dtype="float32", crs="EPSG:4326", transform=transform, nodata=np.nan,
    ) as dst:
        dst.write(data, 1)


def _subdataset_path(global_file: Path, subdataset_prefix: str) -> str:
    """
    Arma la ruta GDAL del subdataset "precipitation" segun el formato:
    NETCDF:"archivo":precipitation para los diarios nc4, y
    HDF5:"archivo"://Grid/precipitation para los media-horarios.
    """
    if subdataset_prefix == "NETCDF":
        return f'NETCDF:"{global_file}":precipitation'
    return f'HDF5:"{global_file}"://Grid/precipitation'


def _listing(session: EarthdataSession, dir_url: str, pattern: str) -> list[str]:
    """
    Lista los archivos de un directorio HTML de GES DISC que calzan
    con el patron. Un 404 (dia juliano aun sin directorio) devuelve
    lista vacia en vez de error: es la latencia normal del producto.
    """
    response = session.get(dir_url, timeout=60)
    if response.status_code == 404:
        return []
    response.raise_for_status()
    return sorted(set(re.findall(pattern, response.text)))


def _download(session: EarthdataSession, url: str, dest: Path) -> None:
    response = session.get(url, timeout=300)
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)


def _fetch_daily(session: EarthdataSession, start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Un GeoTIFF recortado por dia (mm/dia) dentro de [start, end]. Los
    dias que GES DISC aun no publica (~1 dia de latencia) se saltan y
    los recogen corridas siguientes.
    """
    rows = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        stamp = day.strftime("%Y%m%dT000000")
        tif_path = DATA_DIR / "imerg_early_daily" / f"{stamp}.tif"
        png_path = DATA_DIR / "imerg_early_daily" / f"{stamp}.png"
        if not tif_path.exists():
            dir_url = f"{GESDISC_BASE}/{DAILY_PRODUCT}/{day.year}/{day.month:02d}/"
            fecha = day.strftime("%Y%m%d")
            files = _listing(session, dir_url, rf'href="(3B-DAY-E\.MS\.MRG\.3IMERG\.{fecha}-[^"]+\.nc4)"')
            if not files:
                day += timedelta(days=1)
                continue
            global_file = tif_path.parent / files[0]
            _download(session, dir_url + files[0], global_file)
            _crop_to_tif(global_file, "NETCDF", bbox, tif_path)
            global_file.unlink()
        if not png_path.exists():
            save_png_overlay(tif_path, png_path)
        rows.append(_row("imerg_early_daily", day, bbox, tif_path, png_path))
        day += timedelta(days=1)
    return rows


def _fetch_half_hourly(session: EarthdataSession, start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Un GeoTIFF recortado por granulo de 30 minutos (mm/h) dentro de
    [start, end]. El archivo global (~8 MB) se descarga entero, se
    recorta y se borra; con el skip de archivos ya recortados, las
    corridas de cron solo bajan los granulos nuevos (~4 horas de
    latencia del Early).
    """
    rows = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        dir_url = f"{GESDISC_BASE}/{HHR_PRODUCT}/{day.year}/{day.strftime('%j')}/"
        fecha = day.strftime("%Y%m%d")
        files = _listing(session, dir_url, rf'href="(3B-HHR-E\.MS\.MRG\.3IMERG\.{fecha}-S\d{{6}}-[^"]+\.HDF5)"')
        for filename in files:
            hora = re.search(r"-S(\d{6})-", filename).group(1)
            valid_time = day.replace(hour=int(hora[:2]), minute=int(hora[2:4]), second=int(hora[4:]))
            if not (start <= valid_time <= end):
                continue
            stamp = valid_time.strftime("%Y%m%dT%H%M%S")
            tif_path = DATA_DIR / "imerg_early_30min" / f"{stamp}.tif"
            png_path = DATA_DIR / "imerg_early_30min" / f"{stamp}.png"
            if not tif_path.exists():
                global_file = tif_path.parent / filename
                _download(session, dir_url + filename, global_file)
                _crop_to_tif(global_file, "HDF5", bbox, tif_path)
                global_file.unlink()
            if not png_path.exists():
                save_png_overlay(tif_path, png_path)
            rows.append(_row("imerg_early_30min", valid_time, bbox, tif_path, png_path))
        day += timedelta(days=1)
    return rows


def _row(variable: str, valid_time: datetime, bbox: tuple, tif_path: Path, png_path: Path) -> dict:
    return {
        "source": "nasa_imerg",
        "variable": variable,
        "region": "centro_sur",
        "valid_time": valid_time,
        "bbox": list(bbox),
        "file_path": str(tif_path),
        "png_overlay_path": str(png_path),
        "created_at": datetime.now(timezone.utc),
    }


def fetch_frames(start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Descarga los frames IMERG Early diarios y media-horarios de la
    ventana [start, end], recortados al bbox, y devuelve una fila por
    frame para frontal_sur.frames_raster.
    """
    session = _session(load_config())
    rows = _fetch_daily(session, start, end, bbox)
    rows += _fetch_half_hourly(session, start, end, bbox)
    return rows


def _comuna_geometries() -> list[tuple[int, dict]]:
    """
    Lee de Postgres (rol frontal_sur_app) las geometrias de las
    comunas de CHOROPLETH_REGION_IDS como GeoJSON simplificado
    (tolerancia ~1 km, suficiente a la escala de 10 km de IMERG), con
    cache de proceso: el orquestador pide 3 ventanas por corrida y las
    comunas no cambian entre llamadas.
    """
    global _comuna_cache
    if _comuna_cache is not None:
        return _comuna_cache

    import json

    from sqlalchemy import text

    from db import get_engine

    config = dict(load_config())
    config["DB_USER"] = config["DB_APP_USER"]
    config["DB_PASSWORD"] = config["DB_APP_PASSWORD"]
    with get_engine(config) as engine:
        with engine.connect() as conn:
            records = conn.execute(text("""
                select c.comuna_id,
                       ST_AsGeoJSON(ST_SimplifyPreserveTopology(c.geometria, 0.01)) as geojson
                from dpa_limites.dpa_comuna_subdere c
                join dpa_limites.dpa_provincia_subdere p on p.id_provincia = c.id_provincia
                where p.id_region = ANY(:region_ids)
            """), {"region_ids": list(CHOROPLETH_REGION_IDS)}).fetchall()
    _comuna_cache = [(comuna_id, json.loads(geojson)) for comuna_id, geojson in records]
    return _comuna_cache


def fetch_choropleth(start: datetime, end: datetime, bbox: tuple,
                     variable: str = "imerg_precipitation") -> list[dict]:
    """
    Suma la precipitacion diaria IMERG Early de [start, end] pixel a
    pixel y la agrega por comuna con una mascara de geometria
    (rasterio.features.geometry_mask, all_touched para que las comunas
    chicas capturen al menos el pixel que tocan), sin Earth Engine.
    Devuelve una fila por comuna para frontal_sur.choropleth_stats,
    con agg "sum" (suma de los valores de pixel dentro de la comuna,
    misma semantica que tenia el reducer de GEE). Si la ventana no
    tiene ningun dia publicado todavia (latencia ~1 dia del diario
    Early), devuelve lista vacia.

    valid_time se redondea a la hora en punto: la UNIQUE de
    choropleth_stats incluye valid_time y asi un reintento del mismo
    cron upsertea en vez de duplicar.
    """
    session = _session(load_config())
    daily_rows = _fetch_daily(session, start, end, bbox)
    if not daily_rows:
        return []

    accumulated = None
    transform = None
    for row in daily_rows:
        with rasterio.open(row["file_path"]) as src:
            band = src.read(1)
            transform = src.transform
        band = np.nan_to_num(band, nan=0.0)
        accumulated = band if accumulated is None else accumulated + band

    valid_time = end.replace(minute=0, second=0, microsecond=0)
    rows = []
    for comuna_id, geometry in _comuna_geometries():
        mask = geometry_mask([geometry], out_shape=accumulated.shape,
                             transform=transform, invert=True, all_touched=True)
        value = float(accumulated[mask].sum()) if mask.any() else 0.0
        rows.append({
            "comuna_id": int(comuna_id),
            "variable": variable,
            "agg": "sum",
            "value": value,
            "valid_time": valid_time,
        })
    return rows
