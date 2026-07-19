"""
Test plano (con assert, sin pytest) para extractors/chirps_climatology.py.

Correr con: .venv/bin/python backend/tests/test_chirps_climatology.py

Solo la validacion pura del minimo WMO y la construccion de URLs: el
recorte real contra data.chc.ucsb.edu lo cubre la corrida real del
orquestador, igual que test_chirps.py con chirps.py.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors.chirps_climatology import (
    MIN_CLIMATOLOGY_YEARS,
    _day_url,
    validar_years_disponibles,
)


def test_validar_years_disponibles_cumple_minimo_wmo():
    # WMO-No. 1203: minimo 10 anios para un "period average" valido.
    assert MIN_CLIMATOLOGY_YEARS == 10
    validar_years_disponibles(1998, 2025)  # 28 anios, no debe lanzar


def test_validar_years_disponibles_rechaza_menos_del_minimo():
    try:
        validar_years_disponibles(2018, 2020)  # 3 anios
        assert False, "debio lanzar ValueError"
    except ValueError as exc:
        assert "10" in str(exc)


def test_day_url_sigue_la_convencion_de_nombre_de_archivo_del_chc():
    # Verificado en vivo el 2026-07-19 contra data.chc.ucsb.edu: rama
    # final/sat (misma desagregacion IMERG que ya usa extractors/chirps.py
    # para la rama prelim), ej chirps-v3.0.sat.2020.01.01.tif.
    url = _day_url(2020, 1, 5)
    assert url == "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/2020/chirps-v3.0.sat.2020.01.05.tif"


if __name__ == "__main__":
    test_validar_years_disponibles_cumple_minimo_wmo()
    test_validar_years_disponibles_rechaza_menos_del_minimo()
    test_day_url_sigue_la_convencion_de_nombre_de_archivo_del_chc()
    print("OK: todos los tests de chirps_climatology pasaron")
