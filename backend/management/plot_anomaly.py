"""
Plots de la anomalia de precipitacion (frontal_sur.frames_raster,
variable chirps_precip_anomaly) para revisar visualmente el resultado
del ajuste de residuos IDW (ver anomaly.py). No es parte del pipeline
de ingesta: es una herramienta de inspeccion manual, sin escritura a
la base de datos.

Pensado para una audiencia sin conocimiento previo del tema: colormap
divergente ROJO-AZUL (rojo = menos lluvia que lo normal, azul = mas
lluvia que lo normal, blanco = normal), con el significado de los
colores escrito explicito en el propio plot en vez de asumir que quien
lo mira ya sabe leer un mapa de anomalias. Azul/rojo se elige en vez
del BrBG verde/marron tradicional en climatologia porque "azul =
agua/lluvia" y "rojo = seco" son asociaciones mas inmediatas para
alguien sin formacion tecnica (BrBG es el estandar en papers de WMO/
NOAA, pero asume a un lector ya familiarizado con esa convencion).

Uso:
    .venv/bin/python backend/management/plot_anomaly.py <directorio_salida>
"""

import json
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib.pyplot as plt
import numpy as np
import rasterio
from matplotlib.colors import TwoSlopeNorm
from rasterio.crs import CRS
from rasterio.features import geometry_mask
from rasterio.transform import array_bounds
from rasterio.warp import Resampling, calculate_default_transform, reproject
from sqlalchemy import text

from db import app_role_config, get_engine
from extractors.nasa_imerg import CHOROPLETH_REGION_IDS

ANOMALY_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "chirps_anomaly"

# Tipografia mas grande y legible que el default de matplotlib (11 en
# vez de 10, sans-serif en vez del serif de algunos backends): el plot
# esta pensado para mostrarse a alguien sin contexto tecnico, no solo
# para uso interno de quien lo genera.
plt.rcParams.update({
    "font.size": 11,
    "font.family": "sans-serif",
    "axes.titlesize": 12,
})

COLOR_SUPERAVIT = "#2166ac"  # azul, extremo alto del colormap RdBu
COLOR_DEFICIT = "#b2182b"    # rojo, extremo bajo del colormap RdBu

# Metodologia en una linea para el pie de cada plot: quien lo mire sin
# contexto (ej. exportado suelto a un reporte) sabe de donde sale el
# numero sin tener que ir a buscar anomaly.py ni el paper. Es la unica
# linea con jerga tecnica a proposito (IDW, CHIRPS): va chica y gris,
# como nota al pie, no como el mensaje principal del plot.
METODOLOGIA = (
    "Metodologia (tecnico): anomalia = precipitacion observada (estaciones DGA/DMC/Agromet + CHIRPS prelim) "
    "menos climatologia CHIRPS 1998-2025 del mismo dia, ajustada por residuos IDW "
    "(Ossa-Moreno et al. 2019, HESS)."
)


def load_anomaly_days(anomaly_dir: Path = ANOMALY_DIR) -> list[tuple[str, np.ndarray, tuple]]:
    """
    Lee todos los GeoTIFF de anomaly_dir, ordenados por fecha, y
    devuelve una lista de (fecha_iso, banda, transform). transform es
    el rasterio.Affine original: se necesita entero (no solo bounds)
    para poder generar mascaras de geometria por region.
    """
    dias = []
    for tif_path in sorted(anomaly_dir.glob("*.tif")):
        with rasterio.open(tif_path) as src:
            band = src.read(1).astype("float64")
            transform = src.transform
        fecha = tif_path.stem[:8]
        fecha_iso = f"{fecha[:4]}-{fecha[4:6]}-{fecha[6:8]}"
        dias.append((fecha_iso, band, transform))
    return dias


def _extent_from_transform(band: np.ndarray, transform) -> tuple:
    height, width = band.shape
    left, top = transform * (0, 0)
    right, bottom = transform * (width, height)
    return (left, right, bottom, top)


