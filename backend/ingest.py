"""
Orquestador de ingesta: corre los extractores de GEE, CHIRPS, DGA, DMC
y Agromet, y escribe los resultados en frontal_sur via upsert, con el
rol acotado frontal_sur_app (nunca el superusuario). Cada fuente corre
en su propio try/except: si una falla, se registra el error en
frontal_sur.ingest_runs y las demas siguen corriendo.

DMC y Google Flood Hub quedan fuera de este orquestador (ver plan de
implementacion, seccion "Flood Hub, diferido a un plan separado"):
climatologia.meteochile.gob.cl no responde desde este entorno de
desarrollo, y Flood Hub requiere aprobacion de un waitlist de Google
que todavia no se tiene.

Uso:
    .venv/bin/python backend/ingest.py --days 7
    .venv/bin/python backend/ingest.py --days 1 --dry-run
"""

import argparse
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import GeneratorType

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text

from db import app_role_config, get_engine, upsert
from extractors import agromet, anomaly_raster, chirps, chirps_climatology, dem, dga, dmc, gee, nasa_imerg
from extractors._raster import save_png_overlay
from logutil import log

# Unica fuente de verdad geografica del proyecto: Coquimbo a
# Magallanes (ampliado el 2026-07-18; el bbox anterior Metropolitana a
# Los Lagos dejaba los sistemas frontales cortados en los bordes), mas
# TODOS los espacios marinos de dpa_limites.espacio_marino_chile
# recortados a esa banda de latitud. Limites medidos en la BD el
# 2026-07-18 con ST_Extent y redondeados con margen chico: norte
# Coquimbo lat -29.037; sur Magallanes lat -56.538; este el mas
# oriental entre Magallanes (lon -66.416) y la Zona Contigua en el
# lado atlantico austral (lon -65.770); el oeste lo sigue fijando el
# Mar Presencial (lon -118.0 medido el 2026-07-17; la ZEE sola llegaba
# a -84.8).
REGION_BBOX = (-118.5, -57.0, -65.5, -28.5)

MIN_DAYS = 1
MAX_DAYS = 90

# Techo de workers concurrentes para dga/dmc/agromet (ver
# extractors/dga.py, dmc.py, agromet.py): verificado en vivo el
# 2026-07-19 que dgasat tolera 4 sesiones simultaneas limpio pero con
# 10 devuelve 500 en el 100% de los casos; el mismo dia se probaron 4
# sesiones concurrentes contra climatologia.meteochile.gob.cl (dmc) y
# agromet.cl (agromet), ambas sin bloqueo. No subir sin volver a
# probar en vivo primero.
MAX_SOURCE_WORKERS = 4

# Fuentes de scraping estacion-por-estacion (ver ids_con_datos en
# db.py y el filtro automatico en cada fetch_batches): ya se saltan
# solo las estaciones con datos, asi que el upsert de estas tres no
# necesita ademas actualizar filas existentes. insert-only (DO
# NOTHING en vez de DO UPDATE) es mas rapido y no le toca nada a lo
# que ya quedo escrito.
FUENTES_INSERT_ONLY = {"dga", "dmc", "agromet"}
# Ventana por defecto del pipeline cuando no se pasa --days ni
# --start/--end. Unico lugar donde vive ese numero: todo lo demas se
# deriva de la ventana [start, end] que arma main().
DEFAULT_DAYS = 7

# Ventanas de acumulacion de precipitacion por comuna definidas en la
# tabla de productos del spec general (coropletas 24h/72h/7d). Son
# fijas e independientes de --days: --days controla cuanta historia de
# frames se descarga, no que acumulados muestra el mapa.
CHOROPLETH_WINDOWS = (
    (1, "imerg_precipitation_24h"),
    (3, "imerg_precipitation_72h"),
    (7, "imerg_precipitation_7d"),
)


def _fetch_choropleth_windows(start: datetime, end: datetime, engine) -> list[dict]:
    """
    Corre la agregacion por comuna una vez por cada ventana de
    CHOROPLETH_WINDOWS, siempre ancladas a "end" (el presente de la
    corrida). "start" se ignora deliberadamente: los acumulados del
    mapa son fijos aunque el backfill de frames use otra ventana.
    """
    rows = []
    for days, variable in CHOROPLETH_WINDOWS:
        rows += nasa_imerg.fetch_choropleth(end - timedelta(days=days), end, REGION_BBOX,
                                            variable=variable, engine=engine)
    return rows


