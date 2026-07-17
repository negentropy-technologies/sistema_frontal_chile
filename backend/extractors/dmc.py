"""
Extractor de la DMC (Direccion Meteorologica de Chile, red EMA) para
el tablon frontal_sur.dmc_datos: una fila por (estacion, momento) con
una columna por variable del endpoint, apuntando a
dmc_stations.id (ema_id) como en la arquitectura legada del usuario.
La geometria vive solo en el catalogo dmc_stations, no se repite por
observacion.

Usa el servicio getDatosRecientesEma/{codigo}/{anio}/{mes}: la
granularidad de consulta es mensual, asi que para una ventana
[start, end] se piden los meses que la tocan y se filtra por momento
al final.

Resiliencia y cortesia con el servidor de la DMC:
- Sesion HTTP compartida con reintentos/backoff sobre 429/5xx.
- Pausa fija entre llamadas (PACING_SECONDS) para no saturar la API.
- El servicio puede responder 200 con un "mensaje" de bloqueo en vez
  de datos (verificado en vivo con una key sin el servicio
  habilitado); en ese caso se lanza RuntimeError para que el
  orquestador lo registre como error visible, no como 0 filas.
- Las estaciones se leen del engine que entrega el orquestador (UN
  solo tunel SSH por corrida): abrir un segundo tunel anidado aqui
  colgaba el cierre de sshtunnel hasta 17 minutos, comprobado
  empiricamente el 2026-07-17.
"""

import re
import time
from datetime import datetime, timezone

from extractors._http import build_session
from logutil import log

DATA_URL = "https://climatologia.meteochile.gob.cl/application/servicios/getDatosRecientesEma"

# Campo del endpoint -> columna del tablon frontal_sur.dmc_datos.
# Explicito (no una conversion automatica camelCase a snake_case) para
# que el esquema de la tabla y este mapeo no puedan divergir en
# silencio; un campo nuevo del endpoint simplemente se ignora hasta
# agregarlo aqui y en una migracion.
COLUMNAS = {
    "temperatura": "temperatura",
    "temperatura02Mts": "temperatura_02_mts",
    "temperatura10Mts": "temperatura_10_mts",
    "temperatura30Mts": "temperatura_30_mts",
    "puntoDeRocio": "punto_de_rocio",
    "temperaturaMinima12Horas": "temperatura_minima_12_horas",
    "temperaturaMaxima12Horas": "temperatura_maxima_12_horas",
    "humedadRelativa": "humedad_relativa",
    "radiacionGlobalInst": "radiacion_global_inst",
    "presionEstacion": "presion_estacion",
    "presionNivelDelMar": "presion_nivel_del_mar",
    "presionNivelEstandar": "presion_nivel_estandar",
    "aguaCaidaDelMinuto": "agua_caida_del_minuto",
    "aguaCaida6Horas": "agua_caida_6_horas",
    "aguaCaida24Horas": "agua_caida_24_horas",
    "direccionDelViento": "direccion_del_viento",
    "fuerzaDelViento": "fuerza_del_viento",
    "direccionDelVientoPromedio2Minutos": "direccion_del_viento_promedio_2_minutos",
    "fuerzaDelVientoPromedio2Minutos": "fuerza_del_viento_promedio_2_minutos",
    "direccionDelVientoPromedio10Minutos": "direccion_del_viento_promedio_10_minutos",
    "fuerzaDelVientoPromedio10Minutos": "fuerza_del_viento_promedio_10_minutos",
    "direccionDelViento02MinutosMax": "direccion_del_viento_02_minutos_max",
    "fuerzaDelViento02MinutosMax": "fuerza_del_viento_02_minutos_max",
    "direccionDelViento10MinutosMax": "direccion_del_viento_10_minutos_max",
    "fuerzaDelViento10MinutosMax": "fuerza_del_viento_10_minutos_max",
}

# Pausa entre llamadas a la API: cortesia con un servicio publico que
# ya bloquea usuarios por abuso.
PACING_SECONDS = 0.3

