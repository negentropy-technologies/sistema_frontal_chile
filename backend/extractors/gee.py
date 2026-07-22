"""
Extractor de Google Earth Engine para frontal_sur.frames_raster:
GOES-19 (GOES-East operativo desde 2025, reemplazo de GOES-16, que se
verifico en vivo el 2026-07-16 con 0 imagenes nuevas en 48 horas),
Full Disk, cubre Sudamerica. Bandas CMI_C08/C09/C10 (vapor de agua
alto/medio/bajo) y CMI_C13 (IR limpio, usado para el umbral de nubes
altas del spec).

GEE queda solo para GOES: IMERG se extrae directo de NASA GES DISC
(extractors/nasa_imerg.py) por la politica del proyecto de preferir
la API del emisor original del dato; migrar GOES al bucket publico
AWS de NOAA (noaa-goes19) queda anotado como siguiente paso, requiere
reproyectar desde la proyeccion geoestacionaria.

Escalabilidad de la descarga: GOES emite un frame cada 10 minutos
(cientos por dia, varios MB cada uno). En vez de bajar todos, el
extractor trae la lista completa de timestamps en UNA sola llamada
(aggregate_array, no un getInfo por imagen) y elige client-side un
frame por hora. Ademas, si el GeoTIFF de un timestamp ya existe en
disco no se vuelve a descargar: las corridas de cron con ventanas
solapadas solo bajan lo nuevo.
"""

from datetime import datetime, timezone
from pathlib import Path

import ee
import numpy as np
import rasterio
from PIL import Image

from db import load_config
from extractors._raster import download_geotiff, save_png_overlay
from logutil import log

GOES_COLLECTION = "NOAA/GOES/19/MCMIPF"

# Variables que se descargan de GOES, cada una con sus bandas y su
# generador de PNG para el loop del mapa:
# - goes_cloud_moisture: vapor de agua (C08/C09/C10, temperatura de
#   brillo en Kelvin) + IR limpio C13; overlay en escala de grises.
# - goes_geocolor: composicion tipo GeoColor de CIRA (quick guide
#   2017): color verdadero de dia con verde sintetico desde
#   C01 azul / C02 rojo / C03 veggie, e IR C13 invertido de noche.
#   GeoColor "ya hecho" no existe como dato georreferenciado (los JPG
#   de STAR son renders de sector sin worldfile), por eso se compone
#   aqui a partir de las bandas.
GOES_BANDS = ["CMI_C08", "CMI_C09", "CMI_C10", "CMI_C13"]
GEOCOLOR_BANDS = ["CMI_C01", "CMI_C02", "CMI_C03", "CMI_C13"]

# Nieve/hielo (C05, reflectancia) y niebla nocturna/estratos bajos
# (C07, IR onda corta): nieve post-frontal en cordillera y niebla.
# La otra coleccion GOES-19 de GEE (FDCF, incendios) se evaluo y
# descarto el 2026-07-17: no aporta al monitoreo frontal de invierno.
NIEVE_NIEBLA_BANDS = ["CMI_C05", "CMI_C07"]

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "frames" / "gee"

_initialized = False


def initialize(config: dict | None = None) -> None:
    """
    Inicializa Earth Engine con las credenciales OAuth ya cacheadas
    localmente por "earthengine authenticate". Idempotente dentro del
    proceso: si ya se inicializo, no vuelve a hacerlo.
    """
    global _initialized
    if _initialized:
        return
    if config is None:
        config = load_config()
    ee.Initialize(project=config["GEE_PROJECT"])
    _initialized = True


def _hourly_images(collection: "ee.ImageCollection") -> list[tuple["ee.Image", datetime]]:
    """
    Devuelve pares (imagen, valid_time) submuestreados a un frame por
    hora. Earth Engine es perezoso y server-side: iterar la coleccion
    con getInfo() por imagen seria una llamada de red por frame. Aca
    se trae la lista completa de timestamps en una sola llamada
    (aggregate_array conserva el orden de la coleccion, igual que
    toList), y la eleccion de que frames bajar se hace client-side:
    el primer frame de cada hora calendario.
    """
    times_ms = collection.aggregate_array("system:time_start").getInfo()
    if not times_ms:
        return []
    ee_list = collection.toList(len(times_ms))

    picked = []
    seen_hours = set()
    for index, time_ms in enumerate(times_ms):
        valid_time = datetime.fromtimestamp(time_ms / 1000, tz=timezone.utc)
        hour_key = valid_time.strftime("%Y%m%d%H")
        if hour_key in seen_hours:
            continue
        seen_hours.add(hour_key)
        picked.append((ee.Image(ee_list.get(index)), valid_time))
    return picked