# Precipitacion diaria por estacion en la ventana, una fila por
# (dia, fuente): SUM para dga/agromet y MAX para dmc (agua_caida_24_horas
# ya es un acumulado de 24h, tomar el ultimo/mayor valor del dia lo
# aproxima sin duplicar). Se arma con un UNION ALL de tres CTEs en vez
# de un JOIN entre las tres tablas: no comparten cadencia horaria ni
# existe una clave natural para cruzarlas fila a fila, un JOIN ahi
# produciria un producto cruzado erroneo. Costo: un aggregate scan por
# tabla acotado por el rango [:start, :end] usando el indice
# *_datos_momento_idx ya existente, no un full scan.
#
# dga_diario usa precipitacion_instantanea (parametro 13 de dgasat), NO
# precipitacion_acumulada (parametro 3): verificado en vivo el
# 2026-07-19 que "acumulada" es un contador que nunca se reinicia (una
# estacion se mantuvo en 410.5 mm constante durante 5 dias seguidos,
# y siguio subiendo despues sin volver a cero), asi que sumarlo por
# dia multiplicaba el valor real por la cantidad de lecturas del dia
# (48 lecturas de 410.5 mm sumaron 19704 mm, un outlier que disparo el
# IDW de anomaly.py a valores de miles de mm). "instantanea" si es el
# incremento real por lectura: su SUM diario coincide con el delta real
# del contador acumulado entre dos dias consecutivos (25.7 mm vs 25.7 mm
# verificado el 07-16, 55.5 mm vs 55.2 mm el 07-17).
ANOMALIA_ESTACIONES_SQL = """
WITH dga_diario AS (
    SELECT s.geometria AS geom, d.momento::date AS dia,
           SUM(d.precipitacion_instantanea) AS valor
    FROM frontal_sur.dga_datos d
    JOIN frontal_sur.dga_stations s ON s.id = d.estacion_id
    WHERE d.momento BETWEEN :start AND :end AND d.precipitacion_instantanea IS NOT NULL
    GROUP BY s.geometria, d.momento::date
),
dmc_diario AS (
    SELECT s.geometria AS geom, d.momento::date AS dia,
           MAX(d.agua_caida_24_horas) AS valor
    FROM frontal_sur.dmc_datos d
    JOIN frontal_sur.dmc_stations s ON s.id = d.ema_id
    WHERE d.momento BETWEEN :start AND :end AND d.agua_caida_24_horas IS NOT NULL
    GROUP BY s.geometria, d.momento::date
),
agromet_diario AS (
    SELECT s.geometria AS geom, d.momento::date AS dia,
           SUM(d.precipitacion_horaria) AS valor
    FROM frontal_sur.agromet_datos d
    JOIN frontal_sur.agromet_stations s ON s.id = d.ema_id
    WHERE d.momento BETWEEN :start AND :end AND d.precipitacion_horaria IS NOT NULL
    GROUP BY s.geometria, d.momento::date
)
SELECT dia, ST_X(geom) AS lon, ST_Y(geom) AS lat, valor FROM dga_diario
UNION ALL
SELECT dia, ST_X(geom) AS lon, ST_Y(geom) AS lat, valor FROM dmc_diario
UNION ALL
SELECT dia, ST_X(geom) AS lon, ST_Y(geom) AS lat, valor FROM agromet_diario
ORDER BY dia
"""

ANOMALY_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "frames" / "chirps_anomaly"


