"""
Runner minimo de migraciones para el esquema frontal_sur.

No usa ningun framework de migraciones tipo ORM (Alembic, etc): las
migraciones son archivos .sql numerados en migrations/, aplicados en
orden por su nombre, y registrados en la propia base de datos, en la
tabla frontal_sur.schema_migrations. Correrlo dos veces sin cambios en
migrations/ no hace nada la segunda vez (idempotente).

Correr con: .venv/bin/python backend/management/migrate.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from db import get_engine

# Directorio con los archivos .sql de migracion, relativo a este
# archivo (no al directorio de trabajo actual).
MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def pending_migrations(migrations_dir: Path, applied: set[str]) -> list[Path]:
    """
    Devuelve, ordenados por nombre de archivo, los archivos .sql de
    migrations_dir cuyo nombre no esta en el set "applied". El orden
    por nombre es lo que garantiza que 0001_init.sql se aplique antes
    que 0002_algo.sql, etc.
    """
    return [path for path in sorted(migrations_dir.glob("*.sql")) if path.name not in applied]


def run() -> None:
    """
    Abre el tunel SSH y una conexion a la base de datos (via
    db.get_engine), se asegura de que exista el esquema frontal_sur y
    la tabla de control schema_migrations, calcula que migraciones
    faltan por aplicar, y las aplica una por una dentro de la misma
    transaccion, insertando un registro en schema_migrations por cada
    una que se aplica con exito.
    """
    with get_engine() as engine:
        with engine.begin() as conn:
            conn.execute(text("CREATE SCHEMA IF NOT EXISTS frontal_sur"))
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS frontal_sur.schema_migrations "
                    "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
                )
            )
            applied_rows = conn.execute(
                text("SELECT filename FROM frontal_sur.schema_migrations")
            ).fetchall()
            applied = {row[0] for row in applied_rows}
            pending = pending_migrations(MIGRATIONS_DIR, applied)
            if not pending:
                print("sin migraciones pendientes")
            for path in pending:
                print(f"aplicando {path.name}")
                conn.execute(text(path.read_text()))
                conn.execute(
                    text("INSERT INTO frontal_sur.schema_migrations (filename) VALUES (:filename)"),
                    {"filename": path.name},
                )


if __name__ == "__main__":
    run()