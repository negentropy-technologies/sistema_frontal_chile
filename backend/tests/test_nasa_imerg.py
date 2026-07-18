"""
Test plano (con assert, sin pytest) para nasa_imerg.py.

Correr con: .venv/bin/python backend/tests/test_nasa_imerg.py

Este test SI llama a NASA GES DISC con las credenciales Earthdata de
backend/.env y descarga archivos reales a data/frames/nasa_imerg/;
el test de coropletas ademas lee las comunas de la BD real (rol
frontal_sur_app). Ventanas chicas para acotar la descarga: 2 dias del
diario y ~3 horas del media-horario (cada granulo global pesa ~8 MB
y se borra tras recortarlo). No escribe nada en la base de datos.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rasterio

from extractors.nasa_imerg import _bbox_slices, _session, _fetch_daily, _fetch_half_hourly, fetch_choropleth
from db import load_config

REGION_BBOX = (-118.5, -57.0, -65.5, -28.5)

FRAME_KEYS = {
    "source", "variable", "region", "valid_time",
    "bbox", "file_path", "png_overlay_path", "created_at",
}


def test_bbox_slices_math():
    rows, cols = _bbox_slices(REGION_BBOX)
    # Grilla global 0.1 grados: lon -118.5 -> columna 615, lat
    # +90-(-28.5) -> fila 1185. El recorte debe medir 530 columnas
    # (hasta el Mar Presencial) x 285 filas.
    assert (cols.start, cols.stop) == (615, 1145)
    assert (rows.start, rows.stop) == (1185, 1470)


def test_fetch_daily_returns_georeferenced_crops():
    session = _session(load_config())
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=2)
    rows = _fetch_daily(session, start, end, REGION_BBOX)

    assert len(rows) > 0, "se esperaba al menos un dia IMERG Early publicado"
    for row in rows:
        assert set(row.keys()) == FRAME_KEYS
        assert row["source"] == "nasa_imerg"
        assert row["variable"] == "imerg_early_daily"
        assert Path(row["file_path"]).exists()
        assert Path(row["png_overlay_path"]).exists()
    with rasterio.open(rows[0]["file_path"]) as src:
        assert src.crs.to_epsg() == 4326
        # El recorte debe cubrir exactamente el bbox del proyecto.
        assert abs(src.bounds.left - REGION_BBOX[0]) < 0.11
        assert abs(src.bounds.top - REGION_BBOX[3]) < 0.11
        band = src.read(1)
        finite = band[band == band]
        assert finite.size > 0 and float(finite.min()) >= 0.0


def test_fetch_half_hourly_returns_rows():
    session = _session(load_config())
    end = datetime.now(timezone.utc) - timedelta(hours=6)
    start = end - timedelta(hours=3)
    rows = _fetch_half_hourly(session, start, end, REGION_BBOX)

    assert len(rows) > 0, "se esperaba al menos un granulo de 30 min con 6h de retraso"
    for row in rows:
        assert set(row.keys()) == FRAME_KEYS
        assert row["variable"] == "imerg_early_30min"
        assert Path(row["file_path"]).exists()


def test_fetch_choropleth_returns_well_shaped_rows():
    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=2)
    rows = fetch_choropleth(start, end, REGION_BBOX)

    assert len(rows) > 0, "se esperaba al menos una comuna con valor agregado"
    for row in rows:
        assert set(row.keys()) == {"comuna_id", "variable", "agg", "value", "valid_time"}
        assert isinstance(row["comuna_id"], int)
        assert row["variable"] == "imerg_precipitation"
        assert row["agg"] == "sum"
        assert row["value"] >= 0.0
        assert isinstance(row["valid_time"], datetime)


if __name__ == "__main__":
    test_bbox_slices_math()
    test_fetch_daily_returns_georeferenced_crops()
    test_fetch_half_hourly_returns_rows()
    test_fetch_choropleth_returns_well_shaped_rows()
    print("OK: todos los tests de nasa_imerg.py pasaron")