_NUMERIC_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _numeric(raw) -> float | None:
    """
    Extrae el numero inicial de un valor de la DMC, que puede venir
    como None, texto vacio, numero puro o "valor unidad" (ej
    "9.8 grados C", "158.900 Watt/m2"). Devuelve None si no hay numero
    parseable; la unidad no se guarda porque en el tablon cada columna
    tiene unidad fija conocida.
    """
    if raw is None:
        return None
    match = _NUMERIC_RE.match(str(raw).strip())
    return float(match.group(0)) if match else None


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


def _stations_in_bbox(engine, bbox: tuple) -> list[tuple[int, str]]:
    """
    Lee (id, cod_estacion) de las estaciones del catalogo dentro del
    bbox, usando el engine YA abierto por el orquestador: nunca abrir
    un tunel SSH propio aqui (ver docstring del modulo).
    """
    from sqlalchemy import text

    xmin, ymin, xmax, ymax = bbox
    with engine.connect() as conn:
        records = conn.execute(text("""
            select id, cod_estacion
            from frontal_sur.dmc_stations
            where geometria && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)
            order by id
        """), {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}).fetchall()
    return [(row[0], row[1]) for row in records]


def _month_payload(session, config: dict, cod_estacion: str, year: int, month: int) -> dict | None:
    """
    Pide un mes de datos de una estacion. Devuelve el JSON, None si el
    servicio dice que no hay informacion para ese mes, o lanza
    RuntimeError si la respuesta trae un mensaje de bloqueo (key sin
    el servicio habilitado): ese caso debe ser visible como error, no
    parecer un mes sin datos.
    """
    response = session.get(
        f"{DATA_URL}/{cod_estacion}/{year}/{month:02d}",
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


def fetch_batches(start: datetime, end: datetime, bbox: tuple, engine):
    """
    Generador: entrega las filas del tablon dmc_datos de UNA estacion
    por iteracion. El orquestador upsertea cada lote apenas sale, asi
    el pico de memoria es el de una estacion y no el de la ventana
    completa, y una corrida interrumpida deja persistidas las
    estaciones ya procesadas. Cada llamada a la API queda logueada:
    nada de silencios largos en el log del pipeline.
    """
    from db import load_config

    stations = _stations_in_bbox(engine, bbox)
    log(f"dmc: {len(stations)} estaciones del catalogo dentro del bbox")
    if not stations:
        return

    config = load_config()
    session = build_session()
    months = _months(start, end)

    for ema_id, cod_estacion in stations:
        rows = []
        for year, month in months:
            payload = _month_payload(session, config, cod_estacion, year, month)
            time.sleep(PACING_SECONDS)
            if payload is None:
                log(f"dmc {cod_estacion} {year}-{month:02d}: sin informacion")
                continue
            datos = (payload.get("datosEstaciones") or {})
            registros = datos.get("datos", []) if isinstance(datos, dict) else []
            month_rows = 0
            for registro in registros:
                momento = registro.get("momento")
                if not momento:
                    continue
                valid_time = datetime.fromisoformat(momento).replace(tzinfo=timezone.utc)
                if not (start <= valid_time <= end):
                    continue
                # Todas las columnas del tablon van SIEMPRE presentes
                # (None si el campo no vino): el upsert arma el SQL
                # con las claves de la primera fila, asi que cada fila
                # debe tener el mismo set de columnas.
                row = {"ema_id": ema_id, "momento": valid_time}
                for field, column in COLUMNAS.items():
                    row[column] = _numeric(registro.get(field))
                rows.append(row)
                month_rows += 1
            log(f"dmc {cod_estacion} {year}-{month:02d}: {month_rows} registros en la ventana")
        if rows:
            yield rows


def fetch(start: datetime, end: datetime, bbox: tuple, engine) -> list[dict]:
    """
    Version lista-completa de fetch_batches, para usos puntuales o
    interactivos. El pipeline usa fetch_batches directamente para no
    acumular toda la ventana en memoria.
    """
    rows = []
    for batch in fetch_batches(start, end, bbox, engine):
        rows += batch
    return rows