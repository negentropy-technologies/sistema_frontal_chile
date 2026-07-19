"""
Test plano (con assert, sin pytest) para extractors/dga.py y
dga_stations.py.

Correr con: .venv/bin/python backend/tests/test_dga.py

Solo partes puras (sin red ni BD): el parser de la tabla de valores
instantaneos y del refresca (fixtures tomadas de respuestas reales de
dgasat del 2026-07-17), el seed del catalogo, y la consistencia del
mapeo PARAMETROS/ETIQUETAS con la migracion 0004. El camino con red
queda cubierto por la corrida real del orquestador.
"""

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dga_stations import load_stations
from extractors.dga import ETIQUETAS, PARAMETROS, _columnas, _parse_pagina, _parse_parametros, _skip_to_resume

MIGRATION = Path(__file__).resolve().parent.parent / "migrations" / "0004_dga.sql"

# Fragmento real de dgasat_param_tablas_instantaneos.jsp (Rio Biobio
# en Rucalhue, 2026-07-17), con el hidden totalpag y la tabla id=datos.
HTML_TABLA = """
<input id="totalpag" name="totalpag" value="3" type="hidden"/>
<table border="1" cellpadding="2" cellspacing="2" id="datos">
  <thead>
    <tr bgcolor="#CCCCCC">
      <th bgcolor="#CCCCCC" class="txt_form" scope='col'>Nro.</th>
      <th nowrap bgcolor="#CCCCCC" class="txt_form" scope='col'>Fecha-Hora de Medicion</th>
      <th class=txt_form nowrap>RIO BIOBIO E<br>Caudal
(m3/seg)</th>
      <th class=txt_form nowrap>RIO BIOBIO E<br>Nivel de Agua
(m)</th>
  </thead>
  <tbody>
    <tr bgcolor=#E4E2E1>
      <td class=txt>1</td>
      <td class=txt>15/07/2026 00:09&nbsp;</td>
      <td class=txt align=right>439.320</td>
      <td class=txt align=right>1.570</td>
    </tr>
    <tr bgcolor=#F3F2F2>
      <td class=txt>2</td>
      <td class=txt>15/07/2026 00:39&nbsp;</td>
      <td class=txt align=right>330.598</td>
      <td class=txt align=right>1.290</td>
    </tr>
  </tbody>
</table>
"""

# Checkboxes reales del refresca de una nivometrica (Cerro Olivares):
# mezcla ids mapeados (3, 5, 72, 111) con descartados (21, 71, 126).
HTML_REFRESCA = """
<input name="parametros" value="04300001-2_3_Pptacion Acum. (mm)" type="checkbox">
<input name="parametros" value="04300001-2_5_Temp.del Aire (C)" type="checkbox">
<input name="parametros" value="04300001-2_21_Temp. Snow Pillow 1 (C)" type="checkbox">
<input name="parametros" value="04300001-2_71_Pptacion Inst (Tx) (mm)" type="checkbox">
<input name="parametros" value="04300001-2_72_Altura de Nieve (cm)" type="checkbox">
<input name="parametros" value="04300001-2_111_Equiv. en agua S.Scale (mm)" type="checkbox">
<input name="parametros" value="04300001-2_126_Tipo Pptacion disdrometro ( )" type="checkbox">
"""


def test_parse_pagina_extrae_filas_normalizadas():
    start = datetime(2026, 7, 14, tzinfo=timezone.utc)
    end = datetime(2026, 7, 18, tzinfo=timezone.utc)
    rows = _parse_pagina(HTML_TABLA, start, end)

    assert len(rows) == 2
    first = rows[0]
    assert set(first.keys()) == {"momento"} | set(PARAMETROS.values())
    # 15/07/2026 00:09 hora de Chile (UTC-4 en julio) = 04:09 UTC.
    assert first["momento"] == datetime(2026, 7, 15, 4, 9, tzinfo=timezone.utc)
    assert first["caudal"] == 439.320
    assert first["nivel_agua"] == 1.570
    assert first["temperatura_aire"] is None
    assert rows[1]["caudal"] == 330.598


def test_parse_pagina_filtra_por_ventana():
    start = datetime(2026, 7, 15, 4, 30, tzinfo=timezone.utc)
    end = datetime(2026, 7, 18, tzinfo=timezone.utc)
    rows = _parse_pagina(HTML_TABLA, start, end)
    assert len(rows) == 1
    assert rows[0]["nivel_agua"] == 1.290


def test_columnas_mapea_encabezados():
    assert _columnas(HTML_TABLA) == ["caudal", "nivel_agua"]


def test_parse_parametros_filtra_ids_mapeados():
    valores = _parse_parametros(HTML_REFRESCA)
    ids = [v.split("_")[1] for v in valores]
    assert ids == ["3", "5", "72", "111"]


def test_seed_catalogo():
    rows = load_stations()
    assert len(rows) > 4000
    for row in rows[:5]:
        assert re.fullmatch(r"\d{8}-[\dK]", row["cod_bna"])
        assert row["geometria"].startswith("POINT(")
    # El codigo BNA verificado en vivo contra dgasat debe estar tal
    # cual en el seed (Rio Biobio en Rucalhue).
    assert any(row["cod_bna"] == "08317001-8" for row in rows)


def test_skip_to_resume_corta_despues_del_cod_bna_dado():
    stations = [(1, "AAA"), (2, "BBB"), (3, "CCC")]
    assert _skip_to_resume(stations, "BBB") == [(3, "CCC")]


def test_skip_to_resume_sin_valor_no_filtra():
    stations = [(1, "AAA")]
    assert _skip_to_resume(stations, None) == stations


def test_skip_to_resume_no_encontrado_corre_completo():
    stations = [(1, "AAA")]
    assert _skip_to_resume(stations, "ZZZ") == stations


def test_parametros_sincronizados_con_migracion():
    ddl = MIGRATION.read_text()
    for columna in PARAMETROS.values():
        assert f"    {columna} DOUBLE PRECISION" in ddl, f"columna {columna} falta en 0004"
    columnas_etiquetas = {col for _, col in ETIQUETAS}
    assert columnas_etiquetas == set(PARAMETROS.values())


if __name__ == "__main__":
    test_parse_pagina_extrae_filas_normalizadas()
    test_parse_pagina_filtra_por_ventana()
    test_columnas_mapea_encabezados()
    test_parse_parametros_filtra_ids_mapeados()
    test_skip_to_resume_corta_despues_del_cod_bna_dado()
    test_skip_to_resume_sin_valor_no_filtra()
    test_skip_to_resume_no_encontrado_corre_completo()
    test_seed_catalogo()
    test_parametros_sincronizados_con_migracion()
    print("OK: todos los tests de dga pasaron")
