# Backend - sistema_frontal_chile

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp backend/.env.example backend/.env
# completar backend/.env con las credenciales reales
```

Autenticar Earth Engine una vez (queda cacheado localmente):

```bash
.venv/bin/earthengine authenticate
```

## Migraciones

```bash
.venv/bin/python backend/migrate.py
```

## Rol de BD acotado (una sola vez)

```bash
.venv/bin/python backend/create_role.py '<password-generado>'
# agregar DB_APP_USER=frontal_sur_app y DB_APP_PASSWORD=<password> a backend/.env
```

## Catalogo de estaciones DMC (una vez, y ocasionalmente)

Puebla `frontal_sur.dmc_stations` (149 estaciones EMA con geometria,
altura y zona geografica) desde getEstacionesRedEma. El extractor de
datos DMC lee las estaciones de esta tabla, no de la API.

```bash
.venv/bin/python backend/dmc_stations.py
```

## Ingesta

```bash
# corrida normal, ventana de 7 dias hacia atras desde ahora
.venv/bin/python backend/ingest.py --days 7

# extraccion por fechas definidas (backfill puntual)
.venv/bin/python backend/ingest.py --start 2026-06-01 --end 2026-06-15

# valida un extractor nuevo sin escribir a la BD
.venv/bin/python backend/ingest.py --days 1 --dry-run
```

Fuentes activas:

| Fuente | Destino | Latencia | Nota |
|---|---|---|---|
| `gee_frames` (GOES-19 + GPM IMERG) | `frames_raster` + GeoTIFF/PNG en `data/frames/gee/` | GOES ~30 min, IMERG ~24 h | 1 frame por hora, recortado al bbox |
| `gee_choropleth` (IMERG por comuna) | `choropleth_stats` | ~24 h | ventanas fijas 24h/72h/7d |
| `noaa_ncei` (estaciones GHCND) | `station_obs` | meses | contexto historico, no near real time |
| `dmc` (red EMA) | `station_obs` | minutos | 7 variables por estacion, pacing de 0.3 s entre llamadas |
| `chirps` (CHIRPS v3.0 prelim) | `frames_raster` + `chirps_raster` (postgis_raster) | ~7 dias | recorte por rango HTTP, sin bajar el tif global |

Convenciones: toda columna de geometria se llama `geometria` (o
`geometria_<rol>`) y va al final de la tabla; el bbox del proyecto
(`REGION_BBOX` en `ingest.py`, RM a Los Lagos + ZEE) es la unica
fuente de verdad geografica y se pasa por parametro a cada extractor.

Fuera del pipeline por ahora: Google Flood Hub (waitlist de Google
pendiente), MSWEP (requiere registro en GloH2O y acceso a su Drive),
CHIRPS v3 final (el CHC lo publica con meses de retraso; el upsert
reemplazara los preliminares cuando exista).

## Monitoreo continuo (cron)

Sin scheduler ni cola de tareas: una entrada de crontab basta, porque
el pipeline es idempotente (upsert con ON CONFLICT, y los rasters ya
descargados no se vuelven a bajar). Editar con `crontab -e`:

```cron
0 */6 * * * cd /ruta/al/proyecto && .venv/bin/python backend/ingest.py --days 2 >> /var/log/frontal_sur_ingest.log 2>&1
```

Corre cada 6 horas con ventana de 2 dias: IMERG llega al catalogo de
Earth Engine con ~24 horas de retraso (medido el 2026-07-17), asi que
una ventana de 1 dia lo perderia sistematicamente. El solape extra es
barato gracias a la idempotencia. CHIRPS (~7 dias de latencia) se
recoge solo a medida que el CHC publica, sin ventana especial.

## Tests

Sin pytest: cada archivo en `backend/tests/` corre con `assert` plano.

```bash
.venv/bin/python backend/tests/test_db.py
.venv/bin/python backend/tests/test_migrate.py
.venv/bin/python backend/tests/test_role.py
.venv/bin/python backend/tests/test_retry.py
.venv/bin/python backend/tests/test_http.py
.venv/bin/python backend/tests/test_noaa_ncei.py
.venv/bin/python backend/tests/test_gee.py
.venv/bin/python backend/tests/test_chirps.py
.venv/bin/python backend/tests/test_ingest.py
```

Los tests de `noaa_ncei`, `gee`, `chirps` e `ingest` (parcialmente)
llaman a APIs externas reales o a la BD real con el rol acotado
`frontal_sur_app`; no hay mocks en este proyecto.
