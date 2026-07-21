"""
Extractor del portal satelital de la DGA (dgasat, snia.mop.gob.cl)
para el tablon frontal_sur.dga_datos: una fila por (estacion, momento)
con una columna por parametro instantaneo (caudal, niveles, tiempo
atmosferico, nieve), apuntando a dga_stations.id (estacion_id). Mismo
patron que DMC y Agromet. Cubre TODOS los tipos de estaciones que
dgasat publica (fluviometricas, meteorologicas, pozos, embalses,
nivometricas): la lista de estaciones del formulario es la fuente de
verdad de cuales estan telemetrizadas.

Flujo del portal (ingenieria inversa verificada en vivo el
2026-07-17; no hay API documentada):
 1. GET  dgasat_param.jsp?param=1        -> crea la sesion (JSESSIONID;
    sin la cookie los POST devuelven Error 500 NullPointerException) y
    trae el <select> con las ~1770 estaciones telemetrizadas.
 2. POST dgasat_param_1.jsp accion=refresca estacion1=<cod_bna>
    -> devuelve los parametros disponibles de la estacion como
    checkboxes name="parametros" value="<cod>_<id>_<etiqueta>".
 3. POST dgasat_param_tablas.jsp con esos "parametros" y el rango de
    fechas -> 302 a dgasat_param_tablas_instantaneos.jsp?pag=1, tabla
    HTML paginada (input hidden "totalpag" trae el total de paginas).

Resiliencia y cortesia:
- El WAF exige user-agent de navegador (403 con uno de libreria);
  pacing entre requests.
- El limite "no mas de 6 parametros" es solo un alert del JS; el
  servidor acepta mas (verificado en vivo con 8).
- La pagina declara reCAPTCHA pero solo deshabilita botones en el
  cliente; el servidor no lo exige (verificado en vivo).
- Momentos: el portal entrega hora local de Chile (America/Santiago);
  se convierten a UTC como en el resto del sistema.
- Estaciones desde el engine del orquestador (un solo tunel SSH),
  cruzadas con las opciones del formulario para no gastar requests en
  las ~1800 estaciones del bbox sin telemetria.
"""

import queue
import re
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from db import ids_con_datos
from extractors._http import build_session
from logutil import log

BASE = "https://snia.mop.gob.cl/dgasat/pages/dgasat_param"
PARAM_URL = f"{BASE}/dgasat_param.jsp?param=1"
REFRESCA_URL = f"{BASE}/dgasat_param_1.jsp"
TABLAS_URL = f"{BASE}/dgasat_param_tablas.jsp"
PAGINA_URL = f"{BASE}/dgasat_param_tablas_instantaneos.jsp"

