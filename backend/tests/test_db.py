"""
Tests planos (con assert, sin pytest) para db.py.

Correr con: .venv/bin/python tests/test_db.py

Estos tests no abren ninguna conexion real ni tunel SSH: solo prueban
las funciones puras de db.py (build_db_url, build_ssh_tunnel_kwargs,
build_upsert_sql), que reciben la configuracion como un diccionario
explicito en vez de leerla implicitamente desde variables de entorno.
Por eso no dependen de que exista un archivo .env real en disco.
"""

import sys
from pathlib import Path

# Agrega la raiz del proyecto (el directorio padre de tests/) al path
# de importacion, para poder hacer "from db import ..." sin instalar
# el proyecto como paquete. Se usa pathlib en vez de os.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import build_db_url, build_ssh_tunnel_kwargs, build_upsert_sql


def test_build_db_url():
    # Diccionario de configuracion de prueba, equivalente a lo que
    # devolveria dotenv_values() al leer un archivo .env real.
    config = {"DB_USER": "foo", "DB_PASSWORD": "bar", "DB_NAME": "baz"}
    assert build_db_url(config, 5555) == "postgresql+psycopg2://foo:bar@127.0.0.1:5555/baz"


def test_build_ssh_tunnel_kwargs():
    # Config de prueba con todas las variables que necesita el tunel
    # SSH. SSH_PORT llega como string (asi vienen los valores de un
    # archivo .env), por eso build_ssh_tunnel_kwargs debe convertirlo
    # a int internamente.
    config = {
        "SSH_HOST": "h",
        "SSH_PORT": "2222",
        "SSH_USER": "u",
        "SSH_KEY_PATH": "/path/key",
        "SSH_REMOTE_DB_PORT": "5432",
    }
    kwargs = build_ssh_tunnel_kwargs(config)
    assert kwargs["host"] == "h"
    assert kwargs["port"] == 2222
    assert kwargs["ssh_username"] == "u"
    assert kwargs["ssh_pkey"] == "/path/key"
    assert kwargs["remote_bind_address"] == ("127.0.0.1", 5432)


def test_build_upsert_sql_plain():
    # Caso sin columnas de geometria: el upsert es un INSERT ...
    # ON CONFLICT ... DO UPDATE plano, todas las columnas menos las
    # de conflicto se actualizan con el valor entrante (EXCLUDED).
    sql = build_upsert_sql("frontal_sur.ingest_runs", ["id", "source", "status"], ["id"])
    assert sql == (
        "INSERT INTO frontal_sur.ingest_runs (id, source, status) "
        "VALUES (:id, :source, :status) "
        "ON CONFLICT (id) DO UPDATE SET source = EXCLUDED.source, status = EXCLUDED.status"
    )


def test_build_upsert_sql_with_geom():
    # Caso con una columna de geometria: en vez de bindear el valor
    # directo, la columna debe envolverse en ST_GeomFromText(:col, srid)
    # para que Postgres/PostGIS lo interprete como geometria y no texto.
    sql = build_upsert_sql(
        "frontal_sur.station_obs",
        ["station_id", "geom", "value"],
        ["station_id"],
        geom_cols={"geom": 4326},
    )
    assert "ST_GeomFromText(:geom, 4326)" in sql
    assert "VALUES (:station_id, ST_GeomFromText(:geom, 4326), :value)" in sql


if __name__ == "__main__":
    test_build_db_url()
    test_build_ssh_tunnel_kwargs()
    test_build_upsert_sql_plain()
    test_build_upsert_sql_with_geom()
    print("OK: todos los tests de db.py pasaron")