def load_region_geometries(engine) -> list[tuple[int, str, dict]]:
    """
    Trae id, nombre y geometria (GeoJSON) de las regiones del bbox del
    proyecto, en el mismo orden norte-sur de CHOROPLETH_REGION_IDS
    (extractors/nasa_imerg.py, ya corregido para cubrir Coquimbo a
    Magallanes). Reusar ese orden evita mantener una segunda lista de
    regiones que se puede desincronizar de la real.
    """
    with engine.connect() as conn:
        filas = conn.execute(text("""
            SELECT region_id, region, ST_AsGeoJSON(geometria),
                   ST_XMin(geometria), ST_YMin(geometria), ST_XMax(geometria), ST_YMax(geometria)
            FROM dpa_limites.dpa_region_subdere
            WHERE region_id = ANY(:ids)
        """), {"ids": list(CHOROPLETH_REGION_IDS)}).fetchall()
    por_id = {
        region_id: (nombre, json.loads(geojson), (xmin, ymin, xmax, ymax))
        for region_id, nombre, geojson, xmin, ymin, xmax, ymax in filas
    }
    return [(rid, *por_id[rid]) for rid in CHOROPLETH_REGION_IDS if rid in por_id]


def mask_to_region(band: np.ndarray, transform, geometry: dict) -> np.ndarray:
    """
    Copia de band con NaN en todo pixel fuera de geometry (mismo
    geometry_mask de rasterio que usa
    extractors/nasa_imerg.py::fetch_choropleth). Sin esto, recortar
    solo al bbox de una region angosta (ej. Nuble) mostraria de rebote
    pixeles de la region vecina que caen dentro del mismo rectangulo.
    """
    mask = geometry_mask([geometry], out_shape=band.shape, transform=transform,
                         invert=True, all_touched=True)
    recortado = band.copy()
    recortado[~mask] = np.nan
    return recortado


def crop_to_bbox(band: np.ndarray, transform, bbox: tuple, margin_deg: float = 0.3):
    """
    Recorta banda y transform al bbox (xmin, ymin, xmax, ymax) en
    lon/lat, con un margen chico de contexto. Usa la transform inversa
    de rasterio para ubicar filas/columnas en vez de asumir una grilla
    regular alineada a los ejes.
    """
    xmin, ymin, xmax, ymax = bbox
    xmin, xmax = xmin - margin_deg, xmax + margin_deg
    ymin, ymax = ymin - margin_deg, ymax + margin_deg
    inversa = ~transform
    col0, row0 = inversa * (xmin, ymax)
    col1, row1 = inversa * (xmax, ymin)
    f0, f1 = sorted((int(row0), int(row1) + 1))
    c0, c1 = sorted((int(col0), int(col1) + 1))
    f0, c0 = max(0, f0), max(0, c0)
    f1, c1 = min(band.shape[0], f1), min(band.shape[1], c1)

    recortada = band[f0:f1, c0:c1]
    nuevo_transform = transform * rasterio.Affine.translation(c0, f0)
    return recortada, nuevo_transform


def _utm_epsg(centroid_lon: float) -> int:
    """
    EPSG de UTM WGS84 hemisferio sur (32700 + zona) para la zona que
    contiene centroid_lon. Chile entero cae en el hemisferio sur, asi
    que solo la longitud decide la zona (6 grados cada una). Cada
    region usa SU PROPIA zona UTM (centrada en su propio centroide) en
    vez de forzar una unica zona para todo el pais: con 12 regiones de
    Coquimbo a Magallanes, una sola zona UTM estiraria fuerte a las mas
    lejanas de su meridiano central.
    """
    zona = int((centroid_lon + 180) / 6) + 1
    return 32700 + zona


