"""
Test plano (con assert, sin pytest) para _retry.py.

Correr con: .venv/bin/python backend/tests/test_retry.py

No usa red real: prueba el decorador contra funciones de prueba que
fallan un numero fijo de veces antes de tener exito, o que nunca
tienen exito, con un backoff casi instantaneo para que el test corra
rapido.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors._retry import retry


def test_retry_succeeds_after_failures():
    calls = {"count": 0}

    @retry(times=3, backoff_seconds=0.01)
    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise ValueError("fallo simulado")
        return "ok"

    assert flaky() == "ok"
    assert calls["count"] == 3


def test_retry_raises_after_exhausting_attempts():
    calls = {"count": 0}

    @retry(times=3, backoff_seconds=0.01)
    def always_fails():
        calls["count"] += 1
        raise ValueError("siempre falla")

    try:
        always_fails()
        assert False, "se esperaba que relanzara ValueError"
    except ValueError:
        pass
    assert calls["count"] == 3


def test_retry_does_not_retry_on_first_success():
    calls = {"count": 0}

    @retry(times=3, backoff_seconds=0.01)
    def succeeds_immediately():
        calls["count"] += 1
        return "ok"

    assert succeeds_immediately() == "ok"
    assert calls["count"] == 1


if __name__ == "__main__":
    test_retry_succeeds_after_failures()
    test_retry_raises_after_exhausting_attempts()
    test_retry_does_not_retry_on_first_success()
    print("OK: todos los tests de _retry.py pasaron")