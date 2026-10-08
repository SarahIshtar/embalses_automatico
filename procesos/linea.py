"""
Gráfico de línea: porcentaje de la reserva hidráulica peninsular, semana a semana.

Fuente: Boletín Hidrológico semanal del MITECO (documento 40), fila TOTAL de la página 5.
Acumula histórico: añade todos los boletines publicados desde el último dato guardado.

Uso: python -m procesos.linea
"""

from datetime import date

import pandas as pd

from procesos import comun

PROCESO = "linea"
COLUMNAS = ["Capacidad actual", "Año anterior", "Media 5 años", "Media 10 años"]
COL_FECHA = "Date"


def procesar(pdfs) -> pd.DataFrame:
    """Una fila por boletín, ordenadas por fecha."""
    filas = []
    for pdf in pdfs:
        fila = comun.porcentajes_pagina5(pdf)
        fila[COL_FECHA] = comun.fecha_portada(pdf)
        print(f"  {fila[COL_FECHA]}: {fila['Capacidad actual']} %")
        filas.append(fila)
    return pd.DataFrame(filas)[COLUMNAS + [COL_FECHA]].sort_values(COL_FECHA).reset_index(drop=True)


def validar(nuevas: pd.DataFrame, datos: pd.DataFrame | None, config: dict, hoy: date | None = None):
    print("Validación:")
    v = comun.Validador()
    reglas = config["validacion"]
    ultima = datos[COL_FECHA].max() if datos is not None else None

    for col in COLUMNAS:
        v.comprobar(nuevas[col].notna().all(), f"'{col}' leído en todos los boletines")
        v.rango(nuevas[col].dropna(), f"'{col}'")

    v.comprobar(nuevas[COL_FECHA].is_unique, "Sin fechas repetidas entre los boletines nuevos")
    v.comprobar(nuevas[COL_FECHA].min() > ultima if ultima else True,
                f"Todas las fechas nuevas posteriores al último dato guardado ({ultima})")
    v.fecha(nuevas[COL_FECHA].max(), ultima, reglas["dias_max_antiguedad"], hoy)

    ## Salto máximo respecto al valor anterior (el último no vacío).
    salto_max = reglas["linea_salto_max_puntos"]
    previa = datos.dropna(subset=COLUMNAS).iloc[-1] if datos is not None else None
    for _, fila in nuevas.iterrows():
        if previa is not None:
            for col in COLUMNAS:
                salto = abs(fila[col] - previa[col])
                v.comprobar(salto <= salto_max,
                            f"{fila[COL_FECHA]} '{col}': {previa[col]} -> {fila[col]} (salto {salto:.1f} <= {salto_max})")
        previa = fila
    v.terminar()


def escribir(datos: pd.DataFrame, historico: pd.DataFrame | None, nuevas: pd.DataFrame):
    completos = pd.concat([datos, nuevas], ignore_index=True) if datos is not None else nuevas
    fecha = nuevas[COL_FECHA].max()
    comun.escribir_atomico({
        comun.ruta_datos(PROCESO): comun.a_csv(completos),
        comun.ruta_metadata(PROCESO): comun.metadata(PROCESO, fecha),
        comun.ruta_historico(PROCESO): comun.a_csv(comun.anadir_historico(historico, nuevas, COL_FECHA)),
    })


def principal(carpeta_tmp):
    config = comun.cargar_config()
    datos = comun.leer_csv(comun.ruta_datos(PROCESO), COL_FECHA)
    historico = comun.leer_csv(comun.ruta_historico(PROCESO), COL_FECHA)
    ultima = datos[COL_FECHA].max() if datos is not None else None

    print(f"1. Descarga (último dato guardado: {ultima})")
    pdfs = comun.buscar_nuevos(ultima, comun.DOC_BOLETIN, carpeta_tmp)
    if not pdfs:
        raise comun.SinNovedades(f"no hay boletín posterior a {ultima}")

    print("2. Procesamiento")
    nuevas = procesar(pdfs)
    if ultima is not None and nuevas[COL_FECHA].max() <= ultima:
        raise comun.SinNovedades(f"el boletín más reciente ({nuevas[COL_FECHA].max()}) no es posterior a {ultima}")

    print("3. ", end="")
    validar(nuevas, datos, config)

    print("4. Escritura")
    escribir(datos, historico, nuevas)
    return nuevas[COL_FECHA].max()


if __name__ == "__main__":
    comun.ejecutar(PROCESO, principal)
