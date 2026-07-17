"""
Extractor de NOAA NCEI (Access Data Service, dataset daily-summaries)
para frontal_sur.station_obs. Fuente de contexto historico (no near
real time: las estaciones chilenas de GHCND llegan con meses de
retraso, ver nota de latencia en el spec general): temperatura
maxima/minima, precipitacion y viento por estacion, dentro del bbox
de la macrozona centro-sur.

La descarga es en dos pasos porque el endpoint de datos
(access/services/data/v1) ya no acepta el parametro "bbox" (devuelve
400 "A station is required", verificado en vivo el 2026-07-16 incluso
con el ejemplo de la propia documentacion): primero se descubren las
estaciones dentro del bbox con el Search Service
(access/services/search/v1), y luego se piden los datos diarios de
esas estaciones al endpoint de datos.

Documentacion oficial:
https://www.ncei.noaa.gov/support/access-data-service-api-user-documentation
https://www.ncei.noaa.gov/support/access-search-service-api-user-documentation
"""

from datetime import datetime, timezone

import requests

from extractors._http import build_session

NOAA_DATA_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
NOAA_SEARCH_URL = "https://www.ncei.noaa.gov/access/services/search/v1/data"

# Variables (dataTypes de GHCND) que cubren temperatura, precipitacion
# y viento. NCEI daily-summaries no incluye presion atmosferica (es un
# dataset diario derivado de GHCND, no de observaciones horarias), por
# eso no se pide aqui.
DATA_TYPES = ("TMAX", "TMIN", "PRCP", "AWND")


def _station_ids(session: requests.Session, start: datetime, end: datetime, bbox: tuple) -> list[str]:
    """
    Descubre via el Search Service los ids de estacion GHCND con datos
    dentro de "bbox" y la ventana [start, end]. El parametro bbox del
    Search Service espera North,West,South,East, por eso se reordena
    desde el (xmin, ymin, xmax, ymax) del proyecto.
    """
    xmin, ymin, xmax, ymax = bbox
    params = {
        "dataset": "daily-summaries",
        "bbox": f"{ymax},{xmin},{ymin},{xmax}",
        "startDate": start.strftime("%Y-%m-%dT00:00:00"),
        "endDate": end.strftime("%Y-%m-%dT23:59:59"),
        # ponytail: limite fijo de 1000 resultados sin paginar; el bbox
        # del proyecto tiene decenas de estaciones GHCND, no miles. Si
        # algun dia count > 1000, paginar con offset.
        "limit": 1000,
    }
    response = session.get(NOAA_SEARCH_URL, params=params, timeout=30)
    response.raise_for_status()
    payload = response.json()

    ids = []
    for result in payload.get("results", []):
        for station in result.get("stations", []):
            if station["id"] not in ids:
                ids.append(station["id"])
    return ids


def fetch(start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Pide a NCEI todas las estaciones con datos diarios dentro de
    "bbox" entre "start" y "end", y devuelve una fila por
    (estacion, variable, dia) con datos no nulos, en formato largo
    para frontal_sur.station_obs. Si el Search Service no encuentra
    estaciones con datos en la ventana (normal en ventanas cortas por
    la latencia de GHCND), devuelve lista vacia sin llamar al endpoint
    de datos.
    """
    session = build_session()
    stations = _station_ids(session, start, end, bbox)
    if not stations:
        return []

    params = {
        "dataset": "daily-summaries",
        "stations": ",".join(stations),
        "startDate": start.strftime("%Y-%m-%d"),
        "endDate": end.strftime("%Y-%m-%d"),
        "dataTypes": ",".join(DATA_TYPES),
        "format": "json",
        "units": "metric",
        "includeStationName": "true",
        "includeStationLocation": "true",
    }
    response = session.get(NOAA_DATA_URL, params=params, timeout=60)
    response.raise_for_status()
    records = response.json()

    rows = []
    for record in records:
        # Registros sin coordenadas (raro, pero posible en datasets
        # historicos) se descartan: sin geom no se puede insertar en
        # station_obs (columna NOT NULL).
        if "LATITUDE" not in record or "LONGITUDE" not in record:
            continue
        latitude = float(record["LATITUDE"])
        longitude = float(record["LONGITUDE"])
        station_id = record["STATION"]
        station_name = record.get("NAME")
        # Fecha diaria sin hora: se ancla a medianoche UTC explicita
        # porque station_obs.valid_time es TIMESTAMPTZ y un datetime
        # naive dependeria de la zona horaria de la sesion de Postgres.
        valid_time = datetime.strptime(record["DATE"], "%Y-%m-%d").replace(tzinfo=timezone.utc)

        for variable in DATA_TYPES:
            raw_value = record.get(variable)
            if raw_value in (None, ""):
                continue
            rows.append({
                "source": "noaa_ncei",
                "station_id": station_id,
                "station_name": station_name,
                "geom": f"POINT({longitude} {latitude})",
                "valid_time": valid_time,
                "variable": variable,
                "value": float(raw_value),
                "unit": "metric",
            })
    return rows