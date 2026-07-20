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

import queue
import re
import threading
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


def _skip_to_resume(stations: list[tuple[int, str]], resume_after: str | None) -> list[tuple[int, str]]:
    """
    Recorta el catalogo a lo que falta por procesar cuando una corrida
    se retoma tras un corte (--resume-after <cod_estacion>). Si el
    codigo no calza con ninguna estacion del catalogo se corre la
    ventana completa en vez de fallar: es mas seguro reprocesar de
    mas, idempotente via upsert, que saltarse estaciones por error.
    """
    if resume_after is None:
        return stations
    for i, (_, cod) in enumerate(stations):
        if cod == resume_after:
            return stations[i + 1:]
    log(f"agromet: resume_after {resume_after!r} no encontrado en el catalogo, se corre completo")
    return stations


def _new_session():
    """
    Sesion con el user-agent de navegador y verificacion TLS desactivada
    que requiere agromet.cl (ver docstring del modulo). Separada para
    que cada worker de _fetch_batches_parallel abra la suya propia.
    """
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    session = build_session()
    session.headers["User-Agent"] = BROWSER_UA
    session.verify = False
    return session


def _fetch_station(session, cod_estacion: str, date_from: str, date_to: str,
                    start: datetime, end: datetime) -> list[dict]:
    """
    Pide y parsea la ventana de una estacion. Aislado en su propia
    funcion para correr tanto secuencial (una sola sesion) como en
    paralelo (una sesion por worker, ver _fetch_batches_parallel).

    Lanza RuntimeError si el WAF bloqueo la sesion: a diferencia de un
    fallo de red puntual, un bloqueo de WAF no es un problema de esta
    estacion sino de la sesion/IP completa, asi que debe detener TODA
    la corrida (secuencial o paralela) en vez de saltarse la estacion.
    """
    url = (f"{DATA_URL}?ema_ia_id={cod_estacion}"
           f"&dateFrom={date_from}&dateTo={date_to}&portada=false")
    response = session.get(url, timeout=60)
    time.sleep(PACING_SECONDS)
    response.raise_for_status()
    if "Web Application Firewall" in response.text:
        raise RuntimeError("WAF de agromet.cl bloqueo la corrida; reintentar mas tarde con pacing mayor")
    rows = _parse_datos(response.text, start, end)
    log(f"agromet {cod_estacion}: {len(rows)} registros en la ventana")
    return rows


def _fetch_batches_sequential(stations: list[tuple[int, str]], session, date_from: str, date_to: str,
                               start: datetime, end: datetime):
    for ema_id, cod_estacion in stations:
        rows = _fetch_station(session, cod_estacion, date_from, date_to, start, end)
        if rows:
            for row in rows:
                row["ema_id"] = ema_id
            yield rows


def _fetch_batches_parallel(stations: list[tuple[int, str]], date_from: str, date_to: str,
                             start: datetime, end: datetime, workers: int):
    """
    Version con varios workers de _fetch_batches_sequential (mismo
    patron que extractors/dga.py::_fetch_batches_parallel). Verificado
    en vivo el 2026-07-19 que 4 sesiones concurrentes contra
    agromet.cl no disparan el WAF (0 bloqueos). Cada worker abre su
    propia sesion; si un worker recibe el bloqueo del WAF, se detienen
    todos los demas (stop.set()) en vez de seguir mandando trafico
    contra un WAF ya activo.
    """
    work_q: queue.Queue = queue.Queue()
    for item in stations:
        work_q.put(item)
    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def worker() -> None:
        session = _new_session()
        while not stop.is_set():
            try:
                ema_id, cod_estacion = work_q.get_nowait()
            except queue.Empty:
                return
            try:
                rows = _fetch_station(session, cod_estacion, date_from, date_to, start, end)
                out_q.put((ema_id, rows, None))
            except RuntimeError as exc:
                stop.set()
                out_q.put((ema_id, None, exc))
                return

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()

    recibidos = 0
    error_final = None
    while recibidos < len(stations):
        ema_id, rows, error = out_q.get()
        recibidos += 1
        if error is not None:
            error_final = error
            break
        if rows:
            for row in rows:
                row["ema_id"] = ema_id
            yield rows

    for t in threads:
        t.join()
    if error_final is not None:
        raise error_final


def fetch_batches(start: datetime, end: datetime, bbox: tuple, engine, resume_after: str | None = None,
                   workers: int = 1):
    """
    Generador: entrega las filas del tablon agromet_datos de UNA
    estacion por iteracion, para que el orquestador upsertee lote a
    lote (pico de memoria constante, corridas interrumpidas dejan lo
    procesado persistido). La ventana se pide en hora local de Chile
    porque asi la espera el endpoint.

    resume_after: cod_estacion de la ultima estacion confirmada
    upserteada en una corrida previa de la MISMA ventana; salta el
    catalogo hasta despues de esa estacion.

    workers: cantidad de estaciones scrapeadas en simultaneo (default
    1 = secuencial, comportamiento identico al de antes). Verificado en
    vivo el 2026-07-19 que 4 workers responden limpio sin disparar el
    WAF (mismo techo que dga, ver MAX_SOURCE_WORKERS en ingest.py).
    """
    stations = _stations_in_bbox(engine, bbox)
    log(f"agromet: {len(stations)} estaciones del catalogo dentro del bbox")
    if not stations:
        return
    stations = _skip_to_resume(stations, resume_after)
    if resume_after is not None:
        log(f"agromet: retomando despues de {resume_after}, {len(stations)} estaciones restantes")

    # La API construye su grilla horaria arrastrando los minutos y
    # segundos del request (pedir dateFrom=..:42:24 genera slots a las
    # :00:24 que no calzan con los datos y vienen vacios, verificado
    # en vivo): la ventana se alinea a horas exactas.
    start = start.replace(minute=0, second=0, microsecond=0)
    end = end.replace(minute=0, second=0, microsecond=0)
    date_from = start.astimezone(TZ_CHILE).strftime("%Y-%m-%d+%H:%M:%S")
    date_to = end.astimezone(TZ_CHILE).strftime("%Y-%m-%d+%H:%M:%S")

    if workers <= 1:
        session = _new_session()
        yield from _fetch_batches_sequential(stations, session, date_from, date_to, start, end)
    else:
        log(f"agromet: corriendo con {workers} workers concurrentes")
        yield from _fetch_batches_parallel(stations, date_from, date_to, start, end, workers)


def fetch(start: datetime, end: datetime, bbox: tuple, engine) -> list[dict]:
    """
    Version lista-completa de fetch_batches, para usos puntuales.
    """
    rows = []
    for batch in fetch_batches(start, end, bbox, engine):
        rows += batch
    return rows
