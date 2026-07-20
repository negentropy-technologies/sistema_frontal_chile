"""
Test plano (con assert, sin pytest) para ingest.py.

Correr con: .venv/bin/python backend/tests/test_ingest.py

test_parse_days_* no usa red ni BD. test_upsert_is_idempotent SI pega
contra la BD real (rol frontal_sur_app): inserta una fila sintetica
dos veces en frontal_sur.frames_raster (que tiene UNIQUE sobre source,
variable, region, valid_time) y verifica que la segunda pasada no
duplica filas, luego limpia lo que inserto.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone

from db import app_role_config, get_engine, upsert
from ingest import MAX_SOURCE_WORKERS, parse_days, parse_workers
from sqlalchemy import text


def test_parse_days_accepts_valid_range():
    assert parse_days("7") == 7
    assert parse_days("1") == 1
    assert parse_days("90") == 90


def test_parse_days_rejects_out_of_range():
    for invalid in ("0", "91", "-3", "abc"):
        try:
            parse_days(invalid)
            assert False, f"se esperaba ValueError para {invalid!r}"
        except ValueError:
            pass


def test_parse_workers_accepts_valid_range():
    assert parse_workers("1") == 1
    assert parse_workers(str(MAX_SOURCE_WORKERS)) == MAX_SOURCE_WORKERS


def test_parse_workers_rejects_out_of_range():
    # 10 es justo el valor que en vivo el 2026-07-19 hizo que dgasat
    # devolviera 500 en el 100% de los casos: debe rechazarse antes de
    # llegar a dga.fetch_batches.
    for invalid in ("0", "10", "-1", "abc"):
        try:
            parse_workers(invalid)
            assert False, f"se esperaba ValueError para {invalid!r}"
        except ValueError:
            pass


def test_upsert_is_idempotent():
    config = app_role_config()

    row = {
        "source": "test_ingest_idempotency",
        "variable": "test_variable",
        "region": "test_region",
        "valid_time": datetime(2000, 1, 1, tzinfo=timezone.utc),
        "bbox": [-118.5, -57.0, -65.5, -28.5],
        "file_path": "/tmp/test_ingest.tif",
        "png_overlay_path": "/tmp/test_ingest.png",
        "created_at": datetime(2000, 1, 1, tzinfo=timezone.utc),
    }
    conflict_cols = ["source", "variable", "region", "valid_time"]
    where = "source = :source"
    params = {"source": row["source"]}

    with get_engine(config) as engine:
        try:
            upsert(engine, "frontal_sur.frames_raster", [row], conflict_cols)
            # Segunda pasada con otro file_path: debe actualizar la
            # misma fila (ON CONFLICT DO UPDATE), no insertar otra.
            row["file_path"] = "/tmp/test_ingest_v2.tif"
            upsert(engine, "frontal_sur.frames_raster", [row], conflict_cols)

            with engine.connect() as conn:
                count = conn.execute(text(
                    f"select count(*) from frontal_sur.frames_raster where {where}"
                ), params).scalar()
                path = conn.execute(text(
                    f"select file_path from frontal_sur.frames_raster where {where}"
                ), params).scalar()
            assert count == 1, f"se esperaba 1 fila tras dos upserts, hay {count}"
            assert path == "/tmp/test_ingest_v2.tif", f"se esperaba file_path actualizado, hay {path}"
        finally:
            with engine.begin() as conn:
                conn.execute(text(
                    f"delete from frontal_sur.frames_raster where {where}"
                ), params)


if __name__ == "__main__":
    test_parse_days_accepts_valid_range()
    test_parse_days_rejects_out_of_range()
    test_parse_workers_accepts_valid_range()
    test_parse_workers_rejects_out_of_range()
    test_upsert_is_idempotent()
    print("OK: todos los tests de ingest.py pasaron")
