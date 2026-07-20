"""
Tests planos (con assert, sin pytest) para migrate.py.

Correr con: .venv/bin/python tests/test_migrate.py

Solo prueba pending_migrations, que es pura (recibe un directorio y un
set de nombres ya aplicados, y devuelve una lista ordenada de archivos
pendientes). No toca la base de datos: usa un directorio temporal
creado con tempfile, para no depender de los archivos reales de
migrations/.
"""

import sys
import tempfile
from pathlib import Path

# Agrega la raiz del proyecto al path de importacion. Se usa pathlib
# en vez de os.path para construir la ruta.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "management"))

from migrate import pending_migrations


def test_pending_migrations_skips_applied():
    # Con dos migraciones en disco y una ya marcada como aplicada,
    # pending_migrations debe devolver solo la que falta.
    with tempfile.TemporaryDirectory() as tmp_dir:
        migrations_dir = Path(tmp_dir)
        (migrations_dir / "0001_init.sql").write_text("-- init")
        (migrations_dir / "0002_add_col.sql").write_text("-- add col")
        result = pending_migrations(migrations_dir, applied={"0001_init.sql"})
        assert [path.name for path in result] == ["0002_add_col.sql"]


def test_pending_migrations_sorted_when_none_applied():
    # Los archivos se crean en disco en orden inverso (0002 antes que
    # 0001) para probar que pending_migrations los devuelve ordenados
    # por nombre y no por orden de creacion en el filesystem.
    with tempfile.TemporaryDirectory() as tmp_dir:
        migrations_dir = Path(tmp_dir)
        (migrations_dir / "0002_add_col.sql").write_text("-- add col")
        (migrations_dir / "0001_init.sql").write_text("-- init")
        result = pending_migrations(migrations_dir, applied=set())
        assert [path.name for path in result] == ["0001_init.sql", "0002_add_col.sql"]


if __name__ == "__main__":
    test_pending_migrations_skips_applied()
    test_pending_migrations_sorted_when_none_applied()
    print("OK: todos los tests de migrate.py pasaron")