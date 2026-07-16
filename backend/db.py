"""
Conexion a la base de datos compartida de Negentropy via tunel SSH, mas
helpers de upsert para el esquema frontal_sur.

Toda la configuracion (host SSH, credenciales de base de datos, etc) se
lee del archivo .env con dotenv_values(), que devuelve un diccionario
en memoria en vez de mutar las variables de entorno del proceso. Por
eso este modulo no importa "os": ni para rutas de archivo (se usa
pathlib) ni para leer variables de entorno (se usa dotenv_values).

get_engine() es la unica funcion que toca la red: abre el tunel SSH
hacia el servidor remoto y crea un Engine de SQLAlchemy apuntando al
puerto local que expone ese tunel. El resto de las funciones son
puras (build_db_url, build_ssh_tunnel_kwargs, build_upsert_sql) y se
pueden testear sin acceso a red, como hace tests/test_db.py.
"""

from contextlib import contextmanager
from pathlib import Path

from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sshtunnel import SSHTunnelForwarder

# Ruta al archivo .env, relativa a este archivo (no al directorio de
# trabajo actual desde el que se invoque el script), para que el
# modulo funcione igual sin importar desde donde se lo importe.
ENV_PATH = Path(__file__).resolve().parent / ".env"


def load_config(env_path: Path = ENV_PATH) -> dict:
    """
    Lee el archivo .env indicado y devuelve un diccionario plano con
    todas las variables definidas ahi (DB_USER, DB_PASSWORD, DB_NAME,
    SSH_HOST, SSH_PORT, SSH_USER, SSH_KEY_PATH, SSH_REMOTE_DB_PORT,
    y cualquier otra que tenga el archivo). No modifica el entorno
    del proceso en ningun momento.
    """
    return dict(dotenv_values(env_path))


def build_db_url(config: dict, local_port: int) -> str:
    """
    Construye la URL de conexion de SQLAlchemy (dialecto psycopg2)
    apuntando a 127.0.0.1 en el puerto local que abre el tunel SSH.
    Las credenciales (usuario, password, nombre de la base de datos)
    salen del diccionario de configuracion, no de variables de
    entorno globales, para que la funcion sea facil de testear con
    distintos valores.
    """
    user = config["DB_USER"]
    password = config["DB_PASSWORD"]
    dbname = config["DB_NAME"]
    return f"postgresql+psycopg2://{user}:{password}@127.0.0.1:{local_port}/{dbname}"


def build_ssh_tunnel_kwargs(config: dict) -> dict:
    """
    Arma el diccionario de argumentos que necesita SSHTunnelForwarder
    para abrir el tunel: host y puerto del servidor SSH, usuario SSH,
    ruta a la llave privada, y la direccion remota (dentro del propio
    servidor remoto) del puerto de Postgres al que debe apuntar el
    extremo remoto del tunel.

    SSH_PORT y SSH_REMOTE_DB_PORT llegan como texto desde el archivo
    .env, por eso se convierten explicitamente a int aqui.
    """
    return {
        "host": config["SSH_HOST"],
        "port": int(config.get("SSH_PORT", 22)),
        "ssh_username": config["SSH_USER"],
        "ssh_pkey": config["SSH_KEY_PATH"],
        "remote_bind_address": ("127.0.0.1", int(config["SSH_REMOTE_DB_PORT"])),
    }


@contextmanager
def get_engine(config: dict | None = None):
    """
    Context manager que abre el tunel SSH hacia la base de datos
    remota y entrega (con "yield") un Engine de SQLAlchemy ya
    apuntando al puerto local del tunel.

    Si no se pasa un diccionario de configuracion explicito, se carga
    con load_config() desde el archivo .env del proyecto. Pasar un
    config explicito permite, por ejemplo, conectarse con el rol
    acotado frontal_sur_app en vez del rol por defecto, reemplazando
    DB_USER/DB_PASSWORD en una copia del diccionario antes de llamar
    a esta funcion.

    Al salir del bloque "with" (incluso si hubo una excepcion), el
    Engine se libera (dispose) y el tunel SSH se cierra solo, porque
    SSHTunnelForwarder tambien es un context manager.
    """
    if config is None:
        config = load_config()
    tunnel_kwargs = build_ssh_tunnel_kwargs(config)
    with SSHTunnelForwarder(
        (tunnel_kwargs["host"], tunnel_kwargs["port"]),
        ssh_username=tunnel_kwargs["ssh_username"],
        ssh_pkey=tunnel_kwargs["ssh_pkey"],
        remote_bind_address=tunnel_kwargs["remote_bind_address"],
    ) as tunnel:
        engine = create_engine(build_db_url(config, tunnel.local_bind_port))
        try:
            yield engine
        finally:
            engine.dispose()


def build_upsert_sql(
    table: str,
    cols: list[str],
    conflict_cols: list[str],
    geom_cols: dict[str, int] | None = None,
) -> str:
    """
    Arma el texto SQL de un INSERT ... ON CONFLICT ... DO UPDATE
    (upsert) para la tabla indicada, con placeholders con nombre
    (":col") pensados para bindear con SQLAlchemy.

    geom_cols es un diccionario opcional {nombre_columna: srid} para
    las columnas de tipo geometry: en vez de bindear el valor crudo,
    esas columnas se envuelven en ST_GeomFromText(:col, srid) para
    que Postgres/PostGIS las interprete como geometria (el valor
    bindeado debe venir como texto WKT).

    Las columnas que no estan en conflict_cols se actualizan en el
    DO UPDATE con el valor entrante (EXCLUDED.columna); las columnas
    de conflicto no se re-escriben porque son las que identifican la
    fila existente.
    """
    geom_cols = geom_cols or {}

    def placeholder(col: str) -> str:
        if col in geom_cols:
            return f"ST_GeomFromText(:{col}, {geom_cols[col]})"
        return f":{col}"

    col_list = ", ".join(cols)
    placeholders = ", ".join(placeholder(col) for col in cols)
    update_cols = [col for col in cols if col not in conflict_cols]
    update_clause = ", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)
    conflict_list = ", ".join(conflict_cols)
    return (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_list}) DO UPDATE SET {update_clause}"
    )


def upsert(
    engine,
    table: str,
    rows: list[dict],
    conflict_cols: list[str],
    geom_cols: dict[str, int] | None = None,
) -> int:
    """
    Ejecuta un upsert de "rows" contra "table" dentro de una unica
    transaccion (engine.begin() hace commit al final del bloque, o
    rollback si hay una excepcion). Devuelve la cantidad de filas
    procesadas.

    Todas las filas de "rows" deben tener las mismas columnas (se
    usan las claves de la primera fila para construir el SQL). Si
    "rows" viene vacia, no se ejecuta nada y se devuelve 0.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = build_upsert_sql(table, cols, conflict_cols, geom_cols)
    with engine.begin() as conn:
        conn.execute(text(sql), rows)
    return len(rows)