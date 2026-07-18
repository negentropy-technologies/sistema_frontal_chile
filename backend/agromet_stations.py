"""
Script de catalogo de estaciones de la Red Agroclimatica Nacional
(agromet.cl): upsertea en frontal_sur.agromet_stations las estaciones
del seed backend/seeds/agromet_estaciones.csv (417 estaciones,
derivado del catalogo del paquete R agrometR de ODES-Chile, el mismo
que usa el sitio). Es la mitad "geometrias" del par de scripts
Agromet: extractors/agromet.py (los datos) lee las estaciones desde
la tabla, no del CSV.

Se usa un seed local y no un endpoint porque agromet.cl no expone un
servicio de catalogo (solo el getGraphData por estacion) y su WAF
bloquea sondeos repetidos; la red cambia poco, y actualizar el seed
es regenerar el CSV.

Uso:
    .venv/bin/python backend/agromet_stations.py
"""

import csv
from pathlib import Path

from db import get_engine, load_config, upsert

SEED_PATH = Path(__file__).resolve().parent / "seeds" / "agromet_estaciones.csv"


def load_stations(seed_path: Path = SEED_PATH) -> list[dict]:
    """
    Lee el seed CSV y devuelve una fila por estacion con las columnas
    de frontal_sur.agromet_stations. Estaciones sin coordenadas se
    descartan (sin geometria no sirven para el mapa ni para el filtro
    por bbox del extractor).
    """
    rows = []
    with seed_path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            if not record["latitud"] or not record["longitud"]:
                continue
            # La comuna del CSV se ignora: la referencia territorial
            # es comuna_id, calculado con ST_Contains contra
            # dpa_limites.dpa_comuna_subdere despues del upsert.
            rows.append({
                "cod_estacion": record["cod_estacion"],
                "institucion": record["institucion"] or None,
                "nombre": record["nombre"] or None,
                "region": record["region"] or None,
                "geometria": f"POINT({float(record['longitud'])} {float(record['latitud'])})",
            })
    return rows


def assign_comunas(engine, table: str) -> int:
    """
    Puebla comuna_id cruzando la geometria de cada estacion contra
    los poligonos de dpa_limites.dpa_comuna_subdere (ST_Contains).
    Queda NULL para estaciones fuera de todo poligono comunal (costa
    afuera o coordenadas imprecisas del catalogo de origen). Devuelve
    cuantas estaciones quedaron con comuna asignada.
    """
    from sqlalchemy import text

    with engine.begin() as conn:
        conn.execute(text(f"""
            update {table} s
            set comuna_id = c.comuna_id
            from dpa_limites.dpa_comuna_subdere c
            where ST_Contains(c.geometria, s.geometria)
        """))
        asignadas = conn.execute(text(
            f"select count(*) from {table} where comuna_id is not null"
        )).scalar()
    return asignadas


def run() -> None:
    rows = load_stations()
    config = dict(load_config())
    config["DB_USER"] = config["DB_APP_USER"]
    config["DB_PASSWORD"] = config["DB_APP_PASSWORD"]
    with get_engine(config) as engine:
        count = upsert(engine, "frontal_sur.agromet_stations", rows, ["cod_estacion"], {"geometria": 4326})
        asignadas = assign_comunas(engine, "frontal_sur.agromet_stations")
    print(f"{count} estaciones Agromet upserteadas en frontal_sur.agromet_stations, {asignadas} con comuna_id")


if __name__ == "__main__":
    run()
