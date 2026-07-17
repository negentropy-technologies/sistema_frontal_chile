"""
Test plano (con assert, sin pytest) para ingest.py.

Correr con: .venv/bin/python backend/tests/test_ingest.py

test_parse_days_* no usa red ni BD. test_upsert_is_idempotent SI pega
contra la BD real (rol frontal_sur_app): inserta una fila sintetica
dos veces en frontal_sur.station_obs (que tiene UNIQUE sobre source,
variable, station_id, valid_time) y verifica que la segunda pasada no
duplica filas, luego limpia lo que inserto.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datetime import datetime, timezone

from db import get_engine, load_config, upsert
from ingest import parse_days
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


def test_upsert_is_idempotent():
    config = dict(load_config())
    config["DB_USER"] = config["DB_APP_USER"]
    config["DB_PASSWORD"] = config["DB_APP_PASSWORD"]

    row = {
        "source": "test_ingest_idempotency",
        "station_id": "TEST0001",
        "station_name": "estacion sintetica de test",
        "valid_time": datetime(2000, 1, 1, tzinfo=timezone.utc),
        "variable": "TMAX",
        "value": 1.0,
        "unit": "metric",
        "geometria": "POINT(-71.0 -35.0)",
    }
    conflict_cols = ["source", "variable", "station_id", "valid_time"]
    where = "source = :source"
    params = {"source": row["source"]}

    with get_engine(config) as engine:
        try:
            upsert(engine, "frontal_sur.station_obs", [row], conflict_cols, {"geometria": 4326})
            # Segunda pasada con otro value: debe actualizar la misma
            # fila (ON CONFLICT DO UPDATE), no insertar una segunda.
            row["value"] = 2.0
            upsert(engine, "frontal_sur.station_obs", [row], conflict_cols, {"geometria": 4326})

            with engine.connect() as conn:
                count = conn.execute(text(
                    f"select count(*) from frontal_sur.station_obs where {where}"
                ), params).scalar()
                value = conn.execute(text(
                    f"select value from frontal_sur.station_obs where {where}"
                ), params).scalar()
            assert count == 1, f"se esperaba 1 fila tras dos upserts, hay {count}"
            assert value == 2.0, f"se esperaba value actualizado a 2.0, hay {value}"
        finally:
            with engine.begin() as conn:
                conn.execute(text(
                    f"delete from frontal_sur.station_obs where {where}"
                ), params)


if __name__ == "__main__":
    test_parse_days_accepts_valid_range()
    test_parse_days_rejects_out_of_range()
    test_upsert_is_idempotent()
    print("OK: todos los tests de ingest.py pasaron")