def _fetch_anomaly(start: datetime, end: datetime, engine) -> list[dict]:
    """
    Anomalia DIARIA de precipitacion: el residuo estacion-CHIRPS se
    ajusta a resolucion de BLOQUE (Ossa-Moreno et al. 2019, Eq. 9,
    bloques de a lo mas ~31 dias rodantes desde "start", ver
    anomaly_raster.bucket_dias) pero el resultado se desagrega a un
    mapa por dia segun la forma real de CHIRPS dentro del bloque
    (anomaly.py::compute_anomaly_windowed), con umbral de lluvia
    espuria en pixeles secos y, si el DEM esta disponible, un ajuste
    liviano por gradiente altitudinal (elevation_trend: CHIRPS
    subestima precipitacion orografica y el IDW puro no tiene nocion
    de altura, ver extractors/anomaly_raster.py). La climatologia se
    compara dia exacto contra dia exacto (no agregada).

    Se salta un bloque si no hay ninguna estacion con dato en esos
    dias, o si ningun dia del bloque tiene CHIRPS prelim en disco
    todavia. Dentro de un bloque, se salta un dia puntual si su CHIRPS
    prelim (recien publicado, latencia ~1 semana del CHC) o su
    climatologia no estan en disco, o si ninguna estacion cae dentro
    de la grilla de fondo. Nada de esto es un error del pipeline.

    El DEM (extractors/dem.py) se descarga UNA VEZ por corrida (cache
    indefinido en disco, no por fecha); si Earth Engine no responde la
    anomalia se sigue calculando igual, solo sin el ajuste de
    elevacion (se registra en el log, no se aborta la fuente completa
    por esto).

    La climatologia se descarga (y cachea en disco, igual que el resto
    de los extractores) la primera vez que se pide un dia-del-anio: en
    la corrida donde esto se activa por primera vez puede bajar hasta
    28 anios de historia por cada dia del evento, una descarga grande
    mordida por rango HTTP (no el archivo global completo); si se
    corta a mitad de camino, la corrida siguiente retoma sola porque
    los recortes ya bajados no se vuelven a pedir.
    """
    with engine.connect() as conn:
        filas = conn.execute(text(ANOMALIA_ESTACIONES_SQL), {"start": start, "end": end}).fetchall()

    valores_por_dia = defaultdict(dict)  # date -> {(lon, lat): [valores diarios]}
    for dia, lon, lat, valor in filas:
        valores_por_dia[dia].setdefault((lon, lat), []).append(valor)

    if not valores_por_dia:
        log("chirps_anomaly: sin estaciones con dato en la ventana, se salta")
        return []

    try:
        dem_path = dem.fetch_elevation(REGION_BBOX)
    except Exception as exc:
        log(f"chirps_anomaly: DEM no disponible ({exc}), se sigue sin ajuste de elevacion")
        dem_path = None

    rows = []
    for bucket in anomaly_raster.bucket_dias(start, end):
        etiqueta = f"{bucket[0]:%Y-%m-%d} a {bucket[-1]:%Y-%m-%d}"
        estaciones_bucket = defaultdict(list)  # coord -> [total diario, uno por dia del bloque]
        for dia in bucket:
            for coord, vals in valores_por_dia.get(dia.date(), {}).items():
                estaciones_bucket[coord].append(sum(vals))
        if not estaciones_bucket:
            log(f"chirps_anomaly {etiqueta}: sin estaciones con dato, se salta")
            continue

        try:
            background_window_grid, transform, profile, dias_usados = anomaly_raster.background_mensual(bucket, REGION_BBOX)
        except ValueError as exc:
            log(f"chirps_anomaly {etiqueta}: {exc}")
            continue

        coords = list(estaciones_bucket.keys())
        totales_ventana = [sum(vs) for vs in estaciones_bucket.values()]

        elevation_grid = station_elevations = None
        if dem_path is not None:
            try:
                elevation_grid = anomaly_raster.elevation_grid_for(dem_path, transform, background_window_grid.shape)
                station_elevations = [
                    anomaly_raster.sample_at_point(elevation_grid, transform, lon, lat) or 0.0
                    for lon, lat in coords
                ]
            except Exception as exc:
                log(f"chirps_anomaly {etiqueta}: elevacion no disponible ({exc}), sin ajuste de elevacion")
                elevation_grid = station_elevations = None

        for dia in dias_usados:
            diario = anomaly_raster.background_dia(dia, REGION_BBOX)
            if diario is None:
                continue
            background_daily_grid, _, _ = diario

            try:
                climatology_daily_grid, _ = anomaly_raster.climatologia_dia(dia.month, dia.day, REGION_BBOX)
            except ValueError as exc:
                log(f"chirps_anomaly {dia}: sin climatologia -> {exc}")
                continue
            if climatology_daily_grid.shape != background_window_grid.shape:
                log(f"chirps_anomaly {dia}: climatologia {climatology_daily_grid.shape} no calza con "
                    f"CHIRPS prelim {background_window_grid.shape}, se salta")
                continue

            try:
                anomaly_grid = anomaly_raster.compute_anomaly_grid_windowed(
                    coords, totales_ventana, background_window_grid, transform,
                    background_daily_grid, climatology_daily_grid,
                    station_elevations=station_elevations, elevation_grid=elevation_grid,
                )
            except ValueError as exc:
                log(f"chirps_anomaly {dia}: {exc}")
                continue

            valid_time = dia
            tif_path = ANOMALY_DATA_DIR / f"{valid_time:%Y%m%dT000000}.tif"
            png_path = ANOMALY_DATA_DIR / f"{valid_time:%Y%m%dT000000}.png"
            anomaly_raster.write_geotiff(anomaly_grid, profile, tif_path)
            save_png_overlay(tif_path, png_path)
            log(f"chirps_anomaly {dia} (bloque {etiqueta}): {len(coords)} estaciones, grilla {anomaly_grid.shape}")

            rows.append({
                "source": "chirps_anomaly",
                "variable": "chirps_precip_anomaly",
                "region": "centro_sur",
                "valid_time": valid_time,
                "bbox": list(REGION_BBOX),
                "file_path": str(tif_path),
                "png_overlay_path": str(png_path),
                "created_at": datetime.now(timezone.utc),
            })
    return rows