# Mismo user-agent de navegador que agromet: el WAF del MOP tambien
# responde 403 a los user-agents de libreria (verificado en vivo).
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Id de parametro dgasat -> columna del tablon (el id es el segundo
# campo del value del checkbox, ej "08317001-8_12_Caudal (m3/seg)").
# El test de consistencia compara estas columnas contra la migracion
# 0004. Ids vistos y descartados a proposito: 21 (temp del snow
# pillow, sensor interno), 71/112/125/126 (canales duplicados de
# precipitacion via Tx y disdrometro), 109 (calidad de senal).
PARAMETROS = {
    "1": "nivel_agua",
    "2": "temperatura_agua",
    "3": "precipitacion_acumulada",
    "5": "temperatura_aire",
    "7": "humedad",
    "9": "radiacion_solar",
    "12": "caudal",
    "13": "precipitacion_instantanea",
    "65": "nivel_pozo",
    "66": "direccion_del_viento",
    "67": "presion_atmosferica",
    "72": "altura_nieve",
    "82": "velocidad_del_viento",
    "93": "nivel_embalse",
    "111": "equivalente_agua_nieve",
}

# Fragmento de la etiqueta del <th> de la tabla -> columna, para
# mapear las columnas de valores por su encabezado y no por posicion
# (el encabezado concatena nombre de estacion truncado + etiqueta,
# ej "RIO BIOBIO ECaudal (m3/seg)"). Fragmentos ASCII para que el
# encoding windows-1252 del portal no afecte el match; "Acum" e
# "Instant" van con mayuscula para no calzar con los canales
# "Pp. acum."/"Pp. instan." del disdrometro, que no se piden.
ETIQUETAS = (
    ("Caudal", "caudal"),
    ("Nivel de Agua", "nivel_agua"),
    ("Nivel de Pozo", "nivel_pozo"),
    ("Nivel de embalse", "nivel_embalse"),
    ("Temp.del Agua", "temperatura_agua"),
    ("Temp.del Aire", "temperatura_aire"),
    ("Humedad", "humedad"),
    ("Rad.Solar", "radiacion_solar"),
    ("Direccion del Viento", "direccion_del_viento"),
    ("Velocidad del Vto", "velocidad_del_viento"),
    ("Atmosf", "presion_atmosferica"),
    ("Altura de Nieve", "altura_nieve"),
    ("Equiv. en agua", "equivalente_agua_nieve"),
    ("Acum", "precipitacion_acumulada"),
    ("Instant", "precipitacion_instantanea"),
)

PACING_SECONDS = 1.0

# El portal JSP demora hasta 31s en primer byte incluso con poca carga
# (medido en vivo el 2026-07-18); con 60s la corrida de la manana agoto
# los 3 reintentos de build_session sin recibir respuesta.
TIMEOUT_SECONDS = 180

TZ_CHILE = ZoneInfo("America/Santiago")
TZ_UTC = ZoneInfo("UTC")

_TH_RE = re.compile(r"<th[^>]*>(.*?)</th>", re.S)
_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAGS_RE = re.compile(r"<[^>]+>")
_TOTALPAG_RE = re.compile(r'name="totalpag"\s+value="(\d+)"')
_CHECKBOX_RE = re.compile(r'name="parametros"\s+value="([^"]+)"')
_OPTION_RE = re.compile(r"<option value=([0-9]{8}-[0-9K])\s*>")


def _texto(celda: str) -> str:
    """
    Limpia una celda HTML: quita tags, entidades &nbsp; y colapsa el
    whitespace (los <th> traen saltos de linea dentro de la etiqueta).
    """
    plano = _TAGS_RE.sub(" ", celda).replace("&nbsp;", " ")
    return re.sub(r"\s+", " ", plano).strip()


def _columnas(html: str) -> list[str | None]:
    """
    Devuelve, en orden, la columna del tablon que corresponde a cada
    <th> de valores de la tabla (los que siguen a "Fecha-Hora de
    Medicion"), o None si el encabezado no calza con ninguna etiqueta
    conocida (esa columna se ignora).
    """
    headers = [_texto(h) for h in _TH_RE.findall(html)]
    try:
        inicio = next(i for i, h in enumerate(headers) if "Fecha-Hora" in h) + 1
    except StopIteration:
        return []
    columnas = []
    for header in headers[inicio:]:
        columna = next((col for frag, col in ETIQUETAS if frag in header), None)
        columnas.append(columna)
    return columnas


def _parse_pagina(html: str, start: datetime, end: datetime) -> list[dict]:
    """
    Parsea una pagina del informe de valores instantaneos: una fila
    por momento con TODAS las columnas de PARAMETROS presentes (None
    si el parametro no vino: el upsert exige columnas uniformes),
    filtrada a [start, end] y con momento en UTC.
    """
    columnas = _columnas(html)
    if not columnas:
        return []
    rows = []
    for tr in _TR_RE.findall(html):
        celdas = [_texto(c) for c in _TD_RE.findall(tr)]
        # Fila de datos: [nro, fecha-hora, valor, valor, ...]; las
        # filas de botones y titulos no calzan con el patron de fecha.
        if len(celdas) < 3 or not re.fullmatch(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}", celdas[1]):
            continue
        momento_local = datetime.strptime(celdas[1], "%d/%m/%Y %H:%M").replace(tzinfo=TZ_CHILE)
        momento = momento_local.astimezone(TZ_UTC)
        if not (start <= momento <= end):
            continue
        row = {"momento": momento}
        for columna in PARAMETROS.values():
            row[columna] = None
        for columna, valor in zip(columnas, celdas[2:]):
            if columna is None:
                continue
            try:
                row[columna] = float(valor)
            except ValueError:
                row[columna] = None
        if all(row[columna] is None for columna in PARAMETROS.values()):
            continue
        rows.append(row)
    return rows


def _parse_parametros(html: str) -> list[str]:
    """
    Extrae de la respuesta del refresca los values de los checkboxes
    de parametros de la estacion, filtrados a los ids de PARAMETROS.
    """
    valores = []
    for value in _CHECKBOX_RE.findall(html):
        partes = value.split("_")
        if len(partes) >= 3 and partes[1] in PARAMETROS:
            valores.append(value)
    return valores


def _stations_in_bbox(engine, bbox: tuple) -> list[tuple[int, str]]:
    """
    Lee (id, cod_bna) de todas las estaciones del catalogo dentro del
    bbox (el cruce con las telemetrizadas de dgasat lo hace el caller
    con las opciones del formulario), con el engine YA abierto por el
    orquestador (un solo tunel SSH por corrida).
    """
    from sqlalchemy import text

    xmin, ymin, xmax, ymax = bbox
    with engine.connect() as conn:
        records = conn.execute(text("""
            select id, cod_bna
            from frontal_sur.dga_stations
            where geometria && ST_MakeEnvelope(:xmin, :ymin, :xmax, :ymax, 4326)
            order by id
        """), {"xmin": xmin, "ymin": ymin, "xmax": xmax, "ymax": ymax}).fetchall()
    return [(row[0], row[1]) for row in records]


def _skip_to_resume(stations: list[tuple[int, str]], resume_after: str | None) -> list[tuple[int, str]]:
    """
    Recorta el catalogo a lo que falta por procesar cuando una corrida
    se retoma tras un corte (--resume-after <cod_bna>). Si el codigo no
    calza con ninguna estacion del catalogo (por ejemplo cambio el
    bbox entre corridas), se corre la ventana completa en vez de
    fallar: es mas seguro reprocesar de mas, idempotente via upsert,
    que saltarse estaciones por error.
    """
    if resume_after is None:
        return stations
    for i, (_, cod) in enumerate(stations):
        if cod == resume_after:
            return stations[i + 1:]
    log(f"dga: resume_after {resume_after!r} no encontrado en el catalogo, se corre completo")
    return stations


def _fetch_station(session: requests.Session, cod_bna: str, fecha_ini: str, fecha_fin: str,
                    start: datetime, end: datetime) -> tuple[list[dict] | None, bool]:
    """
    Scrapea una estacion (parametros + tabla paginada) con la sesion
    dada. Devuelve (rows, fallo): rows es None si la estacion no
    publica parametros instantaneos (no es una falla, se omite) o si
    hubo un error de red (fallo=True); si rows es una lista
    (posiblemente vacia), la estacion se proceso con exito. Aislado en
    su propia funcion para poder correrlo tanto secuencial (una sola
    sesion, ver _fetch_batches_sequential) como en paralelo (una
    sesion por worker, ver _fetch_batches_parallel): cada estacion va
    en su propio try/except porque el portal se pone lento bajo carga
    y un timeout puntual no debe botar la fuente completa.
    """
    try:
        refresca = session.post(REFRESCA_URL, data={
            "accion": "refresca", "param": "1", "tipo": "ANO", "hora_fin": "0",
            "estacion1": cod_bna, "estacion2": "-1", "estacion3": "-1",
            "UserID": "nobody", "EsDL1": "", "EsDL2": "", "EsDL3": "",
        }, timeout=TIMEOUT_SECONDS)
        time.sleep(PACING_SECONDS)
        refresca.raise_for_status()
        parametros = _parse_parametros(refresca.text)
        if not parametros:
            log(f"dga {cod_bna}: sin parametros instantaneos publicados, se omite")
            return None, False

        tabla = session.post(TABLAS_URL, data={
            "accion": "refresca", "param": "1", "tipo": "ANO", "hora_fin": "0",
            "tiporep": "I", "period": "rango",
            "fechaInicioTabla": fecha_ini, "fechaFinTabla": fecha_fin,
            "fechaFinGrafico": fecha_fin,
            "estacion1": cod_bna, "estacion2": "-1", "estacion3": "-1",
            "parametros": parametros, "UserID": "nobody",
        }, timeout=TIMEOUT_SECONDS)
        time.sleep(PACING_SECONDS)
        tabla.raise_for_status()
        rows = _parse_pagina(tabla.text, start, end)

        match = _TOTALPAG_RE.search(tabla.text)
        totalpag = int(match.group(1)) if match else 1
        for pag in range(2, totalpag + 1):
            pagina = session.get(f"{PAGINA_URL}?pag={pag}", timeout=TIMEOUT_SECONDS)
            time.sleep(PACING_SECONDS)
            pagina.raise_for_status()
            rows += _parse_pagina(pagina.text, start, end)
    except requests.RequestException as exc:
        log(f"dga {cod_bna}: fallo de red, se omite -> {exc}")
        return None, True

    log(f"dga {cod_bna}: {len(rows)} registros en la ventana ({totalpag} paginas)")
    return rows, False


def _fetch_batches_sequential(stations: list[tuple[int, str]], session: requests.Session,
                               fecha_ini: str, fecha_fin: str, start: datetime, end: datetime):
    fallidas = 0
    for estacion_id, cod_bna in stations:
        rows, fallo = _fetch_station(session, cod_bna, fecha_ini, fecha_fin, start, end)
        if fallo:
            fallidas += 1
            continue
        if rows:
            for row in rows:
                row["estacion_id"] = estacion_id
            yield rows
    if fallidas:
        log(f"dga: {fallidas} estaciones omitidas por fallos de red en esta corrida")


def _fetch_batches_parallel(stations: list[tuple[int, str]], fecha_ini: str, fecha_fin: str,
                             start: datetime, end: datetime, workers: int):
    """
    Version con varios workers de _fetch_batches_sequential: verificado
    en vivo el 2026-07-19 que 10 sesiones concurrentes contra dgasat
    devuelven 500 Internal Server Error en el 100% de los casos (el
    backend del portal no tolera esa carga), pero 4 sesiones
    concurrentes corrieron limpias (0 fallos, ~4-8x mas rapido que
    secuencial). Cada worker abre su PROPIA sesion (su propio
    JSESSIONID): el flujo del portal deja estado ligado a la sesion
    (que estacion quedo "seleccionada" via accion=refresca), asi que
    compartir una sesion entre threads corromperia ese estado.

    Reparto por cola compartida (no particion estatica) porque el
    tiempo por estacion varia bastante (5-9 paginas): asi ningun
    worker queda ocioso mientras otro todavia tiene trabajo largo.
    """
    work_q: queue.Queue = queue.Queue()
    for item in stations:
        work_q.put(item)
    out_q: queue.Queue = queue.Queue()

    def worker() -> None:
        # Cada worker necesita su PROPIO JSESSIONID (ver docstring del
        # modulo: sin este GET previo, los POST de refresca devuelven
        # una respuesta sin parametros -- confirmado en vivo el
        # 2026-07-19 al omitir este paso por error en la primera
        # version de esta funcion).
        session = build_session()
        session.headers["User-Agent"] = BROWSER_UA
        try:
            portada = session.get(PARAM_URL, timeout=TIMEOUT_SECONDS)
            time.sleep(PACING_SECONDS)
            portada.raise_for_status()
        except requests.RequestException as exc:
            # ponytail: si ESTE worker no logra abrir sesion, se retira
            # y deja sus items en work_q para que los otros workers los
            # tomen; si TODOS fallan aca el consumidor se queda
            # esperando para siempre (no hay reintento de sesion). No
            # se vio en las pruebas en vivo; subir un pool con retry si
            # llega a pasar.
            log(f"dga: worker no pudo abrir sesion -> {exc}")
            return
        while True:
            try:
                estacion_id, cod_bna = work_q.get_nowait()
            except queue.Empty:
                return
            rows, fallo = _fetch_station(session, cod_bna, fecha_ini, fecha_fin, start, end)
            out_q.put((estacion_id, rows, fallo))

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()

    fallidas = 0
    for _ in range(len(stations)):
        estacion_id, rows, fallo = out_q.get()
        if fallo:
            fallidas += 1
            continue
        if rows:
            for row in rows:
                row["estacion_id"] = estacion_id
            yield rows

    for t in threads:
        t.join()
    if fallidas:
        log(f"dga: {fallidas} estaciones omitidas por fallos de red en esta corrida")


def fetch_batches(start: datetime, end: datetime, bbox: tuple, engine, resume_after: str | None = None,
                   workers: int = 1):
    """
    Generador: entrega las filas del tablon dga_datos de UNA estacion
    por iteracion, para que el orquestador upsertee lote a lote (pico
    de memoria constante, corridas interrumpidas dejan lo procesado
    persistido). El rango se pide en fechas locales de Chile porque
    asi lo espera el portal; el recorte fino a [start, end] se hace al
    parsear.

    resume_after: cod_bna de la ultima estacion confirmada upserteada
    en una corrida previa de la MISMA ventana (ver log del pipeline);
    salta el catalogo hasta despues de esa estacion. Opcional: aunque
    no se pase, el catalogo igual se filtra automaticamente contra
    dga_datos (ver ids_con_datos en db.py) para saltarse las
    estaciones que YA tienen datos en esta ventana exacta, esten donde
    esten en el catalogo (no solo un prefijo contiguo) -- asi una
    corrida cortada a mitad de camino por varios workers concurrentes
    (que no escriben en orden de catalogo) no obliga a re-scrapear todo
    lo que ya quedo bien.

    workers: cantidad de estaciones scrapeadas en simultaneo (default
    1 = secuencial, comportamiento identico al de antes). Con 4 se
    verifico en vivo que dgasat responde limpio; con 10 el portal
    empieza a devolver 500 en el 100% de los casos, asi que no subir
    de 4 sin volver a probar en vivo primero.
    """
    stations = _stations_in_bbox(engine, bbox)
    log(f"dga: {len(stations)} estaciones del catalogo dentro del bbox")
    if not stations:
        return

    session = build_session()
    session.headers["User-Agent"] = BROWSER_UA

    # Paso 1: sesion JSP + lista de estaciones telemetrizadas.
    # El portal declara el charset correcto en cada respuesta (mezcla
    # windows-1252 en las paginas y UTF-8 en el refresca), asi que se
    # deja que requests decodifique por el header Content-Type; los
    # fragmentos de ETIQUETAS son ASCII y no dependen del charset.
    portada = session.get(PARAM_URL, timeout=TIMEOUT_SECONDS)
    time.sleep(PACING_SECONDS)
    portada.raise_for_status()
    telemetrizadas = set(_OPTION_RE.findall(portada.text))
    stations = [(sid, cod) for sid, cod in stations if cod in telemetrizadas]
    log(f"dga: {len(stations)} de esas estaciones publican en dgasat")

    stations = _skip_to_resume(stations, resume_after)
    if resume_after is not None:
        log(f"dga: retomando despues de {resume_after}, {len(stations)} estaciones restantes")

    ya_con_datos = ids_con_datos(engine, "frontal_sur.dga_datos", "estacion_id",
                                  [sid for sid, _ in stations], start, end)
    if ya_con_datos:
        antes = len(stations)
        stations = [(sid, cod) for sid, cod in stations if sid not in ya_con_datos]
        log(f"dga: {antes - len(stations)} de {antes} estaciones ya tienen datos en esta ventana, se omiten")

    fecha_ini = start.astimezone(TZ_CHILE).strftime("%d/%m/%Y")
    fecha_fin = end.astimezone(TZ_CHILE).strftime("%d/%m/%Y")

    if workers <= 1:
        yield from _fetch_batches_sequential(stations, session, fecha_ini, fecha_fin, start, end)
    else:
        log(f"dga: corriendo con {workers} workers concurrentes")
        yield from _fetch_batches_parallel(stations, fecha_ini, fecha_fin, start, end, workers)


def fetch(start: datetime, end: datetime, bbox: tuple, engine) -> list[dict]:
    """
    Version lista-completa de fetch_batches, para usos puntuales.
    """
    rows = []
    for batch in fetch_batches(start, end, bbox, engine):
        rows += batch
    return rows
