"""
Test plano (con assert, sin pytest) para chirps.py.

Correr con: .venv/bin/python backend/tests/test_chirps.py

Este test SI llama al servidor real del CHC (data.chc.ucsb.edu) y
descarga recortes reales a data/frames/chirps/. Usa una ventana que
termina 7 dias atras para caer dentro de lo ya publicado (latencia
prelim ~6-7 dias). No escribe nada en la base de datos.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors.chirps import fetch

REGION_BBOX = (-118.5, -44.5, -69.5, -32.5)


def test_fetch_returns_well_shaped_rows():
    end = datetime.now(timezone.utc) - timedelta(days=7)
    start = end - timedelta(days=2)
    rows = fetch(start, end, REGION_BBOX)

    assert isinstance(rows, list)
    assert len(rows) > 0, "se esperaba al menos un dia CHIRPS publicado en la ventana"
    for row in rows:
        assert set(row.keys()) == {
            "source", "variable", "region", "valid_time",
            "bbox", "file_path", "png_overlay_path", "created_at",
        }
        assert row["source"] == "chirps"
        assert row["variable"] == "chirps_precipitation"
        assert isinstance(row["valid_time"], datetime)
        assert row["valid_time"].tzinfo is not None
        assert Path(row["file_path"]).exists()
        assert Path(row["png_overlay_path"]).exists()


if __name__ == "__main__":
    test_fetch_returns_well_shaped_rows()
    print("OK: todos los tests de chirps.py pasaron")