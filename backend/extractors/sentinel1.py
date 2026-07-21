"""
Descarga de escenas Sentinel-1 nivel 1 desde el Copernicus Data Space
Ecosystem (CDSE), pensada para deteccion de cambios multitemporal
(Multitemporal Change Detection): comparar la misma zona en dos fechas
distintas requiere que ambas escenas vengan de la misma direccion de
orbita (ASCENDING/DESCENDING) y, idealmente, de la misma orbita
relativa, para que el angulo de mirada del radar no cambie entre una
imagen y otra.

Nivel 1 de Sentinel-1 son DOS tipos de producto, no uno solo:
GRD (Ground Range Detected, solo amplitud/backscatter, ya proyectado a
rango terreno) y SLC (Single Look Complex, amplitud + fase, en
geometria de rango oblicuo). Para change detection por backscatter
(el caso de este proyecto: comparar intensidad antes/despues de una
crecida) GRD es lo que corresponde y es el default de producto_tipo;
SLC queda disponible via producto_tipo="SLC" para cuando se necesite
interferometria (InSAR), que S1 GRD no permite por no traer fase.

A diferencia del resto de los extractores de este pipeline, esta
fuente no usa REGION_BBOX (el bbox gigante Coquimbo-Magallanes de todo
el proyecto): una escena pesa ~1-2 GB, asi que bajar el catalogo
completo del bbox grande no tiene sentido. Se pide explicitamente una
region (nombre de dpa_limites.dpa_region_subdere, el mismo catalogo de
limites administrativos ya usado para medir bboxes en vez de
inventarlos) y, opcionalmente, direccion de orbita y plataforma
(A/B/C/D; S1B esta fuera de servicio desde 2021 pero se deja como
opcion valida por si se pide historico).

Cada escena se descarga como zip y se descomprime de una vez en
data/sentinel1/<region>/<producto_tipo>/<orbita>/<plataforma>/<fecha>/
<escena>.SAFE/; el zip se borra despues de descomprimir para no
duplicar espacio en disco (sin comprimir pesa igual o mas que el zip).

A diferencia del resto de las fuentes de ingest.py, esta NO escribe
nada en la BD: no hay fila por escena en frontal_sur.frames_raster ni
en ninguna otra tabla (decision explicita, no un descuido: el uso de
esto es bajar escenas puntuales para procesarlas afuera del pipeline,
no llevar un catalogo persistente). La idempotencia entre corridas
(no bajar de nuevo una escena ya descargada) se resuelve solo con
Path.exists() sobre la carpeta de destino, no con una consulta a la
BD.
"""

import time
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile

import requests
from sqlalchemy import text

from db import load_config
from extractors._http import build_session
from extractors._retry import retry
from logutil import log

TOKEN_URL = "https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token"
SEARCH_URL = "https://catalogue.dataspace.copernicus.eu/odata/v1/Products"
DOWNLOAD_URL = "https://zipper.dataspace.copernicus.eu/odata/v1/Products({id})/$value"

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "sentinel1"

PLATAFORMAS_VALIDAS = {"A", "B", "C", "D"}
ORBITAS_VALIDAS = {"ASCENDING", "DESCENDING"}
TIPOS_VALIDOS = {"GRD", "SLC"}

# Margen de seguridad antes de que venza el token: se renueva un poco
# antes de los ~600 segundos reales que informa CDSE, para no arrancar
# una descarga de varios minutos con un token que vence a mitad de
# camino.
MARGEN_RENOVACION_SEGUNDOS = 30.0


