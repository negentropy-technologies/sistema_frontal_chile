"""
Orquestador de ingesta: corre los extractores de GEE y NOAA NCEI, y
escribe los resultados en frontal_sur via upsert, con el rol acotado
frontal_sur_app (nunca el superusuario). Cada fuente corre en su
propio try/except: si una falla, se registra el error en
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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import GeneratorType

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text

from db import get_engine, load_config, upsert
from extractors import agromet, chirps, dga, dmc, gee, nasa_imerg
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
    # flood_hub queda fuera de SOURCES: ver "Flood Hub, diferido a un
    # plan separado" en el plan de implementacion. Cuando llegue el
    # acceso, agregar aqui una entrada igual a las demas (funcion
    # fetch, tabla frontal_sur.flood_status,
    # conflict_cols=["gauge_id", "issued_time"],
    # geom_cols={"geom_point": 4326}).
]


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


def _app_role_config() -> dict:
    """
    Arma la config de conexion con el rol acotado frontal_sur_app en
    vez del superusuario (DB_USER/DB_PASSWORD), tal como se verifico a
    mano en la Tarea 3 del plan de esquema de BD.
    """
    config = dict(load_config())
    config["DB_USER"] = config["DB_APP_USER"]
    config["DB_PASSWORD"] = config["DB_APP_PASSWORD"]
    return config


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


def run(start: datetime, end: datetime, dry_run: bool, sources: list[str] | None = None) -> None:
    """
    Abre un unico tunel SSH (reutilizado para todas las fuentes) y
    corre cada fuente de SOURCES en su propio try/except, registrando
    el resultado en frontal_sur.ingest_runs. Una fuente que falla no
    detiene a las demas. En dry-run no se escribe nada, ni siquiera en
    ingest_runs, pero el tunel igual se abre: valida de paso que la
    conexion con el rol acotado funciona.
    """
    config = _app_role_config()

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
                            upsert(engine, table, batch, conflict_cols, geom_cols)
                        count += len(batch)
                        log(f"{name}: lote de {len(batch)} filas" + (" (dry-run)" if dry_run else " upserteado"))
                else:
                    if not dry_run:
                        upsert(engine, table, result, conflict_cols, geom_cols)
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Orquestador de ingesta de frontal_sur")
    parser.add_argument("--days", default=str(DEFAULT_DAYS), help=f"ventana en dias hacia atras desde ahora, entre {MIN_DAYS} y {MAX_DAYS} (default: {DEFAULT_DAYS})")
    parser.add_argument("--start", default=None, help="fecha inicial YYYY-MM-DD (extraccion por fechas definidas; requiere --end)")
    parser.add_argument("--end", default=None, help="fecha final YYYY-MM-DD (con --start)")
    parser.add_argument("--dry-run", action="store_true", help="hace fetch sin escribir a la BD")
    parser.add_argument("--sources", default=None, help="lista separada por comas para correr solo esas fuentes (ej: dga,chirps); default: todas")
    args = parser.parse_args()

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
    run(start=start, end=end, dry_run=args.dry_run, sources=sources)


if __name__ == "__main__":
    main()
