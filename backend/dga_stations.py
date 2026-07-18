"""
Script de catalogo de estaciones de la Red Hidrometrica Nacional de
la DGA: upsertea en frontal_sur.dga_stations las estaciones del seed
backend/seeds/dga_estaciones.csv (4349 estaciones con coordenadas,
derivado del MapServer ArcGIS DGA/Red_Hidrometrica de
rest-sit.mop.gob.cl). Es la mitad "geometrias" del par de scripts DGA:
extractors/dga.py (los datos) lee las estaciones desde la tabla, no
del CSV.

Se usa un seed local y no el endpoint en vivo porque el catalogo
cambia poco y el ArcGIS 10.2 del MOP no soporta paginacion estandar
(hay que iterar por rangos de OBJECTID); actualizar el seed es
regenerar el CSV. El cod_bna del ArcGIS viene sin digito verificador
y el generador del seed lo completa con modulo 11 (validado contra
los 1773 codigos del formulario dgasat: 1772 calzan y el restante es
un error de la propia pagina).

Uso:
    .venv/bin/python backend/dga_stations.py
"""

import csv
from pathlib import Path

from agromet_stations import assign_comunas
from db import get_engine, load_config, upsert

SEED_PATH = Path(__file__).resolve().parent / "seeds" / "dga_estaciones.csv"


def load_stations(seed_path: Path = SEED_PATH) -> list[dict]:
    """
    Lee el seed CSV y devuelve una fila por estacion con las columnas
    de frontal_sur.dga_stations. El generador del seed ya descarto las
    estaciones sin codigo BNA o sin coordenadas, pero se revalida aqui
    por si el CSV se regenera con otro criterio.
    """
    rows = []
    with seed_path.open(newline="", encoding="utf-8") as handle:
        for record in csv.DictReader(handle):
            if not record["cod_bna"] or not record["latitud"] or not record["longitud"]:
                continue
            # La comuna del CSV se ignora: la referencia territorial
            # es comuna_id, calculado con ST_Contains contra
            # dpa_limites.dpa_comuna_subdere despues del upsert.
            rows.append({
                "cod_bna": record["cod_bna"],
                "nombre": record["nombre"] or None,
                "tipo_estacion": record["tipo_estacion"] or None,
                "vigencia": record["vigencia"] or None,
                "region": record["region"] or None,
                "cuenca": record["cuenca"] or None,
                "altitud": float(record["altitud"]) if record["altitud"] else None,
                "geometria": f"POINT({float(record['longitud'])} {float(record['latitud'])})",
            })
    return rows


def run() -> None:
    rows = load_stations()
    config = dict(load_config())
    config["DB_USER"] = config["DB_APP_USER"]
    config["DB_PASSWORD"] = config["DB_APP_PASSWORD"]
    with get_engine(config) as engine:
        count = upsert(engine, "frontal_sur.dga_stations", rows, ["cod_bna"], {"geometria": 4326})
        asignadas = assign_comunas(engine, "frontal_sur.dga_stations")
    print(f"{count} estaciones DGA upserteadas en frontal_sur.dga_stations, {asignadas} con comuna_id")


if __name__ == "__main__":
    run()
