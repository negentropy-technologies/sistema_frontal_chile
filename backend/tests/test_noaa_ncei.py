"""
Test plano (con assert, sin pytest) para noaa_ncei.py.

Correr con: .venv/bin/python backend/tests/test_noaa_ncei.py

Este test SI llama a la API real de NOAA NCEI (fuente ya validada
contra este entorno, ver spec). Usa una ventana chica (7 dias) y el
bbox real del proyecto para mantener la respuesta acotada. No escribe
nada en la base de datos.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors.noaa_ncei import fetch

REGION_BBOX = (-85.0, -44.5, -69.5, -32.5)


def test_fetch_returns_well_shaped_rows():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=7)
    rows = fetch(start, end, REGION_BBOX)

    assert isinstance(rows, list)
    # La ventana puede no traer estaciones si NCEI no tiene reportes
    # recientes para la region (ver nota de latencia del spec general),
    # asi que solo se valida la forma de las filas que si llegaron.
    for row in rows:
        assert set(row.keys()) == {
            "source", "station_id", "station_name", "geometria",
            "valid_time", "variable", "value", "unit",
        }
        assert row["source"] == "noaa_ncei"
        assert isinstance(row["station_id"], str)
        assert row["geometria"].startswith("POINT(")
        assert isinstance(row["valid_time"], datetime)
        assert row["valid_time"].tzinfo is not None
        assert row["variable"] in ("TMAX", "TMIN", "PRCP", "AWND")
        assert isinstance(row["value"], float)


if __name__ == "__main__":
    test_fetch_returns_well_shaped_rows()
    print("OK: todos los tests de noaa_ncei.py pasaron")