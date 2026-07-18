"""
Test plano (con assert, sin pytest) para agromet.py y
agromet_stations.py.

Correr con: .venv/bin/python backend/tests/test_agromet.py

Solo partes puras (sin red ni BD): el parser del XML de getGraphData
(fixture tomada de una respuesta real del 2026-07-17), el seed del
catalogo, y la consistencia del mapeo CAMPOS con la migracion 0003.
El camino con red queda cubierto por la corrida real del orquestador
(el WAF de agromet.cl castiga los sondeos repetidos, asi que el test
no le pega a la API).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agromet_stations import load_stations
from extractors.agromet import CAMPOS, _parse_datos

MIGRATION_TABLON = Path(__file__).resolve().parent.parent / "migrations" / "0003_agromet.sql"

XML_REAL = (
    "<?xml version='1.0' encoding='UTF-8'?><estacion id='108'>"
    "<dato>fecha|2026-07-16 00:00:00|1|12.200|2|0.000|3|87.800|4|1017.300"
    "|5|0.000|6|1.300|7|12.200|8|12.300|9|203.000|11||12|</dato>"
    "<dato>fecha|2026-07-16 01:00:00|1|12.700|2|0.300|3|85.500|4|1016.600"
    "|5|0.000|6|2.200|7|12.400|8|12.800|9|210.000|11||12|</dato>"
    "</estacion>"
)


def test_parse_datos_extrae_filas_normalizadas():
    start = datetime(2026, 7, 15, tzinfo=timezone.utc)
    end = datetime(2026, 7, 17, tzinfo=timezone.utc)
    rows = _parse_datos(XML_REAL, start, end)

    assert len(rows) == 2
    first = rows[0]
    assert set(first.keys()) == {"momento"} | set(CAMPOS.values())
    # 2026-07-16 00:00 hora de Chile (UTC-4 en julio) = 04:00 UTC.
    assert first["momento"] == datetime(2026, 7, 16, 4, 0, tzinfo=timezone.utc)
    assert first["temperatura"] == 12.2
    assert first["presion_estacion"] == 1017.3
    assert first["grados_dia_base10"] is None
    assert rows[1]["precipitacion_horaria"] == 0.3


def test_parse_datos_filtra_por_ventana():
    start = datetime(2026, 7, 16, 4, 30, tzinfo=timezone.utc)
    end = datetime(2026, 7, 17, tzinfo=timezone.utc)
    rows = _parse_datos(XML_REAL, start, end)
    assert len(rows) == 1
    assert rows[0]["temperatura"] == 12.7


def test_seed_catalogo():
    rows = load_stations()
    assert len(rows) > 400
    for row in rows[:5]:
        assert row["cod_estacion"]
        assert row["geometria"].startswith("POINT(")


def test_campos_sincronizados_con_migracion():
    tablon = MIGRATION_TABLON.read_text()
    for columna in CAMPOS.values():
        assert f"    {columna} DOUBLE PRECISION" in tablon, f"columna {columna} falta en 0003"


if __name__ == "__main__":
    test_parse_datos_extrae_filas_normalizadas()
    test_parse_datos_filtra_por_ventana()
    test_seed_catalogo()
    test_campos_sincronizados_con_migracion()
    print("OK: todos los tests de agromet pasaron")
