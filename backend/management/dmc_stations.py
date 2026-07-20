"""
Script de catalogo de estaciones EMA de la DMC: descarga la red
completa desde getEstacionesRedEma y la upsertea (geometria incluida)
en frontal_sur.dmc_stations. Es la mitad "geometrias" del par de
scripts DMC: extractors/dmc.py (los datos) lee las estaciones desde
esta tabla, no desde la API, para no repetir la llamada de catalogo en
cada corrida del pipeline.

Correr una vez al inicio y luego ocasionalmente (la red EMA cambia
poco): el upsert por station_id lo hace idempotente.

Uso:
    .venv/bin/python backend/management/dmc_stations.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import app_role_config, get_engine, load_config, upsert
from extractors._http import build_session

STATIONS_URL = "https://climatologia.meteochile.gob.cl/application/servicios/getEstacionesRedEma"


def reparar_encoding(valor):
    """
    Repara textos con doble encoding de la API de la DMC (por ejemplo
    "AgrÃ­cola" en vez de "Agricola" con tilde, verificado en vivo en
    nombreEstacion): el texto llego como UTF-8 leido erroneamente como
    latin-1, asi que se invierte esa lectura. Si la reparacion no
    aplica (texto ya limpio, con caracteres fuera de latin-1), se
    devuelve tal cual. Version minima del limpiar_caracteres del
    script ETL legado del usuario.
    """
    if not isinstance(valor, str) or not valor:
        return valor
    try:
        return valor.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return valor


def fetch_stations(config: dict | None = None) -> list[dict]:
    """
    Pide el catalogo completo de la red EMA y devuelve una fila por
    estacion con las columnas de frontal_sur.dmc_stations (nombres de
    columna del modelo del ETL legado: cod_estacion, nombre, altura,
    zona). latitud/longitud van en la columna geometria.
    """
    if config is None:
        config = load_config()
    session = build_session()
    response = session.get(
        STATIONS_URL,
        params={"usuario": config["CORREO"], "token": config["API_KEY"]},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()

    rows = []
    for station in payload.get("datosEstacion", []):
        # Estaciones sin coordenadas no sirven para el mapa ni para el
        # filtro por bbox del extractor: se descartan aqui.
        if station.get("latitud") in (None, "") or station.get("longitud") in (None, ""):
            continue
        latitude = float(station["latitud"])
        longitude = float(station["longitud"])
        elevation = station.get("altura")
        rows.append({
            "cod_estacion": str(station["codigoNacional"]),
            "nombre": reparar_encoding(station.get("nombreEstacion")),
            "altura": float(elevation) if elevation not in (None, "") else None,
            "zona": reparar_encoding(station.get("zonaGeografica")),
            "region": reparar_encoding(station.get("NombreRegion")),
            "geometria": f"POINT({longitude} {latitude})",
        })
    return rows


def run() -> None:
    """
    Descarga el catalogo y lo upsertea con el rol acotado
    frontal_sur_app. Imprime cuantas estaciones quedaron.
    """
    rows = fetch_stations()
    with get_engine(app_role_config()) as engine:
        count = upsert(engine, "frontal_sur.dmc_stations", rows, ["cod_estacion"], {"geometria": 4326})
    print(f"{count} estaciones EMA upserteadas en frontal_sur.dmc_stations")


if __name__ == "__main__":
    run()