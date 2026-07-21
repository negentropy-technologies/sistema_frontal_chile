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

from db import (
    app_role_config,
    build_db_url,
    build_ids_con_datos_sql,
    build_ssh_tunnel_kwargs,
    build_upsert_sql,
    build_upsert_template,
)


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
    # build_upsert_sql ahora arma el SQL para execute_values: un unico
    # placeholder "VALUES %s" (execute_values lo reemplaza por N tuplas
    # en un solo round-trip), no un INSERT de una fila por :col.
    sql = build_upsert_sql("frontal_sur.ingest_runs", ["id", "source", "status"], ["id"])
    assert sql == (
        "INSERT INTO frontal_sur.ingest_runs (id, source, status) "
        "VALUES %s "
        "ON CONFLICT (id) DO UPDATE SET source = EXCLUDED.source, status = EXCLUDED.status"
    )


def test_build_upsert_sql_do_nothing():
    # do_nothing=True: ON CONFLICT DO NOTHING, sin SET ni EXCLUDED.
    # Las filas ya existentes no se tocan, solo se insertan las nuevas.
    sql = build_upsert_sql("frontal_sur.dga_datos", ["estacion_id", "momento", "caudal"],
                            ["estacion_id", "momento"], conflict_do_nothing=True)
    assert sql == (
        "INSERT INTO frontal_sur.dga_datos (estacion_id, momento, caudal) "
        "VALUES %s "
        "ON CONFLICT (estacion_id, momento) DO NOTHING"
    )


def test_build_upsert_template_plain():
    # Sin columnas de geometria: un placeholder con nombre por columna.
    template = build_upsert_template(["id", "source", "status"])
    assert template == "(%(id)s, %(source)s, %(status)s)"


def test_build_upsert_template_with_geom():
    # Con una columna de geometria: en vez de bindear el valor directo,
    # la columna debe envolverse en ST_GeomFromText(%(col)s, srid) para
    # que Postgres/PostGIS lo interprete como geometria y no texto.
    template = build_upsert_template(
        ["station_id", "geom", "value"],
        geom_cols={"geom": 4326},
    )
    assert template == "(%(station_id)s, ST_GeomFromText(%(geom)s, 4326), %(value)s)"


def test_build_ids_con_datos_sql():
    sql = build_ids_con_datos_sql("frontal_sur.dga_datos", "estacion_id")
    assert sql == (
        "SELECT estacion_id, count(DISTINCT date_trunc('day', momento)) "
        "FROM frontal_sur.dga_datos WHERE estacion_id = ANY(:ids) "
        "AND momento >= :start AND momento < :end GROUP BY estacion_id"
    )


def test_app_role_config_swaps_user_and_password():
    # No debe mutar el diccionario recibido: config original intacta
    # para que el caller la pueda seguir usando (ver docstring).
    config = {"DB_USER": "postgres", "DB_PASSWORD": "root", "DB_APP_USER": "frontal_sur_app", "DB_APP_PASSWORD": "hunter2"}
    resultado = app_role_config(config)
    assert resultado["DB_USER"] == "frontal_sur_app"
    assert resultado["DB_PASSWORD"] == "hunter2"
    assert config["DB_USER"] == "postgres"


if __name__ == "__main__":
    test_build_db_url()
    test_build_ssh_tunnel_kwargs()
    test_build_upsert_sql_plain()
    test_build_upsert_sql_do_nothing()
    test_build_upsert_template_plain()
    test_build_upsert_template_with_geom()
    test_build_ids_con_datos_sql()
    test_app_role_config_swaps_user_and_password()
    print("OK: todos los tests de db.py pasaron")