class _TokenCDSE:
    """
    Envoltorio del access_token de CDSE con renovacion automatica. El
    token dura ~600 segundos (CDSE lo informa en "expires_in" en cada
    respuesta), muy poco comparado con lo que puede tardar una
    descarga de ~1-2 GB: sin esto, una corrida con varias escenas
    grandes terminaria pidiendo credenciales de nuevo a mitad de una
    descarga y perdiendo el intento. get() renueva solo, de forma
    perezosa, cuando el token esta por vencer; forzar_renovacion()
    se usa cuando el servidor ya devolvio 401 con el token vigente
    segun el reloj local (reloj desincronizado, revocacion manual,
    etc), para no quedar reintentando con un token que sabemos que ya
    no sirve.
    """

    def __init__(self, config: dict):
        self._config = config
        self._token: str | None = None
        self._vence_en = 0.0

    def get(self) -> str:
        if self._token is None or time.monotonic() >= self._vence_en - MARGEN_RENOVACION_SEGUNDOS:
            self._renovar()
        return self._token

    def forzar_renovacion(self) -> str:
        self._renovar()
        return self._token

    def _renovar(self) -> None:
        resp = requests.post(TOKEN_URL, data={
            "client_id": "cdse-public",
            "grant_type": "password",
            "username": self._config["COPERNICUS_USER"],
            "password": self._config["COPERNICUS_PASSWORD"],
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        self._token = data["access_token"]
        self._vence_en = time.monotonic() + data["expires_in"]


def _region_wkt(engine, region: str) -> str:
    """
    Trae el bbox real de la region (medido en la BD, no inventado)
    desde dpa_limites.dpa_region_subdere, como WKT rectangular para el
    filtro OData.CSC.Intersects de CDSE. Se usa el bbox (4 puntos) en
    vez del poligono real completo a proposito: el poligono
    administrativo de una region trae miles de vertices de detalle
    costero, y mandarlo entero en la URL de busqueda revento con
    SSLEOFError (la URL quedaba de varios cientos de KB). Una escena
    Sentinel-1 mide ~250x170 km, mucho mas grande que el margen extra
    que agrega el rectangulo sobre el poligono real, asi que el bbox
    no cambia que escenas aparecen como candidatas. Una region puede
    venir partida en varias filas (islas, enclaves): se unen con
    ST_Union antes de sacar el bbox.
    """
    with engine.connect() as conn:
        row = conn.execute(text("""
            with geo as (
                select ST_Union(geometria) as geom
                from dpa_limites.dpa_region_subdere
                where region = :region
            )
            select ST_XMin(geom), ST_YMin(geom), ST_XMax(geom), ST_YMax(geom)
            from geo
            where geom is not null
        """), {"region": region}).fetchone()
    if row is None:
        raise ValueError(f"region desconocida en dpa_limites.dpa_region_subdere: {region!r}")
    xmin, ymin, xmax, ymax = row
    return f"POLYGON(({xmin} {ymin},{xmax} {ymin},{xmax} {ymax},{xmin} {ymax},{xmin} {ymin}))"


def _patron_nombre(producto_tipo: str) -> str:
    """
    Substring del nombre de producto que distingue GRD de SLC en el
    catalogo CDSE: los GRD llevan "IW_GRDH_1S" o "IW_GRDM_1S" segun
    resolucion (ambos calzan con el substring corto "IW_GRD"); los SLC
    llevan "IW_SLC__1S". No hace falta diferenciar GRDH de GRDM aqui:
    en modo IW sobre tierra firme, CDSE practicamente solo publica
    GRDH.
    """
    return "IW_GRD" if producto_tipo == "GRD" else "IW_SLC"


def _solicitud_autenticada(session: requests.Session, url: str, params: dict | None,
                            token_mgr: "_TokenCDSE") -> requests.Response:
    """
    GET autenticado con reintento inmediato ante un 401: el token
    puede vencer aunque get() lo haya considerado vigente (reloj
    local desincronizado con el servidor, o el propio servidor lo
    invalido antes de tiempo). En ese caso se fuerza una renovacion y
    se reintenta una sola vez antes de dejar que la excepcion suba (el
    decorador @retry de las funciones que llaman a esto cubre el resto
    de las fallas transitorias de red).
    """
    resp = session.get(url, params=params, headers={"Authorization": f"Bearer {token_mgr.get()}"}, timeout=60)
    if resp.status_code == 401:
        resp = session.get(url, params=params,
                            headers={"Authorization": f"Bearer {token_mgr.forzar_renovacion()}"}, timeout=60)
    resp.raise_for_status()
    return resp


def _buscar_productos(token_mgr: _TokenCDSE, polygon_wkt: str, start: datetime, end: datetime,
                       orbit_pass: str | None, plataformas: set[str] | None,
                       producto_tipo: str) -> list[dict]:
    """
    Busca en el catalogo CDSE las escenas Sentinel-1 nivel 1 (GRD o
    SLC segun producto_tipo) que intersectan el poligono de la region
    en la ventana pedida. El filtro OData solo cubre coleccion,
    interseccion geografica, rango de fechas y tipo de producto: los
    duplicados "_COG" (mismo dato GRD, otro empaquetado en GeoTIFF) se
    descartan quedandonos con el .SAFE clasico, porque el codigo de
    descompresion asume esa estructura de carpetas. La direccion de
    orbita y la plataforma se filtran en Python leyendo los Attributes
    de cada producto (orbitDirection, platformSerialIdentifier): CDSE
    no los expone como columnas filtrables sin una sintaxis OData mas
    compleja de lo que vale la pena mantener aqui para dos filtros que
    solo restan resultados en memoria (el total tipico es de decenas
    de escenas por region-mes).
    """
    filtro = (
        "Collection/Name eq 'SENTINEL-1' and "
        f"OData.CSC.Intersects(area=geography'SRID=4326;{polygon_wkt}') and "
        f"ContentDate/Start gt {start:%Y-%m-%d}T00:00:00.000Z and "
        f"ContentDate/Start lt {end:%Y-%m-%d}T00:00:00.000Z and "
        f"contains(Name,'{_patron_nombre(producto_tipo)}') and not contains(Name,'_COG')"
    )
    session = build_session()
    productos = []
    url = SEARCH_URL
    params = {"$filter": filtro, "$top": 200, "$expand": "Attributes"}
    while url is not None:
        r = _solicitud_autenticada(session, url, params, token_mgr)
        data = r.json()
        productos.extend(data.get("value", []))
        url = data.get("@odata.nextLink")
        params = None  # el nextLink ya trae los parametros codificados

    seleccionados = []
    for p in productos:
        atributos = {a["Name"]: a["Value"] for a in p.get("Attributes", [])}
        plataforma = atributos.get("platformSerialIdentifier")
        direccion = atributos.get("orbitDirection")
        if orbit_pass is not None and direccion != orbit_pass:
            continue
        if plataformas is not None and plataforma not in plataformas:
            continue
        seleccionados.append({
            "id": p["Id"],
            "name": p["Name"],
            "sensing_start": datetime.fromisoformat(p["ContentDate"]["Start"].replace("Z", "+00:00")),
            "plataforma": plataforma,
            "orbit_pass": direccion,
            "relative_orbit": atributos.get("relativeOrbitNumber"),
            "footprint": p["GeoFootprint"],
            "size_bytes": p.get("ContentLength"),
        })
    return seleccionados


@retry(times=3, backoff_seconds=10.0, exceptions=(requests.RequestException,))
def _descargar_y_descomprimir(token_mgr: _TokenCDSE, producto: dict, carpeta: Path) -> Path:
    """
    Baja el zip del producto por streaming (no tiene sentido cargar a
    memoria un archivo de ~1-2 GB) y lo descomprime de una vez en
    "carpeta"; borra el zip despues de descomprimir para no duplicar
    el espacio en disco. Un 401 a mitad de la descarga fuerza una
    renovacion de token y reintenta la conexion (no el archivo ya
    escrito hasta ese punto, que se trunca y se vuelve a empezar); el
    resto de las fallas transitorias de red las cubre el @retry de
    esta funcion, que reintenta la descarga completa desde cero.
    """
    carpeta.mkdir(parents=True, exist_ok=True)
    zip_path = carpeta / f"{producto['name']}.zip"
    url = DOWNLOAD_URL.format(id=producto["id"])

    resp = requests.get(url, headers={"Authorization": f"Bearer {token_mgr.get()}"}, stream=True, timeout=120)
    if resp.status_code == 401:
        resp.close()
        resp = requests.get(url, headers={"Authorization": f"Bearer {token_mgr.forzar_renovacion()}"},
                             stream=True, timeout=120)
    resp.raise_for_status()
    try:
        with zip_path.open("wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                f.write(chunk)
    finally:
        resp.close()

    with ZipFile(zip_path) as zf:
        zf.extractall(carpeta)
    zip_path.unlink()
    return carpeta / producto["name"]


def fetch_batches(start: datetime, end: datetime, region: str, engine,
                   orbit_pass: str | None = None, plataformas: set[str] | None = None,
                   producto_tipo: str = "GRD", dry_run: bool = False) -> list:
    """
    Descarga y descomprime las escenas Sentinel-1 que calcen con los
    filtros, para deteccion de cambios multitemporal sobre "region"
    (nombre de dpa_limites.dpa_region_subdere). orbit_pass restringe a
    "ASCENDING"/"DESCENDING" (None trae ambas); plataformas restringe
    a un subconjunto de {"A","B","C","D"} (None trae todas);
    producto_tipo es "GRD" (default, backscatter/amplitud) o "SLC"
    (amplitud+fase, para InSAR).

    No devuelve datos para persistir: esta fuente no escribe en la
    BD (a diferencia de dga/dmc/agromet/chirps), solo deja las escenas
    descomprimidas en disco para que un script de procesamiento
    aparte (fuera de este pipeline) las use. Siempre devuelve una
    lista vacia; el valor de retorno solo existe para calzar con la
    forma que espera run() en ingest.py (list en vez de generador),
    que al ver una lista vacia no llama a upsert con nada real.

    dry_run=True corta ANTES de pedir el zip: en el resto del
    pipeline "--dry-run" solo salta el upsert a la BD (ver run() en
    ingest.py) y el fetch_fn igual corre entero, porque el efecto
    secundario de las demas fuentes es liviano. Ese criterio es
    peligroso aca: el efecto secundario de esta fuente es bajar 1-2 GB
    por escena, y un dry-run que de todos modos descarga todo deja de
    ser "dry". Por eso sentinel1 recibe su propio flag y lo respeta
    explicitamente.

    Antes de bajar cada escena se chequea si la carpeta ya existe en
    disco: si ya esta, se omite la descarga. Esa es toda la
    idempotencia entre corridas, no hay tabla en la BD que lleve el
    registro.
    """
    if orbit_pass is not None and orbit_pass not in ORBITAS_VALIDAS:
        raise ValueError(f"orbit_pass debe ser {sorted(ORBITAS_VALIDAS)} o None, se recibio {orbit_pass!r}")
    if plataformas is not None and not plataformas <= PLATAFORMAS_VALIDAS:
        raise ValueError(f"plataformas debe ser subconjunto de {sorted(PLATAFORMAS_VALIDAS)}, se recibio {plataformas!r}")
    if producto_tipo not in TIPOS_VALIDOS:
        raise ValueError(f"producto_tipo debe ser {sorted(TIPOS_VALIDOS)}, se recibio {producto_tipo!r}")

    config = load_config()
    polygon_wkt = _region_wkt(engine, region)
    token_mgr = _TokenCDSE(config)
    productos = _buscar_productos(token_mgr, polygon_wkt, start, end, orbit_pass, plataformas, producto_tipo)
    log(f"sentinel1: {len(productos)} escenas {producto_tipo} encontradas para {region} "
        f"({orbit_pass or 'ambas orbitas'}, plataformas {sorted(plataformas) if plataformas else 'todas'})")

    for p in productos:
        carpeta = DATA_DIR / region / producto_tipo / p["orbit_pass"] / p["plataforma"] / f"{p['sensing_start']:%Y%m%d}"
        safe_dir = carpeta / p["name"]
        tamano_gb = (p["size_bytes"] or 0) / 1e9
        if dry_run:
            log(f"sentinel1 {p['name']}: dry-run, NO se descarga ({tamano_gb:.2f} GB, "
                f"orbita relativa {p['relative_orbit']})")
        elif not safe_dir.exists():
            log(f"sentinel1 {p['name']}: descargando ({tamano_gb:.2f} GB, orbita relativa {p['relative_orbit']})")
            _descargar_y_descomprimir(token_mgr, p, carpeta)
            log(f"sentinel1 {p['name']}: descomprimido en {safe_dir}")
        else:
            log(f"sentinel1 {p['name']}: ya descargado, se omite")

    return []


def _demo() -> None:
    """
    Autochequeo minimo, sin red ni BD: valida las funciones puras
    (patron de nombre GRD/SLC) y la logica de renovacion perezosa de
    _TokenCDSE contra un servidor falso, para no depender de
    credenciales reales en cada corrida de tests.
    """
    assert _patron_nombre("GRD") == "IW_GRD"
    assert _patron_nombre("SLC") == "IW_SLC"

    class _TokenFalso(_TokenCDSE):
        def __init__(self):
            super().__init__({"COPERNICUS_USER": "x", "COPERNICUS_PASSWORD": "y"})
            self.renovaciones = 0

        def _renovar(self):
            self.renovaciones += 1
            self._token = f"token-{self.renovaciones}"
            self._vence_en = time.monotonic() + 600

    tok = _TokenFalso()
    primero = tok.get()
    assert tok.renovaciones == 1
    segundo = tok.get()
    assert segundo == primero and tok.renovaciones == 1  # no renueva de mas si sigue vigente
    tok._vence_en = time.monotonic()  # simula que esta por vencer
    tercero = tok.get()
    assert tercero != primero and tok.renovaciones == 2  # renueva sola al estar por vencer

    print("OK: sentinel1 autochequeo paso")


if __name__ == "__main__":
    _demo()