"""
Migración inicial. Se ejecuta UNA sola vez, en local, para crear los CSV, los metadata.json
y los históricos a partir del proyecto anterior (PANELES/embalses/embalses), que solo se lee.

- linea: importa porcentaje_agua.xlsx y añade como filas vacías las 11 semanas que no se
  pudieron descargar, con la fecha del lunes de cierre de cada boletín.
- cuencas y embalses: procesan y validan los PDF de la semana 40 de 2026 (05/10/2026)
  que ya están descargados en el proyecto anterior.

Uso: python -m migracion.migracion_inicial [--origen RUTA] [--forzar]
"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from procesos import comun, cuencas, embalses, linea

ORIGEN_POR_DEFECTO = comun.RAIZ.parent / "embalses" / "embalses"

## Boletines que el servidor del MITECO ya no sirve (año, número).
SEMANAS_PERDIDAS = [(2022, 52), (2023, 18), (2024, 51), (2024, 52)] + [(2025, n) for n in range(1, 7)]


def migrar_linea(origen: Path):
    print("\n===== linea =====")
    datos = pd.read_excel(origen / "porcentaje_agua.xlsx")
    datos = datos[linea.COLUMNAS + [linea.COL_FECHA]].copy()
    for col in linea.COLUMNAS:
        datos[col] = datos[col].apply(comun.a_decimal)
    datos[linea.COL_FECHA] = pd.to_datetime(datos[linea.COL_FECHA].astype(str), dayfirst=True).dt.date
    print(f"  {len(datos)} filas importadas ({datos[linea.COL_FECHA].min()} a {datos[linea.COL_FECHA].max()})")

    vacias = pd.DataFrame({linea.COL_FECHA: [comun.fecha_cierre(a, n) for a, n in SEMANAS_PERDIDAS]})
    for col in linea.COLUMNAS:
        vacias[col] = np.nan
    for (a, n), f in zip(SEMANAS_PERDIDAS, vacias[linea.COL_FECHA]):
        print(f"  Semana perdida {a}-{n:02d} -> fila vacía con fecha {f}")

    completos = (pd.concat([datos, vacias[linea.COLUMNAS + [linea.COL_FECHA]]], ignore_index=True)
                 .sort_values(linea.COL_FECHA).reset_index(drop=True))

    print("Validación:")
    v = comun.Validador()
    v.comprobar(completos[linea.COL_FECHA].is_unique, "Sin fechas repetidas",
                str(completos.loc[completos[linea.COL_FECHA].duplicated(), linea.COL_FECHA].tolist()))
    existentes = pd.to_datetime(datos[linea.COL_FECHA])
    cercanas = [f for f in vacias[linea.COL_FECHA] if (abs(existentes - pd.Timestamp(f)).dt.days < 4).any()]
    v.comprobar(not cercanas, "Ninguna semana perdida a menos de 4 días de un dato existente", str(cercanas))
    for col in linea.COLUMNAS:
        v.rango(completos[col].dropna(), f"'{col}'")
    v.terminar()

    fecha = completos[linea.COL_FECHA].max()
    comun.escribir_atomico({
        comun.ruta_datos(linea.PROCESO): comun.a_csv(completos),
        comun.ruta_metadata(linea.PROCESO): comun.metadata(linea.PROCESO, fecha),
        comun.ruta_historico(linea.PROCESO): comun.a_csv(completos),
    })


def migrar_cuencas(origen: Path):
    print("\n===== cuencas =====")
    pdf = origen / "informes" / "boletin_2026_40.pdf"
    tabla, fecha, pct_pagina5 = cuencas.procesar(pdf)
    cuencas.validar(tabla, fecha, pct_pagina5, None, comun.cargar_config())
    cuencas.escribir(tabla, fecha, None)


def migrar_embalses(origen: Path):
    print("\n===== embalses =====")
    pdf = origen / "informes" / "embalses_2026_40.pdf"
    tabla, fecha = embalses.procesar(pdf)
    mapa = embalses.validar(tabla, fecha, None, None, comun.cargar_config())
    embalses.escribir(tabla, mapa, fecha, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--origen", type=Path, default=ORIGEN_POR_DEFECTO)
    parser.add_argument("--forzar", action="store_true", help="sobrescribe los datos que ya existan")
    args = parser.parse_args()

    existentes = [p for p in comun.DIR_GRAFICOS.rglob("*") if p.is_file()] + list(comun.DIR_HISTORICOS.glob("*.csv"))
    if existentes and not args.forzar:
        sys.exit(f"Ya hay datos ({len(existentes)} archivos). Usa --forzar si de verdad quieres rehacer la migración.")

    fallos = []
    for nombre, funcion in [("linea", migrar_linea), ("cuencas", migrar_cuencas), ("embalses", migrar_embalses)]:
        try:
            funcion(args.origen)
        except comun.ErrorPaso as e:
            print(f"[{nombre}] FALLO en el paso '{e.paso}': {e}", file=sys.stderr)
            fallos.append(nombre)
    sys.exit(1 if fallos else 0)


if __name__ == "__main__":
    main()