# Cada entrada: (nombre de la fuente, funcion fetch, tabla destino,
# columnas de conflicto, columnas de geometria). Toda funcion fetch
# recibe (start, end, engine): el engine es el UNICO tunel SSH de la
# corrida y las fuentes que leen la BD (dmc, coropletas) lo reusan en
# vez de abrir un tunel anidado, cuyo cierre colgaba sshtunnel.
# ingest_runs registra el resultado de cada fuente por separado.
SOURCES = [
    (
        "gee_goes",
        lambda start, end, engine: gee.fetch_frames(start, end, REGION_BBOX),
        "frontal_sur.frames_raster",
        ["source", "variable", "region", "valid_time"],
        None,
    ),
    (
        "nasa_imerg",
        lambda start, end, engine: nasa_imerg.fetch_frames(start, end, REGION_BBOX),
        "frontal_sur.frames_raster",
        ["source", "variable", "region", "valid_time"],
        None,
    ),
    (
        "imerg_choropleth",
        _fetch_choropleth_windows,
        "frontal_sur.choropleth_stats",
        ["comuna_id", "variable", "agg", "valid_time"],
        None,
    ),
    # NOAA NCEI se retiro del sistema el 2026-07-17: GHCND publica las
    # estaciones chilenas con ~1 anio de retraso, incompatible con
    # monitoreo near real time; el contexto de estaciones lo cubre la
    # DMC. Ver el historial de git si se retoma.
    (
        # dmc escribe el tablon ancho dmc_datos (una columna por
        # variable, FK ema_id al catalogo; la geometria no se repite
        # por fila). Entrega un generador con un lote por estacion:
        # run() upsertea cada lote apenas llega para que el pico de
        # memoria no dependa del tamano de la ventana.
        "dmc",
        lambda start, end, engine: dmc.fetch_batches(start, end, REGION_BBOX, engine),
        "frontal_sur.dmc_datos",
        ["ema_id", "momento"],
        None,
    ),
    (
        # agromet escribe su tablon agromet_datos (mismo patron que
        # dmc: generador con un lote por estacion, FK ema_id al
        # catalogo, columnas normalizadas del diccionario unificado).
        "agromet",
        lambda start, end, engine: agromet.fetch_batches(start, end, REGION_BBOX, engine),
        "frontal_sur.agromet_datos",
        ["ema_id", "momento"],
        None,
    ),
    (
        # dga escribe su tablon dga_datos (mismo patron que dmc y
        # agromet: generador con un lote por estacion, FK estacion_id
        # al catalogo dga_stations, una columna por parametro
        # instantaneo de dgasat).
        "dga",
        lambda start, end, engine: dga.fetch_batches(start, end, REGION_BBOX, engine),
        "frontal_sur.dga_datos",
        ["estacion_id", "momento"],
        None,
    ),
    (
        "chirps",
        lambda start, end, engine: chirps.fetch(start, end, REGION_BBOX),
        "frontal_sur.frames_raster",
        ["source", "variable", "region", "valid_time"],
        None,
    ),
    (
        # chirps_anomaly depende de que dga/dmc/agromet y chirps ya
        # hayan corrido en esta ventana (lee sus datos desde disco/BD,
        # no los vuelve a pedir); por eso va al final de la lista, el
        # orden en que SOURCES corre las fuentes.
        "chirps_anomaly",
        _fetch_anomaly,
        "frontal_sur.frames_raster",
        ["source", "variable", "region", "valid_time"],
        None,
    ),
]


