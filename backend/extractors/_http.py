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


def build_session(total_retries: int = 3, backoff_factor: float = 2.0) -> requests.Session:
    """
    Arma una requests.Session que reintenta automaticamente sobre
    errores de conexion y sobre codigos de estado 429 (rate limit) y
    5xx (error del servidor), con backoff exponencial
    (backoff_factor * (2 ** intento)) entre cada intento. Se monta
    tanto para http:// como https://, ya que ambos esquemas pueden
    aparecer en URLs de configuracion o redirecciones.
    """
    retry_config = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry_config)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session