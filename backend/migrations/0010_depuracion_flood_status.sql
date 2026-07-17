-- Depuracion (2026-07-17): flood_status era la unica tabla sin uso en
-- el pipeline actual (Google Flood Hub sigue diferido por el waitlist
-- de Google). Se elimina; el plan que retome Flood Hub la recreara
-- con su propia migracion (el DDL de referencia queda en el historial
-- de git, migracion 0003).

DROP TABLE IF EXISTS frontal_sur.flood_status;
