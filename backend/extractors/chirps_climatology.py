"""
Climatologia historica de precipitacion CHIRPS para el calculo de
anomalias (ver docs/superpowers/plans/2026-07-19-anomalia-climatica-v1.md).

Metodologia: el ajuste de residuos de Ossa-Moreno et al. (2019, HESS,
https://doi.org/10.5194/hess-23-4763-2019) necesita un campo de fondo
"normal" contra el que restar las observaciones de estacion. WMO-No.
1203 ("WMO Guidelines on the Calculation of Climate Normals") exige
minimo 10 anios consecutivos para que ese normal sea defendible; este
modulo no calcula nada con menos anios de los que CLIMATOLOGY_LAST_YEAR
- CLIMATOLOGY_FIRST_YEAR + 1 alcance.

Fuente elegida (verificado en vivo el 2026-07-19): data.chc.ucsb.edu
-- el MISMO servidor que ya usa extractors/chirps.py para la rama
"prelim/sat" del evento actual -- publica tambien la rama "final/sat"
con historia desde 1998 (el minimo entre CHIRPS-v3 y la disponibilidad
de IMERG, que es el dato usado para desagregar a diario, ver
data.chc.ucsb.edu/.../daily/final/readme.txt). Usar la MISMA
identificacion "sat" (IMERG) que la rama prelim evita inhomogeneidad
de metodo de desagregacion entre el evento actual y su climatologia.

Se prefiere esta API directa del CHC sobre Google Earth Engine
(se verifico en vivo que UCSB-CHC/CHIRPS/V3/DAILY_SAT tambien existe
en GEE con el mismo rango de anios) porque el proyecto prioriza la API
del emisor original del dato salvo que no alcance en cobertura o
resolucion -- mismo criterio que ya documentan extractors/chirps.py y
extractors/gee.py.
"""

from datetime import datetime
from pathlib import Path

import numpy as np
import rasterio
from rasterio.windows import from_bounds

from extractors._http import build_session
from extractors._retry import retry
from logutil import log

CHC_FINAL_BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS/v3.0/daily/final/sat"

# Verificado en vivo el 2026-07-19 contra data.chc.ucsb.edu: 1998 es
# el primer anio con archivos reales en la rama final/sat (arranca con
# IMERG, ver docstring del modulo); 2025 es el ultimo anio calendario
# completo (2026 esta en curso y ademas es el propio periodo de la
# anomalia que se quiere medir, no debe formar parte de su propia
# climatologia).
CLIMATOLOGY_FIRST_YEAR = 1998
CLIMATOLOGY_LAST_YEAR = 2025
MIN_CLIMATOLOGY_YEARS = 10

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "chirps_climatology"


def validar_years_disponibles(first_year: int, last_year: int) -> None:
    """
    Lanza ValueError si el rango de anios no alcanza el minimo WMO de
    10 anios para un "period average" valido (WMO-No. 1203). No se
    calcula ninguna climatologia con menos de esto: seria un normal no
    defendible.
    """
    years = last_year - first_year + 1
    if years < MIN_CLIMATOLOGY_YEARS:
        raise ValueError(
            f"solo {years} anios disponibles ({first_year}-{last_year}), "
            f"WMO-No. 1203 exige un minimo de {MIN_CLIMATOLOGY_YEARS}"
        )


def _day_url(year: int, month: int, day: int) -> str:
    return f"{CHC_FINAL_BASE_URL}/{year}/chirps-v3.0.sat.{year}.{month:02d}.{day:02d}.tif"


@retry(times=3, backoff_seconds=2.0, exceptions=(rasterio.errors.RasterioIOError,))
def _crop_day(url: str, bbox: tuple, dest_path: Path) -> None:
    """
    Recorta por rango HTTP (vsicurl) solo la ventana del bbox del
    GeoTIFF global remoto, igual que extractors/chirps.py::_crop_day.
    Duplicado deliberadamente (no importado desde chirps.py): son
    modulos de fuentes distintas (prelim vs final) que el proyecto
    mantiene desacoplados, mismo criterio que dga/dmc/agromet con
    _skip_to_resume.
    """
    xmin, ymin, xmax, ymax = bbox
    with rasterio.open(url) as src:
        window = from_bounds(xmin, ymin, xmax, ymax, src.transform)
        data = src.read(1, window=window).astype("float32")
        nodata = src.nodata if src.nodata is not None else -9999.0
        data[data == nodata] = np.nan
        profile = src.profile.copy()
        profile.update(
            width=data.shape[1],
            height=data.shape[0],
            transform=src.window_transform(window),
            dtype="float32",
            nodata=np.nan,
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(dest_path, "w", **profile) as dst:
        dst.write(data, 1)


def climatology_for_dayofyear(
    bbox: tuple,
    month: int,
    day: int,
    first_year: int = CLIMATOLOGY_FIRST_YEAR,
    last_year: int = CLIMATOLOGY_LAST_YEAR,
    session=None,
) -> tuple[np.ndarray, "rasterio.Affine"]:
    """
    Promedio pixel a pixel de la precipitacion CHIRPS final/sat del
    dia calendario (month, day) a traves de [first_year, last_year],
    mas el transform georreferenciado de la grilla (identico entre
    anios: mismo bbox recortado del mismo grid CHIRPS nativo). Un anio
    sin archivo publicado (404, ej: 29 de febrero en anio no bisiesto)
    se descarta del promedio en vez de fallar. Los recortes ya
    descargados no se vuelven a bajar.
    """
    validar_years_disponibles(first_year, last_year)
    session = session or build_session()
    capas = []
    transform = None
    for year in range(first_year, last_year + 1):
        try:
            datetime(year, month, day)
        except ValueError:
            continue  # 29 feb en anio no bisiesto: ese archivo no existe
        dest_path = DATA_DIR / f"{month:02d}{day:02d}" / f"{year}.tif"
        if not dest_path.exists():
            url = _day_url(year, month, day)
            if session.head(url, timeout=20).status_code == 404:
                log(f"chirps_climatology {year}-{month:02d}-{day:02d}: no publicado, se salta")
                continue
            _crop_day(url, bbox, dest_path)
        with rasterio.open(dest_path) as src:
            capas.append(src.read(1).astype("float64"))
            transform = src.transform
    if not capas:
        raise ValueError(f"sin datos de climatologia para {month:02d}-{day:02d} en {first_year}-{last_year}")
    return np.nanmean(np.stack(capas), axis=0), transform