def print_status(engine) -> None:
    """
    Reporte de solo lectura del estado de la ingesta: hasta que fecha
    hay datos en cada fuente (BD y disco) y el resultado de la ultima
    corrida registrada de cada una. Agregados baratos sobre columnas ya
    indexadas (*_datos_momento_idx, UNIQUE de frames_raster), no un
    full scan. No abre ningun tunel nuevo: usa el engine que ya paso
    main().
    """
    with engine.connect() as conn:
        print("-- estaciones (min/max momento en BD) --")
        for tabla in ("dga_datos", "dmc_datos", "agromet_datos"):
            fila = conn.execute(text(
                f"SELECT min(momento), max(momento), count(*) FROM frontal_sur.{tabla}"
            )).fetchone()
            print(f"{tabla:15s} {fila[0]} -> {fila[1]}  ({fila[2]} filas)")

        print("\n-- frames_raster (min/max valid_time por variable) --")
        filas = conn.execute(text(
            "SELECT source, variable, min(valid_time), max(valid_time), count(*) "
            "FROM frontal_sur.frames_raster GROUP BY source, variable ORDER BY source, variable"
        )).fetchall()
        for source, variable, first, last, n in filas:
            print(f"{source:15s} {variable:28s} {first} -> {last}  ({n} filas)")

        print("\n-- ultima corrida registrada por fuente --")
        filas = conn.execute(text(
            "SELECT DISTINCT ON (source) source, started_at, status, error "
            "FROM frontal_sur.ingest_runs ORDER BY source, started_at DESC"
        )).fetchall()
        for source, started_at, status, error in filas:
            linea = f"{source:15s} {status:6s} inicio={started_at}"
            if error:
                linea += f"  error={error[:80]}"
            print(linea)

    print("\n-- CHIRPS prelim en disco --")
    prelim_dir = chirps.DATA_DIR / "chirps_precipitation"
    dias = sorted(p.stem[:8] for p in prelim_dir.glob("*.tif")) if prelim_dir.exists() else []
    print(f"{len(dias)} dias" + (f", {dias[0]} -> {dias[-1]}" if dias else ""))

    print("\n-- climatologia CHIRPS cacheada --")
    clima_dir = chirps_climatology.DATA_DIR
    dias_clima = sorted(p.name for p in clima_dir.iterdir()) if clima_dir.exists() else []
    archivos_clima = len(list(clima_dir.glob("*/*.tif"))) if clima_dir.exists() else 0
    print(f"{len(dias_clima)} dias-del-anio con cache ({archivos_clima} archivos anio-dia en total)")


def parse_days(raw: str) -> int:
    """
    Valida --days como entero entre MIN_DAYS y MAX_DAYS. Es el unico
    input externo de este script (limite de confianza), por eso se
    valida antes de construir el rango de fechas: un valor absurdo
    (negativo, cero, o gigantesco) no debe llegar a los extractores.
    """
    try:
        days = int(raw)
    except ValueError:
        raise ValueError(f"--days debe ser un entero, se recibio {raw!r}")
    if not (MIN_DAYS <= days <= MAX_DAYS):
        raise ValueError(f"--days debe estar entre {MIN_DAYS} y {MAX_DAYS}, se recibio {days}")
    return days


def parse_workers(raw: str) -> int:
    """
    Valida --workers como entero entre 1 y MAX_SOURCE_WORKERS. Mismo
    criterio que parse_days: input externo, se valida antes de
    llegar a fetch_batches para no repetir por error la corrida de 10
    workers que devolvio 500 en el 100% de los casos contra dgasat.
    """
    try:
        workers = int(raw)
    except ValueError:
        raise ValueError(f"--workers debe ser un entero, se recibio {raw!r}")
    if not (1 <= workers <= MAX_SOURCE_WORKERS):
        raise ValueError(f"--workers debe estar entre 1 y {MAX_SOURCE_WORKERS}, se recibio {workers}")
    return workers


