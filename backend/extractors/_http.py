"""
Sesion HTTP compartida con reintentos y backoff automaticos, reusada
por los extractores que hacen llamadas HTTP planas (NOAA NCEI, Google
Flood Forecasting API cuando llegue el acceso). Usa
urllib3.util.retry.Retry, que ya viene instalado como dependencia
transitiva de "requests" (sin agregar ninguna libreria nueva): un solo
lugar centraliza esta logica en vez de duplicarla en cada extractor.
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def mount_retries(session: requests.Session, total_retries: int = 3,
                  backoff_factor: float = 2.0) -> requests.Session:
    """
    Monta en una Session existente el adaptador de reintentos con
    backoff exponencial sobre errores de conexion y codigos 429/5xx.
    Existe separado de build_session para que las Session subclase
    (como la de auth Earthdata en nasa_imerg.py) reciban la misma
    politica de reintentos.
    """
    retry_config = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    adapter = HTTPAdapter(max_retries=retry_config)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def build_session(total_retries: int = 3, backoff_factor: float = 2.0) -> requests.Session:
    """
    Arma una requests.Session nueva con la politica de reintentos de
    mount_retries. Se monta tanto para http:// como https://, ya que
    ambos esquemas pueden aparecer en URLs de configuracion o
    redirecciones.
    """
    return mount_retries(requests.Session(), total_retries, backoff_factor)