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
from psycopg2.extras import execute_values
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


def app_role_config(config: dict | None = None) -> dict:
    """
    Devuelve una copia de la config (por defecto, la de load_config())
    con DB_USER/DB_PASSWORD reemplazados por DB_APP_USER/DB_APP_PASSWORD:
    el rol acotado frontal_sur_app que usa todo el pipeline y los
    scripts de management/, en vez del superusuario. Copia el
    diccionario en vez de mutar el que recibe, para no pisar la config
    original si el caller la sigue usando para otra cosa.
    """
    if config is None:
        config = load_config()
    resultado = dict(config)
    resultado["DB_USER"] = resultado["DB_APP_USER"]
    resultado["DB_PASSWORD"] = resultado["DB_APP_PASSWORD"]
    return resultado


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
    tunnel = SSHTunnelForwarder(
        (tunnel_kwargs["host"], tunnel_kwargs["port"]),
        ssh_username=tunnel_kwargs["ssh_username"],
        ssh_pkey=tunnel_kwargs["ssh_pkey"],
        remote_bind_address=tunnel_kwargs["remote_bind_address"],
    )
    # Los hilos del tunel se marcan daemon ANTES de start(): sin esto,
    # sshtunnel puede quedarse colgado indefinidamente en stop() si
    # alguna conexion del pool de SQLAlchemy sigue viva al cerrar
    # (comprobado empiricamente el 2026-07-17: un cierre tardo 17
    # minutos). Con hilos daemon, stop() no espera a esos forwarders.
    tunnel.daemon_forward_servers = True
    tunnel.daemon_transport = True
    tunnel.start()
    try:
        engine = create_engine(build_db_url(config, tunnel.local_bind_port))
        try:
            yield engine
        finally:
            engine.dispose()
    finally:
        tunnel.stop()


def build_upsert_sql(
    table: str,
    cols: list[str],
    conflict_cols: list[str],
    conflict_do_nothing: bool = False,
) -> str:
    """
    Arma el texto SQL de un INSERT ... ON CONFLICT ... (upsert) con un
    unico placeholder "VALUES %s", pensado para psycopg2.extras.
    execute_values: esa funcion reemplaza el "%s" por N tuplas de
    valores en un solo round-trip, en vez de una sentencia por fila
    (ver upsert() y build_upsert_template() para las columnas de
    geometria, que van en el template, no aqui).

    do_nothing=False (default): las columnas que no estan en
    conflict_cols se actualizan en el DO UPDATE con el valor entrante
    (EXCLUDED.columna); comportamiento historico, para tablas donde un
    reproceso legitimamente puede traer un valor corregido.

    do_nothing=True: ON CONFLICT DO NOTHING, ninguna fila existente se
    toca. Lo usan dga/dmc/agromet (ver backfill de estaciones): la fila
    ya escrita no se recalcula ni se reescribe, solo se insertan las
    que faltan, mas rapido que un DO UPDATE innecesario.
    """
    col_list = ", ".join(cols)
    conflict_list = ", ".join(conflict_cols)
    base = f"INSERT INTO {table} ({col_list}) VALUES %s ON CONFLICT ({conflict_list})"

    if conflict_do_nothing:
        return f"{base} DO NOTHING"

    update_cols = [col for col in cols if col not in conflict_cols]
    update_clause = ", ".join(f"{col} = EXCLUDED.{col}" for col in update_cols)
    return f"{base} DO UPDATE SET {update_clause}"


def build_upsert_template(cols: list[str], geom_cols: dict[str, int] | None = None) -> str:
    """
    Arma el "template" de una fila para execute_values: una tupla con
    un placeholder con nombre (%(col)s) por columna, en el mismo orden
    que build_upsert_sql. geom_cols es un diccionario opcional
    {nombre_columna: srid} para las columnas de tipo geometry: en vez
    de bindear el valor crudo, esas columnas se envuelven en
    ST_GeomFromText(%(col)s, srid) para que Postgres/PostGIS las
    interprete como geometria (el valor bindeado debe venir como texto
    WKT).
    """
    geom_cols = geom_cols or {}

    def placeholder(col: str) -> str:
        if col in geom_cols:
            return f"ST_GeomFromText(%({col})s, {geom_cols[col]})"
        return f"%({col})s"

    return "(" + ", ".join(placeholder(col) for col in cols) + ")"


