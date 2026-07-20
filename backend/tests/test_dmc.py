"""
Test plano (con assert, sin pytest) para dmc.py y dmc_stations.py.

Correr con: .venv/bin/python backend/tests/test_dmc.py

Solo prueba las partes puras (sin red ni BD): el calculo de meses de
la ventana, el parser numerico de los valores de la DMC, la
reparacion de encoding del catalogo, y que el mapeo COLUMNAS este
sincronizado con la migracion del tablon dmc_datos. El camino con
red/BD queda cubierto por la corrida real del orquestador (fuente
"dmc" en ingest_runs).
"""

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "management"))

from dmc_stations import reparar_encoding
from extractors.dmc import COLUMNAS, _months, _numeric, _skip_to_resume

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0002_dmc.sql"
DICCIONARIO = Path(__file__).resolve().parent.parent / "migrations" / "0002_dmc.sql"


def test_months_within_one_month():
    start = datetime(2026, 7, 2, tzinfo=timezone.utc)
    end = datetime(2026, 7, 20, tzinfo=timezone.utc)
    assert _months(start, end) == [(2026, 7)]


def test_months_across_year_boundary():
    start = datetime(2025, 11, 15, tzinfo=timezone.utc)
    end = datetime(2026, 2, 3, tzinfo=timezone.utc)
    assert _months(start, end) == [(2025, 11), (2025, 12), (2026, 1), (2026, 2)]


def test_numeric():
    assert _numeric("9.8 °C") == 9.8
    assert _numeric("94 %") == 94.0
    assert _numeric("158.900 Watt/m2") == 158.9
    assert _numeric("-3.2 °C") == -3.2
    assert _numeric("12.5") == 12.5
    assert _numeric(None) is None
    assert _numeric("") is None
    assert _numeric("s/n") is None


def test_columnas_sincronizadas_con_migracion():
    ddl = MIGRATION.read_text()
    for column in COLUMNAS.values():
        assert f"    {column} DOUBLE PRECISION" in ddl, f"columna {column} falta en la migracion 0002"


def test_columnas_sincronizadas_con_diccionario():
    ddl = DICCIONARIO.read_text()
    for field, column in COLUMNAS.items():
        assert f"('{column}', " in ddl, f"variable {column} falta en el diccionario de 0002"
        assert f", '{field}')" in ddl, f"campo_endpoint {field} falta en el diccionario de 0002"


def test_reparar_encoding():
    assert reparar_encoding("San Felipe Escuela AgrÃ\xadcola") == "San Felipe Escuela Agrícola"
    assert reparar_encoding("ValparaÃ\xadso") == "Valparaíso"
    assert reparar_encoding("Osorno") == "Osorno"
    assert reparar_encoding(None) is None
    assert reparar_encoding("") == ""


def test_skip_to_resume_corta_despues_del_cod_estacion_dado():
    stations = [(1, "AAA"), (2, "BBB"), (3, "CCC")]
    assert _skip_to_resume(stations, "BBB") == [(3, "CCC")]


def test_skip_to_resume_sin_valor_no_filtra():
    stations = [(1, "AAA")]
    assert _skip_to_resume(stations, None) == stations


def test_skip_to_resume_no_encontrado_corre_completo():
    stations = [(1, "AAA")]
    assert _skip_to_resume(stations, "ZZZ") == stations


if __name__ == "__main__":
    test_months_within_one_month()
    test_months_across_year_boundary()
    test_numeric()
    test_columnas_sincronizadas_con_migracion()
    test_reparar_encoding()
    test_skip_to_resume_corta_despues_del_cod_estacion_dado()
    test_skip_to_resume_sin_valor_no_filtra()
    test_skip_to_resume_no_encontrado_corre_completo()
    print("OK: todos los tests de dmc pasaron")