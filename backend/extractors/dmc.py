"""
Extractor de la DMC (Direccion Meteorologica de Chile, red EMA) para
frontal_sur.station_obs: temperatura, presion, viento y precipitacion
por estacion, en formato largo, dentro del bbox del proyecto.

Usa el servicio getDatosRecientesEma/{codigo}/{anio}/{mes} (el mismo
del script ETL legado del usuario): la granularidad de consulta es
mensual, asi que para una ventana [start, end] se piden los meses que
la tocan y se filtra por momento al final. Las estaciones y sus
geometrias salen de frontal_sur.dmc_stations (pobladas antes con
backend/dmc_stations.py), no de la API, para no repetir el catalogo en
cada corrida.

Resiliencia y cortesia con el servidor de la DMC:
- Sesion HTTP compartida con reintentos/backoff sobre 429/5xx
  (extractors/_http.py).
- Pausa fija entre llamadas (PACING_SECONDS) para no saturar la API
  con decenas de estaciones seguidas.
- El servicio puede responder 200 con un "mensaje" de bloqueo en vez
  de datos (verificado en vivo: "Este servicio ha sido bloqueda para
  el usuario que consulta" con una key sin ese servicio habilitado).
  En ese caso se lanza RuntimeError con el mensaje tal cual, para que
  el orquestador lo registre en ingest_runs como error visible en vez
  de una lista vacia silenciosa.
"""

import re
import time
from datetime import datetime, timezone

from db import get_engine, load_config
from extractors._http import build_session

DATA_URL = "https://climatologia.meteochile.gob.cl/application/servicios/getDatosRecientesEma"

# Se extraen TODOS los campos climaticos que entrega el endpoint (26
# verificados en vivo el 2026-07-17: temperaturas a distintas alturas,
# punto de rocio, min/max 12h, humedad, radiacion, 3 presiones, agua
# caida minuto/6h/24h, y viento instantaneo/promedios/maximas), en vez
# de una lista fija: si la DMC agrega un campo nuevo, entra solo. El
# unico campo que no es una variable es el timestamp.
NON_VARIABLE_FIELDS = {"momento"}

# Pausa entre llamadas a la API. Con ~70 estaciones en el bbox y 1-2
# meses por ventana tipica, agrega menos de un minuto por corrida y
# evita rafagas contra un servicio publico que ya bloquea usuarios.
PACING_SECONDS = 0.3

# ponytail: parser numerico por regex, igual que el script legado; los
# valores vienen como "12.3" o "158.900 Watt/m2" y solo interesa el
# numero inicial.
_NUMERIC_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _numeric_with_unit(raw) -> tuple[float, str | None] | None:
    """
    Separa un valor de la DMC en (numero, unidad): "9.8 °C" ->
    (9.8, "°C"), "94 %" -> (94.0, "%"), "12.5" -> (12.5, None).
    Devuelve None si el valor viene vacio o sin numero parseable
    (los campos None del endpoint caen aqui y se descartan).
    """
    if raw is None:
        return None
    texto = str(raw).strip()
    match = _NUMERIC_RE.match(texto)
    if not match:
        return None
    unit = texto[match.end():].strip() or None
    return float(match.group(0)), unit


def _months(start: datetime, end: datetime) -> list[tuple[int, int]]:
    """
    Lista los pares (anio, mes) que tocan la ventana [start, end], en
    orden. La API de la DMC solo consulta por mes completo, asi que
    esta es la unidad minima de descarga.
    """
    months = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def _stations_in_bbox(bbox: tuple) -> list[dict]:
    """
    Lee de frontal_sur.dmc_stations (rol frontal_sur_app) las
    estaciones dentro del bbox, con su geometria como WKT listo para
    las filas de station_obs.
    """
    from sqlalchemy import text

    xmin, ymin, xmax, ymax = bbox
    config = dict(load_config())
    config["DB_USER"] = config["DB_APP_USER"]
    config["DB_PASSWORD"] = config["DB_APP_PASSWORD"]
    with get_engine(config) as engine:
        with engine.connect() as conn:
            records = conn.execute(text("""
                select cod_estacion, nombre, ST_X(geometria), ST_Y(geometria)
                from frontal_sur.dmc_stations
                where geometria && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)
                order by cod_estacion
            """), {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}).fetchall()
    return [
        {
            "station_id": station_id,
            "station_name": station_name,
            "geometria": f"POINT({lon} {lat})",
        }
        for station_id, station_name, lon, lat in records
    ]


def _month_payload(session, config: dict, station_id: str, year: int, month: int) -> dict | None:
    """
    Pide un mes de datos de una estacion. Devuelve el JSON, None si el
    servicio dice que no hay informacion para ese mes, o lanza
    RuntimeError si la respuesta trae un mensaje de bloqueo (key sin
    el servicio habilitado): ese caso debe ser visible como error, no
    parecer un mes sin datos.
    """
    response = session.get(
        f"{DATA_URL}/{station_id}/{year}/{month:02d}",
        params={"usuario": config["CORREO"], "token": config["API_KEY"]},
        timeout=30,
    )
    response.raise_for_status()
    if response.text.strip().startswith("Sin Informaci"):
        return None
    payload = response.json()
    mensaje = payload.get("mensaje") or ""
    if "bloquead" in mensaje.lower():
        raise RuntimeError(f"servicio DMC bloqueado para esta key: {mensaje}")
    return payload


def fetch(start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Devuelve una fila por (estacion, variable, momento) con datos no
    nulos dentro de [start, end] y el bbox, en formato largo para
    frontal_sur.station_obs. Los momentos vienen en UTC segun la
    propia API (campo timezone del catalogo).
    """
    stations = _stations_in_bbox(bbox)
    if not stations:
        return []

    config = load_config()
    session = build_session()
    months = _months(start, end)
    rows = []

    for station in stations:
        for year, month in months:
            payload = _month_payload(session, config, station["station_id"], year, month)
            time.sleep(PACING_SECONDS)
            if payload is None:
                continue
            datos = (payload.get("datosEstaciones") or {})
            registros = datos.get("datos", []) if isinstance(datos, dict) else []
            for registro in registros:
                momento = registro.get("momento")
                if not momento:
                    continue
                valid_time = datetime.fromisoformat(momento).replace(tzinfo=timezone.utc)
                if not (start <= valid_time <= end):
                    continue
                for field, raw in registro.items():
                    if field in NON_VARIABLE_FIELDS:
                        continue
                    parsed = _numeric_with_unit(raw)
                    if parsed is None:
                        continue
                    value, unit = parsed
                    rows.append({
                        "source": "dmc",
                        "station_id": station["station_id"],
                        "station_name": station["station_name"],
                        "valid_time": valid_time,
                        # El nombre de variable es el campo del
                        # endpoint tal cual (aguaCaida24Horas, etc.),
                        # sin renombrar: fidelidad 1:1 con la fuente.
                        "variable": field,
                        "value": value,
                        "unit": unit,
                        "geometria": station["geometria"],
                    })
    return rows
