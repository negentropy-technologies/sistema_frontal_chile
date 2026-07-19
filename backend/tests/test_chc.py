"""
Test plano (con assert, sin pytest) para extractors/_chc.py.

Correr con: .venv/bin/python backend/tests/test_chc.py

Solo la construccion de URL (pura): el recorte real (crop_day) requiere
red y lo cubren test_chirps.py y la corrida real del orquestador.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors._chc import day_url


def test_day_url_rama_prelim():
    url = day_url("prelim/sat", "prelim", 2026, 7, 15)
    assert url == "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/prelim/sat/2026/chirps-v3.0.prelim.2026.07.15.tif"


def test_day_url_rama_final():
    url = day_url("final/sat", "sat", 2020, 1, 5)
    assert url == "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat/2020/chirps-v3.0.sat.2020.01.05.tif"


if __name__ == "__main__":
    test_day_url_rama_prelim()
    test_day_url_rama_final()
    print("OK: todos los tests de _chc.py pasaron")
