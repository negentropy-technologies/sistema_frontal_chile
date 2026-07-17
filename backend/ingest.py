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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text

from db import get_engine, load_config, upsert
from extractors import gee, noaa_ncei

# Unica fuente de verdad geografica del proyecto: Metropolitana a Los
# Lagos, mas la Zona Economica Exclusiva y Plataforma Continental de
# dpa_limites.espacio_marino_chile recortada a esa banda de latitud.
# Ver docs/superpowers/specs/2026-07-16-data-ingestion-design.md para
# como se calculo.
REGION_BBOX = (-85.0, -44.5, -69.5, -32.5)

MIN_DAYS = 1
MAX_DAYS = 90

# Cada entrada: (nombre de la fuente, funcion fetch, tabla destino,
# columnas de conflicto, columnas de geometria). ingest_runs registra
# el resultado de cada una por separado.
SOURCES = [
    (
        "gee_frames",
        lambda start, end: gee.fetch_frames(start, end, REGION_BBOX),
        "frontal_sur.frames_raster",
        ["source", "variable", "region", "valid_time"],
        None,
    ),
    (
        "gee_choropleth",
        lambda start, end: gee.fetch_choropleth(start, end, REGION_BBOX),
        "frontal_sur.choropleth_stats",
        ["comuna_id", "variable", "agg", "valid_time"],
        None,
    ),
    (
        "noaa_ncei",
        lambda start, end: noaa_ncei.fetch(start, end, REGION_BBOX),
        "frontal_sur.station_obs",
        ["source", "variable", "station_id", "valid_time"],
        {"geom": 4326},
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


def run(days: int, dry_run: bool) -> None:
    """
    Abre un unico tunel SSH (reutilizado para todas las fuentes) y
    corre cada fuente de SOURCES en su propio try/except, registrando
    el resultado en frontal_sur.ingest_runs. Una fuente que falla no
    detiene a las demas. En dry-run no se escribe nada, ni siquiera en
    ingest_runs, pero el tunel igual se abre: valida de paso que la
    conexion con el rol acotado funciona.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    config = _app_role_config()

    with get_engine(config) as engine:
        for name, fetch_fn, table, conflict_cols, geom_cols in SOURCES:
            started_at = datetime.now(timezone.utc)
            try:
                rows = fetch_fn(start, end)
                if not dry_run:
                    upsert(engine, table, rows, conflict_cols, geom_cols)
                status = "ok"
                error = None
                print(f"{name}: {len(rows)} filas" + (" (dry-run, no escrito)" if dry_run else ""))
            except Exception as exc:
                # Se guarda solo el str() de la excepcion: los
                # extractores no interpolan API keys ni passwords en
                # sus mensajes (restriccion del plan), asi que esto no
                # filtra secretos a ingest_runs ni al log del cron.
                status = "error"
                error = str(exc)
                print(f"{name}: fallo -> {error}")

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
    parser.add_argument("--days", default="7", help=f"ventana en dias, entre {MIN_DAYS} y {MAX_DAYS} (default: 7)")
    parser.add_argument("--dry-run", action="store_true", help="hace fetch sin escribir a la BD")
    args = parser.parse_args()

    days = parse_days(args.days)
    run(days=days, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
