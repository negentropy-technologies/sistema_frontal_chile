"""
Extractor de la Red Agroclimatica Nacional (agromet.cl, INIA/Minagri)
para el tablon frontal_sur.agromet_datos: una fila por (estacion,
momento) con una columna por variable, apuntando a
agromet_stations.id (ema_id). Mismo patron que la DMC.

Endpoint (descubierto del paquete R agrometR de ODES-Chile y
verificado en vivo el 2026-07-17):
    GET https://www.agromet.cl/ext/aux/getGraphData.php
        ?ema_ia_id=<id>&dateFrom=<Y-m-d+H:M:S>&dateTo=<...>&portada=false
Respuesta XML: elementos <dato> con pares id|valor separados por
pipes (ids de variable segun el mapeo CAMPOS de este modulo), y
elementos <gra> con las etiquetas.

Resiliencia y cortesia:
- El WAF de agromet.cl bloquea user-agents de herramienta y rafagas
  (comprobado en vivo: 403 con curl pelado y bloqueo temporal tras
  sondeos seguidos): se usa user-agent de navegador y pacing de 1 s
  entre estaciones.
- Momentos: la API entrega hora local de Chile (America/Santiago);
  se convierten a UTC con zoneinfo para que el tablon quede en la
  misma referencia temporal que el resto del sistema.
- Estaciones desde el engine del orquestador (un solo tunel SSH).
"""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from extractors._http import build_session
from logutil import log

DATA_URL = "https://www.agromet.cl/ext/aux/getGraphData.php"

# User-agent de navegador: el WAF de agromet.cl responde 403 a los
# user-agents de libreria (verificado en vivo).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Id de campo del XML -> columna del tablon (verificado en vivo el
# 2026-07-17 contra las etiquetas <gra> del endpoint; el test de
# consistencia compara las columnas contra la migracion 0003).
CAMPOS = {
    "1": "temperatura",
    "2": "precipitacion_horaria",
    "3": "humedad_relativa",
    "4": "presion_estacion",
    "5": "radiacion_solar_max",
    "6": "velocidad_maxima_viento",
    "7": "temperatura_minima",
    "8": "temperatura_maxima",
    "9": "direccion_del_viento",
    "11": "grados_dia_base10",
    "12": "horas_frio_base7",
}

PACING_SECONDS = 1.0

TZ_CHILE = ZoneInfo("America/Santiago")

_DATO_RE = re.compile(r"<dato>(.*?)</dato>", re.S)


def _parse_datos(xml: str, start: datetime, end: datetime) -> list[dict]:
    """
    Parsea los elementos <dato> del XML: cada uno es
    "fecha|<Y-m-d H:M:S>|id|valor|id|valor|...". Devuelve una fila por
    momento con las columnas normalizadas de CAMPOS (siempre todas
    presentes, None si el campo vino vacio: el upsert exige columnas
    uniformes), filtrada a [start, end] y con momento en UTC.
    """
    rows = []
    for dato in _DATO_RE.findall(xml):
        parts = dato.split("|")
        if len(parts) < 2 or parts[0] != "fecha":
            continue
        momento_local = datetime.strptime(parts[1], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CHILE)
        momento = momento_local.astimezone(ZoneInfo("UTC"))
        if not (start <= momento <= end):
            continue
        valores = {}
        for i in range(2, len(parts) - 1, 2):
            valores[parts[i]] = parts[i + 1]
        row = {"momento": momento}
        for campo, columna in CAMPOS.items():
            raw = valores.get(campo, "")
            try:
                row[columna] = float(raw)
            except ValueError:
                row[columna] = None
        # El catalogo (2022) incluye estaciones dadas de baja que
        # responden la ventana completa con campos vacios: una fila
        # sin ningun valor no aporta y solo engorda el tablon.
        if all(row[columna] is None for columna in CAMPOS.values()):
            continue
        rows.append(row)
    return rows


def _stations_in_bbox(engine, bbox: tuple) -> list[tuple[int, str]]:
    """
    Lee (id, cod_estacion) del catalogo dentro del bbox, con el
    engine YA abierto por el orquestador (un solo tunel SSH por
    corrida, nunca uno anidado aqui).
    """
    from sqlalchemy import text

    xmin, ymin, xmax, ymax = bbox
    with engine.connect() as conn:
        records = conn.execute(text("""
            select id, cod_estacion
            from frontal_sur.agromet_stations
            where geometria && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)
            order by id
        """), {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}).fetchall()
    return [(row[0], row[1]) for row in records]


def fetch_batches(start: datetime, end: datetime, bbox: tuple, engine):
    """
    Generador: entrega las filas del tablon agromet_datos de UNA
    estacion por iteracion, para que el orquestador upsertee lote a
    lote (pico de memoria constante, corridas interrumpidas dejan lo
    procesado persistido). La ventana se pide en hora local de Chile
    porque asi la espera el endpoint.
    """
    stations = _stations_in_bbox(engine, bbox)
    log(f"agromet: {len(stations)} estaciones del catalogo dentro del bbox")
    if not stations:
        return

    # La API construye su grilla horaria arrastrando los minutos y
    # segundos del request (pedir dateFrom=..:42:24 genera slots a las
    # :00:24 que no calzan con los datos y vienen vacios, verificado
    # en vivo): la ventana se alinea a horas exactas.
    start = start.replace(minute=0, second=0, microsecond=0)
    end = end.replace(minute=0, second=0, microsecond=0)

    session = build_session()
    session.headers["User-Agent"] = BROWSER_UA
    # agromet.cl sirve una cadena de certificados incompleta (falla
    # CERTIFICATE_VERIFY_FAILED con verificacion estricta); el propio
    # paquete R agrometR desactiva la verificacion (ssl_verifypeer =
    # FALSE). Se replica esa decision: son datos publicos de solo
    # lectura y el riesgo se limita a esta sesion.
    session.verify = False
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    date_from = start.astimezone(TZ_CHILE).strftime("%Y-%m-%d+%H:%M:%S")
    date_to = end.astimezone(TZ_CHILE).strftime("%Y-%m-%d+%H:%M:%S")

    for ema_id, cod_estacion in stations:
        url = (f"{DATA_URL}?ema_ia_id={cod_estacion}"
               f"&dateFrom={date_from}&dateTo={date_to}&portada=false")
        response = session.get(url, timeout=60)
        time.sleep(PACING_SECONDS)
        response.raise_for_status()
        if "Web Application Firewall" in response.text:
            # Bloqueo del WAF: error visible en ingest_runs, no un
            # silencio con 0 filas.
            raise RuntimeError("WAF de agromet.cl bloqueo la corrida; reintentar mas tarde con pacing mayor")
        rows = _parse_datos(response.text, start, end)
        log(f"agromet {cod_estacion}: {len(rows)} registros en la ventana")
        if rows:
            for row in rows:
                row["ema_id"] = ema_id
            yield rows


def fetch(start: datetime, end: datetime, bbox: tuple, engine) -> list[dict]:
    """
    Version lista-completa de fetch_batches, para usos puntuales.
    """
    rows = []
    for batch in fetch_batches(start, end, bbox, engine):
        rows += batch
    return rows