def parse_date(raw: str) -> datetime:
    """
    Valida una fecha YYYY-MM-DD de --start/--end y la devuelve anclada
    a medianoche UTC. Igual que parse_days, es input externo del
    script y se valida antes de llegar a los extractores.
    """
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise ValueError(f"la fecha debe ser YYYY-MM-DD, se recibio {raw!r}")


def run(start: datetime, end: datetime, dry_run: bool, sources: list[str] | None = None,
        resume_after: str | None = None, workers: int = 1) -> None:
    """
    Abre un unico tunel SSH (reutilizado para todas las fuentes) y
    corre cada fuente de SOURCES en su propio try/except, registrando
    el resultado en frontal_sur.ingest_runs. Una fuente que falla no
    detiene a las demas. En dry-run no se escribe nada, ni siquiera en
    ingest_runs, pero el tunel igual se abre: valida de paso que la
    conexion con el rol acotado funciona.

    resume_after solo aplica a las fuentes que scrapean estacion por
    estacion (dga, dmc, agromet: las que demostraron cortarse a mitad
    de corrida por caidas del tunel SSH o de internet, corridas de
    horas contra catalogos de cientos de estaciones). Se pasa junto
    con --sources <una de esas tres> para retomar el catalogo justo
    despues del ultimo codigo de estacion confirmado upserteado en el
    log, sin repetir el scraping ya persistido. gee_goes, nasa_imerg,
    imerg_choropleth y chirps no tienen este mecanismo: piden toda la
    ventana en una sola llamada (no hay "estacion por estacion" que
    retomar) y una corrida cortada se relanza completa.

    workers aplica a dga, dmc y agromet (las tres fuentes de scraping
    por estacion, ver fetch_batches de cada extractor): verificado en
    vivo el 2026-07-19 que 4 workers corren limpios contra las tres
    (dgasat, climatologia.meteochile.gob.cl, agromet.cl); con 10,
    dgasat devuelve 500 en el 100% de los casos (no probado a esa
    escala en dmc/agromet, no subir de 4 sin volver a probar en vivo).

    """
    config = app_role_config()

    # Con "sources" se corre solo el subconjunto pedido, para relanzar
    # fuentes puntuales (por ejemplo tras un corte de la base) sin
    # repetir las que ya terminaron ok en la misma ventana.
    seleccion = SOURCES
    if sources is not None:
        conocidas = {name for name, *_ in SOURCES}
        desconocidas = set(sources) - conocidas
        if desconocidas:
            raise ValueError(f"fuentes desconocidas: {sorted(desconocidas)}; validas: {sorted(conocidas)}")
        seleccion = [s for s in SOURCES if s[0] in sources]

    if resume_after is not None or workers != 1:
        modulo_por_fuente = {"dga": dga, "dmc": dmc, "agromet": agromet}

        def _con_resume_y_workers(name, fetch_fn):
            if name not in modulo_por_fuente:
                return fetch_fn
            mod = modulo_por_fuente[name]
            kwargs = {}
            if resume_after is not None:
                kwargs["resume_after"] = resume_after
            if workers != 1:
                kwargs["workers"] = workers
            return lambda s, e, en, mod=mod, kwargs=kwargs: mod.fetch_batches(s, e, REGION_BBOX, en, **kwargs)

        seleccion = [
            (name, _con_resume_y_workers(name, fetch_fn), table, conflict_cols, geom_cols)
            for name, fetch_fn, table, conflict_cols, geom_cols in seleccion
        ]

    with get_engine(config) as engine:
        for name, fetch_fn, table, conflict_cols, geom_cols in seleccion:
            started_at = datetime.now(timezone.utc)
            log(f"== fuente {name}: inicio (ventana {start:%Y-%m-%d %H:%M} a {end:%Y-%m-%d %H:%M} UTC)")
            try:
                result = fetch_fn(start, end, engine)
                if isinstance(result, GeneratorType):
                    # Fuente por lotes (dmc): cada lote se upsertea
                    # apenas sale del generador, para que el pico de
                    # memoria sea el de un lote y no el de la ventana.
                    count = 0
                    for batch in result:
                        if not dry_run:
                            upsert(engine, table, batch, conflict_cols, geom_cols,
                                   do_nothing=name in FUENTES_INSERT_ONLY)
                        count += len(batch)
                        log(f"{name}: lote de {len(batch)} filas" + (" (dry-run)" if dry_run else " upserteado"))
                else:
                    if not dry_run:
                        upsert(engine, table, result, conflict_cols, geom_cols,
                               do_nothing=name in FUENTES_INSERT_ONLY)
                    count = len(result)
                status = "ok"
                error = None
                log(f"== fuente {name}: fin, {count} filas" + (" (dry-run, no escrito)" if dry_run else ""))
            except Exception as exc:
                # Se guarda solo el str() de la excepcion: los
                # extractores no interpolan API keys ni passwords en
                # sus mensajes (restriccion del plan), asi que esto no
                # filtra secretos a ingest_runs ni al log del cron.
                status = "error"
                error = str(exc)
                log(f"== fuente {name}: fallo -> {error}")

            if dry_run:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(
                        "INSERT INTO frontal_sur.ingest_runs "
                        "(started_at, finished_at, source, status, error) "
                        "VALUES (:started_at, :finished_at, :source, :status, :error)"
                    ), {
                        "started_at": started_at,
                        "finished_at": datetime.now(timezone.utc),
                        "source": name,
                        "status": status,
                        "error": error,
                    })
            except Exception as exc:
                # Si la fuente ya fallo por perder la conexion (el caso
                # de esta noche: tunel SSH caido a mitad de corrida),
                # este INSERT tambien va a fallar. Sin este try/except
                # esa segunda excepcion no tenia donde caer y tumbaba
                # TODO el proceso, matando de paso las fuentes que
                # todavia no habian corrido en este run. Se deja
                # loggeado y se sigue con la siguiente fuente: registrar
                # en ingest_runs es best-effort, no debe ser un punto
                # unico de falla para el resto del pipeline.
                log(f"== fuente {name}: no se pudo registrar en ingest_runs -> {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Orquestador de ingesta de frontal_sur")
    parser.add_argument("--days", default=str(DEFAULT_DAYS), help=f"ventana en dias hacia atras desde ahora, entre {MIN_DAYS} y {MAX_DAYS} (default: {DEFAULT_DAYS})")
    parser.add_argument("--start", default=None, help="fecha inicial YYYY-MM-DD (extraccion por fechas definidas; requiere --end)")
    parser.add_argument("--end", default=None, help="fecha final YYYY-MM-DD (con --start)")
    parser.add_argument("--dry-run", action="store_true", help="hace fetch sin escribir a la BD")
    parser.add_argument("--sources", default=None, help="lista separada por comas para correr solo esas fuentes (ej: dga,chirps); default: todas")
    parser.add_argument("--resume-after", default=None, help="codigo de la ultima estacion confirmada upserteada en el log (cod_bna/cod_estacion segun la fuente); retoma el catalogo despues de esa estacion (usar junto con --sources dga|dmc|agromet)")
    parser.add_argument("--workers", default="1", help=f"estaciones scrapeadas en simultaneo (dga/dmc/agromet), entre 1 y {MAX_SOURCE_WORKERS} (verificado en vivo el 2026-07-19 en las tres fuentes; con 10 dgasat devuelve 500 en el 100%% de los casos; default: 1, secuencial)")
    parser.add_argument("--status", action="store_true", help="solo reporta hasta que fecha hay datos por fuente (BD y disco) y la ultima corrida registrada; no ingesta nada")
    args = parser.parse_args()

    if args.status:
        with get_engine(app_role_config()) as engine:
            print_status(engine)
        return

    # Dos modos de ventana: --days (relativa al presente, para el cron)
    # o --start/--end (fechas definidas, para backfill puntual). Son
    # excluyentes en la practica: si viene --start, manda el rango.
    if args.start is not None or args.end is not None:
        if args.start is None or args.end is None:
            raise ValueError("--start y --end van juntos")
        start = parse_date(args.start)
        end = parse_date(args.end)
        if start >= end:
            raise ValueError("--start debe ser anterior a --end")
    else:
        days = parse_days(args.days)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=days)

    sources = args.sources.split(",") if args.sources else None
    workers = parse_workers(args.workers)
    run(start=start, end=end, dry_run=args.dry_run, sources=sources,
        resume_after=args.resume_after, workers=workers)


if __name__ == "__main__":
    main()
