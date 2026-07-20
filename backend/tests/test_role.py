"""
Test plano (con assert, sin pytest) para create_role.py.

Correr con: .venv/bin/python tests/test_role.py

Solo prueba frontal_sur_app_role_sql, que es pura: arma el texto DDL
del rol acotado sin tocar la base de datos. La parte que si pega
contra la BD real (create_role.run) se corre a mano en el Paso 5 del
plan, no aca.
"""

import sys
from pathlib import Path

# Agrega la raiz del proyecto al path de importacion, usando pathlib
# en vez de os.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "management"))

from create_role import frontal_sur_app_role_sql


def test_role_sql_contains_grants_scoped_to_frontal_sur():
    sql = frontal_sur_app_role_sql("hunter2")
    # El rol debe crearse con la password recibida, y sus permisos
    # (GRANT) deben limitarse al esquema frontal_sur mas el SELECT
    # puntual sobre la tabla de comunas que necesita el FK.
    assert "CREATE ROLE frontal_sur_app" in sql
    assert "'hunter2'" in sql
    assert "GRANT ALL ON SCHEMA frontal_sur TO frontal_sur_app" in sql
    assert "GRANT SELECT ON dpa_limites.dpa_comuna_subdere TO frontal_sur_app" in sql
    # El rol nunca debe recibir permisos sobre otros esquemas del
    # tenant compartido (aculeo_*, negentropy_market, etc): esta es
    # la garantia de aislamiento que pide el spec de seguridad.
    assert "aculeo" not in sql
    assert "negentropy_market" not in sql


if __name__ == "__main__":
    test_role_sql_contains_grants_scoped_to_frontal_sur()
    print("OK: todos los tests de create_role.py pasaron")