"""
Plot RESUMEN (un solo mapa) de la anomalia acumulada de precipitacion
para Coquimbo en todo el periodo: suma las anomalias DIARIAS ya
calculadas por chirps_anomaly_coquimbo.py (cada una ya corregida por
IDW de residuos + elevacion + umbral de lluvia espuria, ver
extractors/anomaly_raster.py) -- sumar anomalias diarias ya calculadas
es valido porque la anomalia es una cantidad lineal (observado menos
climatologia), asi que la suma da el deficit/superavit TOTAL del
periodo, igual que sumar los dias de un mes da el total del mes.

Reusa mask_to_region/crop_to_bbox/reproject_to_utm de plot_anomaly.py
(mismo recorte a POLIGONO real + reproyeccion UTM que el plot
nacional). Los GeoTIFF diarios individuales quedan en disco para el
pipeline (una fila por dia en frontal_sur.frames_raster, animable en
el mapa web); este script es solo para revision humana del evento
completo.

Uso:
    .venv/bin/python backend/management/plot_anomaly_coquimbo.py <carpeta de chirps_anomaly_coquimbo> <png de salida>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.colors import TwoSlopeNorm

from db import app_role_config, get_engine
from management.plot_anomaly import (
    COLOR_DEFICIT,
    COLOR_SUPERAVIT,
    METODOLOGIA,
    _extent_from_transform,
    crop_to_bbox,
    load_anomaly_days,
    load_region_geometries,
    mask_to_region,
    reproject_to_utm,
)

REGION_ID_COQUIMBO = 4


def main(anomaly_dir: Path, png_path: Path) -> None:
    dias = load_anomaly_days(anomaly_dir)
    if not dias:
        print(f"sin GeoTIFF de anomalia en {anomaly_dir}")
        return

    fechas = [f for f, _, _ in dias]
    _, primer_band, transform = dias[0]
    total = np.zeros_like(primer_band)
    for _, band, _ in dias:
        total += np.nan_to_num(band, nan=0.0)

    with get_engine(app_role_config()) as engine:
        regiones = load_region_geometries(engine)
    coquimbo = next((r for r in regiones if r[0] == REGION_ID_COQUIMBO), None)
    if coquimbo is None:
        sys.exit(f"region_id {REGION_ID_COQUIMBO} no encontrada")
    _, nombre, geometry, bbox = coquimbo

    centroid_lon = (bbox[0] + bbox[2]) / 2
    recortado, out_transform = reproject_to_utm(
        *crop_to_bbox(mask_to_region(total, transform, geometry), transform, bbox),
        centroid_lon,
    )

    finite = recortado[np.isfinite(recortado)]
    p_bajo, p_alto = np.percentile(finite, [2, 98])
    norm = TwoSlopeNorm(vmin=min(p_bajo, -0.1), vcenter=0, vmax=max(p_alto, 0.1))

    fig, ax = plt.subplots(figsize=(6, 7))
    extent = _extent_from_transform(recortado, out_transform)
    im = ax.imshow(recortado, extent=extent, origin="upper", cmap="RdBu", norm=norm, aspect="equal")
    ax.set_xticks([])
    ax.set_yticks([])
    fig.suptitle(f"¿Llovio mas o menos de lo normal en {nombre}?", fontsize=14, fontweight="bold")
    ax.set_title(f"{fechas[0]} al {fechas[-1]} (acumulado del periodo)", fontsize=11, pad=8)

    cbar = fig.colorbar(im, ax=ax, extend="both", shrink=0.8)
    cbar.set_label("Diferencia respecto a lo normal (mm, acumulado del periodo)", fontsize=9)
    cbar.ax.text(0.5, 1.03, "MAS lluvia\nque lo normal", transform=cbar.ax.transAxes,
                 ha="center", va="bottom", fontsize=8.5, color=COLOR_SUPERAVIT, fontweight="bold")
    cbar.ax.text(0.5, -0.03, "MENOS lluvia\nque lo normal", transform=cbar.ax.transAxes,
                 ha="center", va="top", fontsize=8.5, color=COLOR_DEFICIT, fontweight="bold")

    media = float(np.nanmean(recortado))
    if media > 0:
        resumen = f"Llovio mas de lo normal en toda la region: en promedio {media:.0f} mm de mas para estas fechas."
    else:
        resumen = f"Llovio menos de lo normal en toda la region: en promedio {abs(media):.0f} mm de menos para estas fechas."
    fig.text(0.5, 0.06, resumen, ha="center", va="top", fontsize=10, style="italic")
    fig.text(0.5, 0.01, METODOLOGIA, ha="center", va="top", fontsize=7, color="dimgray", wrap=True)

    fig.subplots_adjust(bottom=0.2)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(png_path, dpi=150, bbox_inches="tight")
    plt.close("all")
    print(f"plot guardado en {png_path} ({len(dias)} dias sumados)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("uso: plot_anomaly_coquimbo.py <carpeta de chirps_anomaly_coquimbo> <png de salida>")
    main(Path(sys.argv[1]), Path(sys.argv[2]))