def reproject_to_utm(band: np.ndarray, transform, centroid_lon: float, src_crs: str = "EPSG:4326"):
    """
    Reproyecta band (en grados, EPSG:4326) a la zona UTM de
    centroid_lon: en vez de un aspect ratio aproximado (1/cos(lat)),
    una reproyeccion real deja la grilla en metros, isotropica en x/y,
    asi que un circulo en el terreno se ve circulo en el mapa (aspect
    "equal" ya es correcto, no una correccion aproximada).
    """
    dst_crs = CRS.from_epsg(_utm_epsg(centroid_lon))
    height, width = band.shape
    left, bottom, right, top = array_bounds(height, width, transform)
    dst_transform, dst_width, dst_height = calculate_default_transform(
        src_crs, dst_crs, width, height, left, bottom, right, top,
    )
    destino = np.full((dst_height, dst_width), np.nan, dtype="float64")
    reproject(
        source=band, destination=destino,
        src_transform=transform, src_crs=src_crs,
        dst_transform=dst_transform, dst_crs=dst_crs,
        resampling=Resampling.bilinear, src_nodata=np.nan, dst_nodata=np.nan,
    )
    return destino, dst_transform


def intro_text(fechas: list[str]) -> str:
    """
    Explica, en una oracion sin jerga, que esta comparando cada mapa y
    por que la serie es justo esa (dinamica: toma las fechas reales de
    los GeoTIFF disponibles, no una fecha fija hardcodeada) en vez de
    asumir que quien lee ya entiende que es una "climatologia" o por
    que el ultimo dia disponible no es hoy.
    """
    return (
        "Cada mapa compara la lluvia real de ese dia contra el promedio historico de esa\n"
        f"misma fecha en los ultimos 28 anios (1998-2025). Dias mostrados: {fechas[0]} al {fechas[-1]}\n"
        "(los mas recientes con datos satelitales disponibles al momento de este analisis)."
    )


