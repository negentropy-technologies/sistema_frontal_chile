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

import queue
import re
import threading
import time
from datetime import datetime, timezone

from db import ids_con_datos
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
    log(f"dmc: resume_after {resume_after!r} no encontrado en el catalogo, se corre completo")
    return stations


def _fetch_station(session, config: dict, cod_estacion: str, months: list[tuple[int, int]],
                    start: datetime, end: datetime) -> list[dict]:
    """
    Pide y parsea todos los meses de una estacion. Aislado en su propia
    funcion para correr tanto secuencial (una sola sesion) como en
    paralelo (una sesion por worker, ver _fetch_batches_parallel).

    Lanza RuntimeError si el servicio reporta la key bloqueada (ver
    _month_payload): a diferencia de un fallo de red puntual, esto no
    es un problema de esta estacion sino de la key completa, asi que
    debe detener TODA la corrida (secuencial o paralela), no solo
    saltarse la estacion.
    """
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
            # Todas las columnas del tablon van SIEMPRE presentes (None
            # si el campo no vino): el upsert arma el SQL con las
            # claves de la primera fila, asi que cada fila debe tener
            # el mismo set de columnas.
            row = {"momento": valid_time}
            for field, column in COLUMNAS.items():
                row[column] = _numeric(registro.get(field))
            rows.append(row)
            month_rows += 1
        log(f"dmc {cod_estacion} {year}-{month:02d}: {month_rows} registros en la ventana")
    return rows


def _fetch_batches_sequential(stations: list[tuple[int, str]], session, config: dict,
                               months: list[tuple[int, int]], start: datetime, end: datetime):
    for ema_id, cod_estacion in stations:
        rows = _fetch_station(session, config, cod_estacion, months, start, end)
        if rows:
            for row in rows:
                row["ema_id"] = ema_id
            yield rows


def _fetch_batches_parallel(stations: list[tuple[int, str]], config: dict,
                             months: list[tuple[int, int]], start: datetime, end: datetime, workers: int):
    """
    Version con varios workers de _fetch_batches_sequential (mismo
    patron que extractors/dga.py::_fetch_batches_parallel). Verificado
    en vivo el 2026-07-19 que 4 sesiones concurrentes contra
    climatologia.meteochile.gob.cl no disparan el mensaje de key
    bloqueada. Cada worker abre su propia sesion; si un worker recibe
    el bloqueo de key, se detienen todos los demas (stop.set()) en vez
    de seguir gastando cupo contra un servicio ya bloqueado.
    """
    work_q: queue.Queue = queue.Queue()
    for item in stations:
        work_q.put(item)
    out_q: queue.Queue = queue.Queue()
    stop = threading.Event()

    def worker() -> None:
        session = build_session()
        while not stop.is_set():
            try:
                ema_id, cod_estacion = work_q.get_nowait()
            except queue.Empty:
                return
            try:
                rows = _fetch_station(session, config, cod_estacion, months, start, end)
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
    Generador: entrega las filas del tablon dmc_datos de UNA estacion
    por iteracion. El orquestador upsertea cada lote apenas sale, asi
    el pico de memoria es el de una estacion y no el de la ventana
    completa, y una corrida interrumpida deja persistidas las
    estaciones ya procesadas. Cada llamada a la API queda logueada:
    nada de silencios largos en el log del pipeline.

    resume_after: cod_estacion de la ultima estacion confirmada
    upserteada en una corrida previa de la MISMA ventana; salta el
    catalogo hasta despues de esa estacion. Opcional: el catalogo
    tambien se filtra siempre contra dmc_datos (ver ids_con_datos en
    db.py) para saltarse automaticamente las estaciones que ya tienen
    datos en esta ventana exacta, esten donde esten en el catalogo
    (ver dga.py para el detalle de por que esto hace falta con
    workers concurrentes).

    workers: cantidad de estaciones scrapeadas en simultaneo (default
    1 = secuencial, comportamiento identico al de antes). Verificado en
    vivo el 2026-07-19 que 4 workers responden limpio (mismo techo que
    dga, ver MAX_SOURCE_WORKERS en ingest.py).
    """
    from db import load_config

    stations = _stations_in_bbox(engine, bbox)
    log(f"dmc: {len(stations)} estaciones del catalogo dentro del bbox")
    if not stations:
        return
    stations = _skip_to_resume(stations, resume_after)
    if resume_after is not None:
        log(f"dmc: retomando despues de {resume_after}, {len(stations)} estaciones restantes")

    ya_con_datos = ids_con_datos(engine, "frontal_sur.dmc_datos", "ema_id",
                                  [sid for sid, _ in stations], start, end)
    if ya_con_datos:
        antes = len(stations)
        stations = [(sid, cod) for sid, cod in stations if sid not in ya_con_datos]
        log(f"dmc: {antes - len(stations)} de {antes} estaciones ya tienen datos en esta ventana, se omiten")

    config = load_config()
    months = _months(start, end)

    if workers <= 1:
        session = build_session()
        yield from _fetch_batches_sequential(stations, session, config, months, start, end)
    else:
        log(f"dmc: corriendo con {workers} workers concurrentes")
        yield from _fetch_batches_parallel(stations, config, months, start, end, workers)


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