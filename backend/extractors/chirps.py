"""
Extractor de CHIRPS v3.0 (Climate Hazards Center, UCSB) para
frontal_sur.frames_raster: precipitacion diaria estimada por satelite
mas estaciones, ~5 km de resolucion.

Por que el servidor del CHC y no una API intermedia: CHIRPS v3 no
esta en el catalogo de Google Earth Engine (solo v2.0, con ~3 semanas
de latencia) ni en ClimateSERV (tambien v2), verificado en vivo el
2026-07-17; data.chc.ucsb.edu es la distribucion oficial del propio
productor del dato. Se usa la rama daily/prelim/sat (preliminar, ~6-7
dias de latencia) porque la rama final tarda meses; el upsert por
(source, variable, region, valid_time) permite reemplazar un dia
preliminar por el final mas adelante sin duplicar.

Eficiencia: los GeoTIFF diarios son globales (7200x2400 pixeles), pero
rasterio/GDAL leen por rango HTTP (vsicurl), asi que solo se descarga
la ventana del bbox (~240x310 pixeles), no el archivo completo.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds

from extractors._http import build_session
from extractors._raster import save_png_overlay
from extractors._retry import retry

CHC_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/prelim/sat"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "chirps"


def _day_url(day: datetime) -> str:
    return f"{CHC_BASE_URL}/{day.year}/chirps-v3.0.prelim.{day.strftime('%Y.%m.%d')}.tif"


@retry(times=3, backoff_seconds=2.0, exceptions=(rasterio.errors.RasterioIOError,))
def _crop_day(url: str, bbox: tuple, dest_path: Path) -> None:
    """
    Lee del GeoTIFF global remoto solo la ventana del bbox (lectura
    por rango HTTP via GDAL/vsicurl) y la guarda como GeoTIFF local.
    El nodata de CHIRPS (-9999) se convierte a NaN para que el overlay
    PNG normalice solo valores reales.
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


def fetch(start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Descarga (recortado al bbox) cada dia CHIRPS prelim disponible en
    [start, end] y devuelve una fila por dia para
    frontal_sur.frames_raster. Los dias que el CHC todavia no publica
    (latencia ~1 semana) simplemente se saltan: las corridas
    siguientes del cron los recogen cuando aparecen. Los archivos ya
    descargados no se vuelven a bajar.
    """
    session = build_session()
    rows = []
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= end:
        url = _day_url(day)
        stamp = day.strftime("%Y%m%dT000000")
        tif_path = DATA_DIR / "chirps_precipitation" / f"{stamp}.tif"
        png_path = DATA_DIR / "chirps_precipitation" / f"{stamp}.png"

        if not tif_path.exists():
            # HEAD barato antes de abrir con GDAL: distingue "dia aun
            # no publicado" (404, se salta sin reintentos) de un error
            # real de red (lo maneja el retry de _crop_day).
            if session.head(url, timeout=20).status_code == 404:
                day += timedelta(days=1)
                continue
            _crop_day(url, bbox, tif_path)
        if not png_path.exists():
            save_png_overlay(tif_path, png_path)

        rows.append({
            "source": "chirps",
            "variable": "chirps_precipitation",
            "region": "centro_sur",
            "valid_time": day,
            "bbox": list(bbox),
            "file_path": str(tif_path),
            "png_overlay_path": str(png_path),
            "created_at": datetime.now(timezone.utc),
        })
        day += timedelta(days=1)
    return rows
