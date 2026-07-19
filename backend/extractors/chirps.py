"""
Extractor de CHIRPS v3.0 (Climate Hazards Center, UCSB) para
frontal_sur.frames_raster: precipitacion diaria estimada por satelite
mas estaciones, ~5 km de resolucion.

Por que el servidor del CHC y no GEE: se verifico en vivo el
2026-07-19 que Google Earth Engine SI tiene CHIRPS v3
(UCSB-CHC/CHIRPS/V3/DAILY_SAT, 1998-presente) -- corrige lo que se
habia verificado en vivo el 2026-07-17 (en ese entonces GEE solo
listaba v2.0). Aun asi se prefiere data.chc.ucsb.edu, la distribucion
oficial del propio productor del dato, sobre GEE como intermediario:
politica del proyecto de priorizar la API directa del emisor salvo que
no alcance en cobertura o resolucion (ver extractors/gee.py). Se usa
la rama daily/prelim/sat (preliminar, ~6-7 dias de latencia) porque la
rama final tarda meses en completarse para el evento en curso (para
climatologia HISTORICA si se usa la rama final, ver
extractors/chirps_climatology.py); el upsert por (source, variable,
region, valid_time) permite reemplazar un dia preliminar por el final
mas adelante sin duplicar.

La descarga y el recorte por rango HTTP (vsicurl, solo la ventana del
bbox, no el GeoTIFF global de 7200x2400 pixeles) viven en
extractors/_chc.py, compartido con chirps_climatology.py: mismo
servidor y mismo formato de archivo para ambas ramas.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

from extractors._chc import crop_day, day_url
from extractors._http import build_session
from extractors._raster import save_png_overlay
from logutil import log

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "chirps"


def _day_url(day: datetime) -> str:
    return day_url("prelim/sat", "prelim", day.year, day.month, day.day)


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
            # real de red (lo maneja el retry de crop_day).
            if session.head(url, timeout=20).status_code == 404:
                log(f"chirps {day:%Y-%m-%d}: aun no publicado por el CHC, se salta")
                day += timedelta(days=1)
                continue
            crop_day(url, bbox, tif_path)
            log(f"chirps {day:%Y-%m-%d}: recortado por rango HTTP")
        else:
            log(f"chirps {day:%Y-%m-%d}: ya existia en disco")
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
