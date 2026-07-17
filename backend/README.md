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

## Ingesta

```bash
# corrida normal, ventana de 7 dias
.venv/bin/python backend/ingest.py --days 7

# valida un extractor nuevo sin escribir a la BD
.venv/bin/python backend/ingest.py --days 1 --dry-run
```

Fuentes activas: GEE frames (GOES-19 nubosidad/vapor de agua + GPM
IMERG precipitacion, un frame por hora, GeoTIFF + PNG en
`data/frames/gee/`), GEE choropleth (IMERG sumado por comuna), y NOAA
NCEI (observaciones diarias de estacion; llegan con meses de retraso,
es contexto historico, no near real time).

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
barato gracias a la idempotencia.

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
.venv/bin/python backend/tests/test_ingest.py
```

Los tests de `noaa_ncei`, `gee` e `ingest` (parcialmente) llaman a
APIs externas reales o a la BD real con el rol acotado
`frontal_sur_app`; no hay mocks en este proyecto. (DMC y Google Flood
Hub quedan fuera por ahora: el host de DMC no responde desde este
entorno y Flood Hub requiere aprobacion de un waitlist de Google.)
