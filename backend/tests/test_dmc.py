"""
Test plano (con assert, sin pytest) para dmc.py y dmc_stations.py.

Correr con: .venv/bin/python backend/tests/test_dmc.py

Solo prueba las partes puras (sin red ni BD): el calculo de meses de
la ventana, el parser numero+unidad de los valores de la DMC, y la
reparacion de encoding del catalogo. El camino con red/BD queda
cubierto por la corrida real del orquestador (fuente "dmc" en
ingest_runs).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dmc_stations import reparar_encoding
from extractors.dmc import _months, _numeric_with_unit


def test_months_within_one_month():
    start = datetime(2026, 7, 2, tzinfo=timezone.utc)
    end = datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert _months(start, end) == [(2026, 7)]


def test_months_across_year_boundary():
    start = datetime(2025, 11, 15, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)
    assert _months(start, end) == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_numeric_with_unit():
    assert _numeric_with_unit("9.8 °C") == (9.8, "°C")
    assert _numeric_with_unit("94 %") == (94.0, "%")
    assert _numeric_with_unit("158.900 Watt/m2") == (158.9, "Watt/m2")
    assert _numeric_with_unit("-3.2 °C") == (-3.2, "°C")
    assert _numeric_with_unit("12.5") == (12.5, None)
    assert _numeric_with_unit(None) is None
    assert _numeric_with_unit("") is None
    assert _numeric_with_unit("s/n") is None


def test_reparar_encoding():
    assert reparar_encoding("San Felipe Escuela AgrÃ\xadcola") == "San Felipe Escuela Agrícola"
    assert reparar_encoding("ValparaÃ\xadso") == "Valparaíso"
    assert reparar_encoding("Osorno") == "Osorno"
    assert reparar_encoding(None) is None
    assert reparar_encoding("") == ""


if __name__ == "__main__":
    test_months_within_one_month()
    test_months_across_year_boundary()
    test_numeric_with_unit()
    test_reparar_encoding()
    print("OK: todos los tests de dmc pasaron")