def _scaled(image: "ee.Image", bands: list[str]) -> "ee.Image":
    """
    Convierte las bandas CMI de cuentas digitales a unidades fisicas
    (reflectancia 0-1, Kelvin) usando la escala y offset que cada
    imagen MCMIP trae como propiedades (CMI_Cxx_scale/_offset).
    Necesario porque getDownloadURL exporta los DN crudos tal como se
    almacenan (comprobado en vivo: reflectancias en miles), y las
    composiciones GeoColor/conveccion usan umbrales fisicos.
    """
    scaled = []
    for band in bands:
        scaled.append(
            image.select(band)
            .multiply(ee.Number(image.get(band + "_scale")))
            .add(ee.Number(image.get(band + "_offset")))
        )
    # toFloat: multiply/add dejan la banda en double y el request de
    # descarga duplica su tamano, superando el limite de 48 MB de GEE
    # con el bbox ancho; float32 sobra para reflectancias y Kelvin.
    return ee.Image.cat(scaled).rename(bands).toFloat()


def _save_geocolor_overlay(tif_path: Path, png_path: Path) -> None:
    """
    Composicion tipo GeoColor (CIRA quick guide 2017) desde el GeoTIFF
    de GEOCOLOR_BANDS: de dia, color verdadero con el verde sintetico
    de CIRA (0.45 rojo + 0.45 azul + 0.10 veggie) y correccion gamma;
    de noche, IR C13 invertido en grises (nubes altas frias = claras).
    La transicion dia/noche se mezcla por pixel segun la reflectancia
    del canal rojo, para que el terminador solar no corte el loop con
    un borde duro.
    """
    with rasterio.open(tif_path) as src:
        blue = np.nan_to_num(src.read(1).astype("float64"))
        red = np.nan_to_num(src.read(2).astype("float64"))
        veggie = np.nan_to_num(src.read(3).astype("float64"))
        ir = np.nan_to_num(src.read(4).astype("float64"), nan=300.0)

    green = 0.45 * red + 0.45 * blue + 0.10 * veggie
    day = np.stack([red, green, blue], axis=-1)
    day = np.clip(day, 0.0, 1.0) ** (1 / 2.2)

    # IR invertido: 300 K (superficie calida) -> negro, 180 K (topes
    # convectivos muy frios) -> blanco.
    night_gray = np.clip((300.0 - ir) / 120.0, 0.0, 1.0)
    night = np.stack([night_gray] * 3, axis=-1)

    weight = np.clip((red - 0.03) / 0.07, 0.0, 1.0)[..., np.newaxis]
    rgb = (weight * day + (1 - weight) * night) * 255
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb.astype("uint8"), mode="RGB").save(png_path)


def _save_nieve_niebla_overlay(tif_path: Path, png_path: Path) -> None:
    """
    Overlay dia/noche para NIEVE_NIEBLA_BANDS: de dia manda C05
    (reflectancia; nieve/hielo brillan), de noche C05 no tiene senal
    (banda solar) y manda C07 (IR onda corta) invertido, que revela
    niebla y estratos bajos. La mezcla es por pixel segun la propia
    reflectancia de C05, asi el terminador solar transiciona suave.
    """
    with rasterio.open(tif_path) as src:
        c05 = np.nan_to_num(src.read(1).astype("float64"))
        c07 = np.nan_to_num(src.read(2).astype("float64"), nan=300.0)

    day = np.clip(c05, 0.0, 1.0) ** (1 / 2.2)
    night = np.clip((300.0 - c07) / 70.0, 0.0, 1.0)
    weight = np.clip((c05 - 0.02) / 0.08, 0.0, 1.0)
    gray = ((weight * day + (1 - weight) * night) * 255).astype("uint8")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(gray, mode="L").save(png_path)


