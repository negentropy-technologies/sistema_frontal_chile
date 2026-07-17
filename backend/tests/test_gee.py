"""
Test plano (con assert, sin pytest) para gee.py.

Correr con: .venv/bin/python backend/tests/test_gee.py

Este test SI llama a la API real de Google Earth Engine (fuente ya
validada, ver spec) y descarga archivos reales a data/frames/. Usa una
ventana chica (1 dia) para mantener la descarga acotada: con el
submuestreo horario del extractor son como maximo ~24 frames por
fuente. No escribe nada en la base de datos.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors.gee import fetch_choropleth, fetch_frames

REGION_BBOX = (-85.0, -44.5, -69.5, -32.5)


def test_fetch_frames_returns_well_shaped_rows():
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=1)
    rows = fetch_frames(start, end, REGION_BBOX)

    assert isinstance(rows, list)
    assert len(rows) > 0, "se esperaba al menos un frame en una ventana de 1 dia"
    for row in rows:
        assert set(row.keys()) == {
            "source", "variable", "region", "valid_time",
            "bbox", "file_path", "png_overlay_path", "created_at",
        }
        assert row["source"] == "gee"
        assert row["variable"] in ("goes_cloud_moisture", "imerg_precipitation")
        assert isinstance(row["valid_time"], datetime)
        assert Path(row["file_path"]).exists()
        assert Path(row["png_overlay_path"]).exists()


def test_fetch_choropleth_returns_well_shaped_rows():
    # Ventana de 2 dias, no 1: IMERG llega a GEE con ~24 horas de
    # retraso (medido en vivo el 2026-07-17), asi que una ventana de
    # 1 dia puede venir vacia y el assert de len > 0 fallaria.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=2)
    rows = fetch_choropleth(start, end, REGION_BBOX)

    assert isinstance(rows, list)
    assert len(rows) > 0, "se esperaba al menos una comuna con valor agregado"
    for row in rows:
        assert set(row.keys()) == {"comuna_id", "variable", "agg", "value", "valid_time"}
        assert isinstance(row["comuna_id"], int)
        assert row["variable"] == "imerg_precipitation"
        assert row["agg"] == "sum"
        assert isinstance(row["valid_time"], datetime)


if __name__ == "__main__":
    test_fetch_frames_returns_well_shaped_rows()
    test_fetch_choropleth_returns_well_shaped_rows()
    print("OK: todos los tests de gee.py pasaron")