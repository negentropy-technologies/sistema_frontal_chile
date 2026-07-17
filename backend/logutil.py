"""
Logging minimo del pipeline: una funcion "log" que imprime con
timestamp UTC y flush inmediato. flush=True es lo importante: bajo
nohup/cron el stdout va a un archivo y sin flush las lineas quedan
retenidas en el buffer hasta que el proceso termina, que es
exactamente el silencio que este pipeline debe evitar (cada descarga
se reporta apenas ocurre).
"""

from datetime import datetime, timezone


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)