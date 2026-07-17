"""
Crea (o actualiza) el rol de base de datos acotado frontal_sur_app,
pensado para que lo use el pipeline de ingesta automatizado en vez del
superusuario "postgres" que se usa hoy para migraciones puntuales
hechas a mano.

Este rol solo puede leer y escribir en el esquema frontal_sur, mas
hacer SELECT sobre dpa_limites.dpa_comuna_subdere (la tabla de comunas
que necesita el FK de choropleth_stats). No tiene ningun permiso sobre
el resto de los esquemas de esta base de datos compartida (aculeo_*,
negentropy_market, cadastrai, cuencas, etc).

Un rol de solo lectura para servir datos a un API/Martin (webmapping)
no esta cubierto aca: es un plan separado, todavia no escrito.

Correr con: .venv/bin/python create_role.py <password>
"""

import sys

from sqlalchemy import text

from db import get_engine


def frontal_sur_app_role_sql(password: str) -> str:
    """
    Arma el DDL completo para crear (si no existe) el rol
    frontal_sur_app con la password recibida, y otorgarle los permisos
    acotados descritos arriba. Es una funcion pura: no toca la base de
    datos, solo devuelve texto SQL, para poder testearla sin conexion
    real (ver tests/test_role.py).

    El CREATE ROLE va envuelto en un bloque DO...END con un chequeo en
    pg_roles para que correr esta funcion mas de una vez no falle con
    "el rol ya existe" (idempotente en cuanto a la creacion; los GRANT
    de mas abajo ya son idempotentes por naturaleza, GRANT no falla si
    el permiso ya estaba otorgado).
    """
    return f"""
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'frontal_sur_app') THEN
        CREATE ROLE frontal_sur_app LOGIN PASSWORD '{password}';
    END IF;
END
$$;

GRANT ALL ON SCHEMA frontal_sur TO frontal_sur_app;
GRANT ALL ON ALL TABLES IN SCHEMA frontal_sur TO frontal_sur_app;
GRANT ALL ON ALL SEQUENCES IN SCHEMA frontal_sur TO frontal_sur_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA frontal_sur GRANT ALL ON TABLES TO frontal_sur_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA frontal_sur GRANT ALL ON SEQUENCES TO frontal_sur_app;

GRANT USAGE ON SCHEMA dpa_limites TO frontal_sur_app;
GRANT SELECT ON dpa_limites.dpa_comuna_subdere TO frontal_sur_app;
-- La agregacion por comuna (extractors/gee.py, fetch_choropleth)
-- necesita el join comuna -> provincia para filtrar por region,
-- porque dpa_comuna_subdere no trae la region directamente.
GRANT SELECT ON dpa_limites.dpa_provincia_subdere TO frontal_sur_app;
"""


def run(password: str) -> None:
    """
    Abre una conexion con el rol actual (superusuario, via
    db.get_engine) y ejecuta el DDL de frontal_sur_app_role_sql dentro
    de una transaccion, creando o actualizando el rol acotado en la
    base de datos real.
    """
    with get_engine() as engine:
        with engine.begin() as conn:
            conn.execute(text(frontal_sur_app_role_sql(password)))
    print("rol frontal_sur_app creado/actualizado")


if __name__ == "__main__":
    run(sys.argv[1])