def _save_conveccion_overlay(tif_path: Path, png_path: Path) -> None:
    """
    IR realzado ("colorized IR", el estandar NOAA/CIRA para vigilar
    conveccion) desde la banda C13 del GeoTIFF de goes_cloud_moisture
    (banda 4, Kelvin): topes mas calidos que -30 C en grises, y de ahi
    hacia abajo rampa azul -> verde -> amarillo -> naranja -> rojo
    (conveccion profunda / nucleos de lluvia) -> magenta (< -85 C).
    """
    with rasterio.open(tif_path) as src:
        kelvin = np.nan_to_num(src.read(4).astype("float64"), nan=300.0)
    celsius = kelvin - 273.15

    # Rampa por canal via interpolacion sobre anclas de temperatura
    # (crecientes, como exige np.interp; -30 C es el umbral donde
    # termina el realce y empieza el gris).
    anchors = [-85.0, -75.0, -65.0, -55.0, -45.0, -30.0]
    reds = [120, 255, 255, 255, 0, 0]
    greens = [0, 0, 120, 255, 255, 60]
    blues = [60, 0, 0, 0, 120, 255]

    gray = np.clip((300.0 - kelvin) / 120.0, 0.0, 1.0) * 255
    r = np.where(celsius <= -30, np.interp(celsius, anchors, reds), gray)
    g = np.where(celsius <= -30, np.interp(celsius, anchors, greens), gray)
    b = np.where(celsius <= -30, np.interp(celsius, anchors, blues), gray)

    rgb = np.stack([r, g, b], axis=-1).astype("uint8")
    png_path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgb, mode="RGB").save(png_path)


def fetch_frames(start: datetime, end: datetime, bbox: tuple) -> list[dict]:
    """
    Descarga los frames GOES-19 (nubosidad/vapor de agua + IR) dentro
    de la ventana [start, end] y el bbox dado, guarda cada uno como
    GeoTIFF + overlay PNG en data/frames/gee/goes_cloud_moisture/, y
    devuelve una fila por frame lista para frontal_sur.frames_raster.
    Los archivos que ya existen en disco no se vuelven a descargar,
    pero si generan fila igual: el upsert del orquestador es
    idempotente y asi una corrida interrumpida antes de escribir a la
    BD se repara sola en la corrida siguiente.
    """
    initialize()
    region = ee.Geometry.Rectangle(list(bbox))
    collection = (
        ee.ImageCollection(GOES_COLLECTION)
        .filterDate(start.isoformat(), end.isoformat())
        .filterBounds(region)
    )
    hourly = _hourly_images(collection)
    log(f"goes: {len(hourly)} frames horarios en la ventana")
    variables = (
        ("goes_cloud_moisture", GOES_BANDS, save_png_overlay),
        ("goes_geocolor", GEOCOLOR_BANDS, _save_geocolor_overlay),
        ("goes_nieve_niebla", NIEVE_NIEBLA_BANDS, _save_nieve_niebla_overlay),
    )
    rows = []
    for image, valid_time in hourly:
        stamp = valid_time.strftime("%Y%m%dT%H%M%S")
        for variable, bands, png_builder in variables:
            tif_path = DATA_DIR / variable / f"{stamp}.tif"
            png_path = DATA_DIR / variable / f"{stamp}.png"
            if not tif_path.exists():
                # scale 3000 (no 2000): con el bbox Coquimbo-Magallanes
                # la peticion de 4 bandas a 2000 m pide ~103 MB y GEE
                # rechaza sobre 48 MB; a 3000 m queda en ~46 MB. Sobre
                # Chile la resolucion efectiva de GOES-East ya es >3 km
                # por el angulo de vista, asi que no se pierde detalle
                # real.
                download_geotiff(_scaled(image, bands), region, tif_path, scale=3000)
                log(f"goes {variable} {stamp}: descargado")
            else:
                log(f"goes {variable} {stamp}: ya existia en disco")
            if not png_path.exists():
                png_builder(tif_path, png_path)
            rows.append({
                "source": "gee",
                "variable": variable,
                "region": "centro_sur",
                "valid_time": valid_time,
                "bbox": list(bbox),
                "file_path": str(tif_path),
                "png_overlay_path": str(png_path),
                "created_at": datetime.now(timezone.utc),
            })
            if variable == "goes_cloud_moisture":
                # IR realzado: derivado de la banda C13 del MISMO tif,
                # sin descarga extra; solo se genera su PNG de rampa.
                conv_png = DATA_DIR / "goes_conveccion" / f"{stamp}.png"
                if not conv_png.exists():
                    _save_conveccion_overlay(tif_path, conv_png)
                rows.append({
                    "source": "gee",
                    "variable": "goes_conveccion",
                    "region": "centro_sur",
                    "valid_time": valid_time,
                    "bbox": list(bbox),
                    "file_path": str(tif_path),
                    "png_overlay_path": str(conv_png),
                    "created_at": datetime.now(timezone.utc),
                })
    return rows