def region_summary(nombre: str, recortados: list[tuple[str, np.ndarray, tuple]]) -> str:
    """
    Una frase ejecutiva por region, en lenguaje simple (sin "promedio
    del periodo" ni signos +/-), a partir de la propia serie de medias
    diarias: si el promedio espacial cambio de signo durante el
    periodo (de menos lluvia a mas lluvia o al reves) lo dice explicito
    con la fecha del cambio; si se mantuvo del mismo signo todo el
    periodo, reporta cuanto en promedio. Nada inventado: son los
    mismos numeros que se grafican, en una oracion.
    """
    con_dato = [(f, b) for f, b, _ in recortados if np.isfinite(b).any()]
    if not con_dato:
        return "Sin datos suficientes para esta region en el periodo mostrado."
    fechas = [f for f, _ in con_dato]
    medias = [float(np.nanmean(b)) for _, b in con_dato]
    mitad = max(1, len(medias) // 2)
    inicio, fin = np.mean(medias[:mitad]), np.mean(medias[mitad:])
    promedio = np.mean(medias)
    if inicio < 0 < fin:
        return (f"Empezo mas seco de lo normal, pero desde el {fechas[-1]} paso a llover mas: "
                f"en promedio {abs(promedio):.0f} mm mas que lo esperado para estas fechas.")
    if fin < 0 < inicio:
        return (f"Empezo mas lluvioso de lo normal, pero desde el {fechas[-1]} paso a llover menos: "
                f"en promedio {abs(promedio):.0f} mm menos que lo esperado para estas fechas.")
    if promedio > 0:
        return f"Llovio mas de lo normal en todo el periodo: en promedio {promedio:.0f} mm de mas para estas fechas."
    return f"Llovio menos de lo normal en todo el periodo: en promedio {abs(promedio):.0f} mm de menos para estas fechas."


def _slug(nombre: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", nombre)
    sin_tildes = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return sin_tildes.lower().replace(" ", "_").replace("'", "")


def plot_one_region(nombre: str, geometry: dict, bbox: tuple, dias: list[tuple[str, np.ndarray, tuple]]):
    """
    Una sola figura por region: un panel por dia (recortado al bbox de
    la region, enmascarado para no mostrar territorio vecino, y
    reproyectado a la zona UTM de esa region para que el mapa salga
    proporcionado, no estirado por trabajar en grados), en 2 filas de
    subplots. Escala de color propia de esta region (percentiles 2/98
    de sus propios dias) para que regiones con anomalias chicas no
    salgan aplastadas por la escala de una region con anomalias
    grandes. El pie repite, en una linea, la metodologia y un resumen
    ejecutivo de la propia serie de esta region.
    """
    centroid_lon = (bbox[0] + bbox[2]) / 2
    recortados = [
        (fecha_iso, *reproject_to_utm(
            *crop_to_bbox(mask_to_region(band, transform, geometry), transform, bbox),
            centroid_lon,
        ))
        for fecha_iso, band, transform in dias
    ]
    finite_all = np.concatenate([b[np.isfinite(b)] for _, b, _ in recortados if np.isfinite(b).any()])
    p_bajo, p_alto = np.percentile(finite_all, [2, 98])
    norm = TwoSlopeNorm(vmin=min(p_bajo, -0.1), vcenter=0, vmax=max(p_alto, 0.1))

    cols = 3
    rows = -(-len(recortados) // cols)
    fig, axes = plt.subplots(rows, cols, figsize=(3.3 * cols, 3.9 * rows))
    axes = np.atleast_2d(axes)
    im = None
    for idx, (fecha_iso, band, transform) in enumerate(recortados):
        ax = axes[idx // cols, idx % cols]
        extent = _extent_from_transform(band, transform)
        im = ax.imshow(band, extent=extent, origin="upper", cmap="RdBu", norm=norm, aspect="equal")
        ax.set_title(fecha_iso, fontsize=10.5, pad=6)
        ax.set_xticks([])
        ax.set_yticks([])
    for idx in range(len(recortados), rows * cols):
        axes[idx // cols, idx % cols].axis("off")

    fechas = [f for f, _, _ in recortados]
    fig.suptitle(f"¿Llovio mas o menos de lo normal en {nombre}?", fontsize=14, y=0.97, fontweight="bold")

    # Margenes reservados ANTES de crear el eje del colorbar: llamar
    # colorbar() con ax=... reacomoda los subplots solo, y si despues
    # se llama subplots_adjust() ese reacomodo se pisa (el bug real que
    # hizo que el colorbar quedara encima de la columna derecha). Con
    # un eje propio (cax) en el margen ya reservado, ninguno de los dos
    # pasos mueve al otro.
    fig.subplots_adjust(top=0.86, bottom=0.24, right=0.85, wspace=0.06, hspace=0.32)
    cbar_ax = fig.add_axes((0.88, 0.30, 0.02, 0.48))
    cbar = fig.colorbar(im, cax=cbar_ax, extend="both")
    cbar.set_label("Diferencia respecto a lo normal (mm)", fontsize=8.5)
    # Significado de cada color, escrito explicito arriba y abajo del
    # colorbar: no asumir que quien mira el plot ya sabe que "azul"
    # significa superavit en esta convencion (es la convencion que ESTE
    # script eligio, no un estandar universal).
    cbar_ax.text(0.5, 1.05, "MAS lluvia\nque lo normal", transform=cbar_ax.transAxes,
                ha="center", va="bottom", fontsize=8.5, color=COLOR_SUPERAVIT, fontweight="bold")
    cbar_ax.text(0.5, -0.05, "MENOS lluvia\nque lo normal", transform=cbar_ax.transAxes,
                ha="center", va="top", fontsize=8.5, color=COLOR_DEFICIT, fontweight="bold")

    fig.text(0.5, 0.155, intro_text(fechas), ha="center", va="top", fontsize=9)
    fig.text(0.5, 0.075, region_summary(nombre, recortados), ha="center", va="top",
            fontsize=10, style="italic")
    fig.text(0.5, 0.015, METODOLOGIA, ha="center", va="top", fontsize=7, color="dimgray", wrap=True)
    return fig


def main(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    dias = load_anomaly_days()
    if not dias:
        print(f"sin GeoTIFF de anomalia en {ANOMALY_DIR}")
        return

    with get_engine(app_role_config()) as engine:
        regiones = load_region_geometries(engine)

    for _, nombre, geometry, bbox in regiones:
        plot_one_region(nombre, geometry, bbox, dias)
        plt.savefig(out_dir / f"anomaly_region_{_slug(nombre)}.png", dpi=150, bbox_inches="tight")
        plt.close("all")

    print(f"{len(regiones)} plots (uno por region, {len(dias)} dias cada uno) en {out_dir}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/anomaly_plots"))
