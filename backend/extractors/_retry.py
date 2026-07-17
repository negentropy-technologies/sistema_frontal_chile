"""
Decorador generico de reintentos con backoff exponencial, en stdlib
puro (sin agregar ninguna libreria nueva). Se usa para envolver
llamadas a la API de Google Earth Engine (ee.data.*, reduceRegions),
que no trae su propio mecanismo de reintentos configurable.
"""

import time
from functools import wraps


def retry(times: int = 3, backoff_seconds: float = 2.0, exceptions: tuple = (Exception,)):
    """
    Devuelve un decorador que reintenta la funcion envuelta hasta
    "times" veces si lanza alguna de las excepciones en "exceptions".
    Entre cada intento (salvo el ultimo) espera
    "backoff_seconds * (2 ** intento)" segundos: backoff exponencial,
    para no seguir golpeando un servicio que ya esta fallando. Si se
    agotan los intentos, relanza la ultima excepcion capturada tal
    cual (no la envuelve), para no perder el traceback original.
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(times):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt < times - 1:
                        time.sleep(backoff_seconds * (2 ** attempt))
            raise last_exc
        return wrapper
    return decorator