def build_ids_con_datos_sql(table: str, fk_col: str) -> str:
    """
    Arma el SELECT que cuenta, por cada valor de fk_col, cuantos dias
    calendario DISTINTOS tienen al menos una fila en table dentro de
    una ventana [start, end). Separado de ids_con_datos() para poder
    testear el texto SQL sin abrir una conexion real (mismo patron que
    build_upsert_sql/upsert).
    """
    return (
        f"SELECT {fk_col}, count(DISTINCT date_trunc('day', momento)) "
        f"FROM {table} WHERE {fk_col} = ANY(:ids) "
        f"AND momento >= :start AND momento < :end GROUP BY {fk_col}"
    )


def ids_con_datos(engine, table: str, fk_col: str, ids: list[int], start, end) -> set:
    """
    Devuelve el subconjunto de "ids" que ya tienen cobertura COMPLETA
    (un dato en cada dia calendario) dentro de la ventana [start, end).

    No alcanza con "alguna fila en la ventana": si la ventana pedida
    fusiona el rango de un backfill puntual con el de la recoleccion
    rutinaria (que corre aparte, con su propia ventana), una estacion
    puede tener filas en la mitad de la ventana (la parte rutinaria) y
    seguir sin las del hueco real que el backfill queria llenar --
    "alguna fila" la marcaria como completa igual, un falso salteo
    (comprobado en vivo el 2026-07-20: 49 de 50 estaciones dmc con el
    hueco jun19-jul9 sin llenar quedaron marcadas "completas" por tener
    datos de jul10-19 de la rutina normal). Contar dias distintos y
    exigir que cubran TODOS los dias de la ventana detecta el hueco
    este donde este (al borde o en el medio), sin necesitar conocer la
    frecuencia de reporte exacta de cada fuente: todas reportan mucho
    mas seguido que 1 vez por dia, asi que un dia entero sin ninguna
    fila es una senal solida de que esa estacion no quedo completa.

    Asume ventanas alineadas a medianoche UTC (asi las arma ingest.py
    con --start/--end): dias_esperados = (end - start).days calza
    exacto con la cantidad de fechas calendario cubiertas.
    """
    if not ids:
        return set()
    dias_esperados = (end - start).days
    with engine.connect() as conn:
        rows = conn.execute(
            text(build_ids_con_datos_sql(table, fk_col)),
            {"ids": ids, "start": start, "end": end},
        ).fetchall()
    return {sid for sid, dias in rows if dias >= dias_esperados}


# Filas por round-trip en execute_values: suficiente para que un lote
# tipico (unos pocos miles de filas) entre en 1-3 round-trips en vez
# de uno por fila; tope para no mandar una sentencia gigante si algun
# batch fuera excepcionalmente grande.
# ponytail: constante fija, subir si algun caller manda batches >> 10k filas.
EXECUTE_VALUES_PAGE_SIZE = 1000


def upsert(
    engine,
    table: str,
    rows: list[dict],
    conflict_cols: list[str],
    geom_cols: dict[str, int] | None = None,
    do_nothing: bool = False,
) -> int:
    """
    Ejecuta un upsert de "rows" contra "table" en una unica
    transaccion, con psycopg2.extras.execute_values: agrupa las filas
    en tandas de EXECUTE_VALUES_PAGE_SIZE y manda cada tanda como un
    unico INSERT ... VALUES (...), (...), ... en vez de una sentencia
    por fila. Con el tunel SSH de por medio, cada round-trip cuesta
    ~200ms; un lote de 2880 filas pasaba de ~10 minutos (2880
    round-trips) a menos de 3 (2880/1000 redondeado hacia arriba).
    Devuelve la cantidad de filas procesadas.

    Todas las filas de "rows" deben tener las mismas columnas (se
    usan las claves de la primera fila para construir el SQL). Si
    "rows" viene vacia, no se ejecuta nada y se devuelve 0.

    do_nothing: ver build_upsert_sql. True para insert-only (no pisa
    filas ya existentes); False (default) mantiene el DO UPDATE de
    siempre.
    """
    if not rows:
        return 0
    cols = list(rows[0].keys())
    sql = build_upsert_sql(table, cols, conflict_cols, conflict_do_nothing=do_nothing)
    template = build_upsert_template(cols, geom_cols)
    raw_conn = engine.raw_connection()
    try:
        with raw_conn.cursor() as cur:
            execute_values(cur, sql, rows, template=template, page_size=EXECUTE_VALUES_PAGE_SIZE)
        raw_conn.commit()
    except Exception:
        raw_conn.rollback()
        raise
    finally:
        raw_conn.close()
    return len(rows)