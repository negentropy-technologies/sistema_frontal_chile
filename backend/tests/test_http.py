"""
Test plano (con assert, sin pytest) para _http.py.

Correr con: .venv/bin/python backend/tests/test_http.py

No abre conexiones reales: solo verifica que build_session arma una
Session con el adaptador de reintentos montado y la configuracion de
reintentos esperada (total, backoff_factor, codigos de estado sobre
los que reintenta).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from extractors._http import build_session


def test_build_session_mounts_retry_adapter():
    session = build_session(total_retries=5, backoff_factor=1.5)
    adapter = session.get_adapter("https://example.com")
    assert adapter.max_retries.total == 5
    assert adapter.max_retries.backoff_factor == 1.5
    assert 429 in adapter.max_retries.status_forcelist
    assert 503 in adapter.max_retries.status_forcelist


if __name__ == "__main__":
    test_build_session_mounts_retry_adapter()
    print("OK: todos los tests de _http.py pasaron")