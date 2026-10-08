"""
Mapa por cuencas: reserva total embalsada por ámbitos.

Fuente: Boletín Hidrológico semanal del MITECO (documento 40), página 7.
Solo se publica el último boletín; cada semana se guarda en historicos/cuencas.csv.

Uso: python -m procesos.cuencas
"""

from datetime import date

import pandas as pd

from procesos import comun

PROCESO = "cuencas"
COL_FECHA = "Fecha"

AMBITOS_ESPERADOS = [
    "Cantábrico Oriental", "Cantábrico Occidental", "Miño - Sil", "Galicia Costa",
    "Cuencas Internas del País Vasco", "Duero", "Tajo", "Guadiana", "Tinto, Odiel y Piedras",
    "Guadalete-Barbate", "Guadalquivir", "Vertiente Atlántica", "Cuenca Mediterránea Andaluza",
    "Segura", "Júcar", "Ebro", "Cuencas Internas de Cataluña", "Vertiente Mediterránea", "TOTAL PENINSULAR",
]

## Nombres que usa el mapa de Datawrapper.
NOMBRES_DATAWRAPPER = {
    "Miño - Sil": "MIÑO-SIL",
    "Galicia Costa": "GALICIA-COSTA",
    "Guadalete-Barbate": "GUADALETE Y BARBATE",
    "Cuenca Mediterránea Andaluza": "CUENCAS MEDITERRÁNEAS ANDALUZAS",
}

COLUMNAS_HM3 = ["Total", "Actual", "Año anterior", "Media 5 años", "Media 10 años"]
COLUMNAS_PCT = ["%_actual", "%_año_anterior", "%_decada"]


def a_entero(x) -> int:
    """'13.639' -> 13639."""
    digitos = "".join(ch for ch in str(x) if ch.isdigit())
    if not digitos:
        raise ValueError(f"valor no numérico: {x!r}")
    return int(digitos)


def procesar(pdf) -> tuple[pd.DataFrame, date, float]:
    """Devuelve la tabla de cuencas (con los nombres originales), su fecha y el % total de la página 5."""
    fecha = comun.fecha_portada(pdf)
    try:
        cuencas = comun.leer_tabla(pdf, 7, stream=True)[0]
        cuencas = cuencas.iloc[2:].reset_index(drop=True).copy()
        ## tabula junta Actual, Año anterior y Media 5 años en una columna; Total y Media 10 años van aparte.
        cuencas[["Actual", "Año anterior", "Media 5 años"]] = cuencas["RESERVA TOTAL EMBALSADA hm3"].str.split(expand=True, n=2)
        cuencas = cuencas.rename(columns={cuencas.columns[0]: "Ámbitos", "Unnamed: 0": "Total", "Unnamed: 1": "Media 10 años"})
        cuencas = cuencas[["Ámbitos"] + COLUMNAS_HM3]
        for col in COLUMNAS_HM3:
            cuencas[col] = cuencas[col].apply(a_entero)
    except Exception as e:
        raise comun.ErrorPaso("procesamiento", f"No se puede leer la tabla de la página 7 de {pdf.name}: {e!r}")

    cuencas["Ámbitos"] = cuencas["Ámbitos"].astype(str).str.strip()
    cuencas["%_actual"] = (cuencas["Actual"] / cuencas["Total"] * 100).round(2)
    cuencas["%_año_anterior"] = (cuencas["Año anterior"] / cuencas["Total"] * 100).round(2)
    cuencas["%_decada"] = (cuencas["Media 10 años"] / cuencas["Total"] * 100).round(2)

    pct_pagina5 = comun.porcentajes_pagina5(pdf)["Capacidad actual"]
    print(f"  {fecha}: {len(cuencas)} ámbitos leídos")
    return cuencas, fecha, pct_pagina5


def validar(cuencas: pd.DataFrame, fecha: date, pct_pagina5: float, ultima: date | None,
            config: dict, hoy: date | None = None):
    print("Validación:")
    v = comun.Validador()
    reglas = config["validacion"]
    v.fecha(fecha, ultima, reglas["dias_max_antiguedad"], hoy)

    faltan = sorted(set(AMBITOS_ESPERADOS) - set(cuencas["Ámbitos"]))
    sobran = sorted(set(cuencas["Ámbitos"]) - set(AMBITOS_ESPERADOS))
    v.comprobar(not faltan and not sobran and len(cuencas) == len(AMBITOS_ESPERADOS),
                f"Están los {len(AMBITOS_ESPERADOS)} ámbitos esperados", f"faltan {faltan}, sobran {sobran}")

    for col in COLUMNAS_PCT:
        v.rango(cuencas[col], f"'{col}'")

    total = cuencas.loc[cuencas["Ámbitos"] == "TOTAL PENINSULAR", "%_actual"]
    if not total.empty:
        dif = abs(total.iloc[0] - pct_pagina5)
        v.comprobar(dif <= reglas["cuencas_diferencia_max_total"],
                    f"TOTAL PENINSULAR {total.iloc[0]} % cuadra con la página 5 ({pct_pagina5} %): "
                    f"diferencia {dif:.2f} <= {reglas['cuencas_diferencia_max_total']}")
    v.terminar()


def para_datawrapper(cuencas: pd.DataFrame) -> pd.DataFrame:
    grafico = cuencas.copy()
    grafico["nombres"] = grafico["Ámbitos"]
    grafico["Ámbitos"] = grafico["Ámbitos"].replace(NOMBRES_DATAWRAPPER)
    return grafico


def escribir(cuencas: pd.DataFrame, fecha: date, historico: pd.DataFrame | None):
    nuevas = cuencas.copy()
    nuevas.insert(0, COL_FECHA, fecha)
    comun.escribir_atomico({
        comun.ruta_datos(PROCESO): comun.a_csv(para_datawrapper(cuencas)),
        comun.ruta_metadata(PROCESO): comun.metadata(PROCESO, fecha),
        comun.ruta_historico(PROCESO): comun.a_csv(comun.anadir_historico(historico, nuevas, COL_FECHA)),
    })


def principal(carpeta_tmp):
    config = comun.cargar_config()
    historico = comun.leer_csv(comun.ruta_historico(PROCESO), COL_FECHA)
    ultima = historico[COL_FECHA].max() if historico is not None else None

    print(f"1. Descarga (último dato guardado: {ultima})")
    pdfs = comun.buscar_nuevos(ultima, comun.DOC_BOLETIN, carpeta_tmp)
    if not pdfs:
        raise comun.SinNovedades(f"no hay boletín posterior a {ultima}")

    print("2. Procesamiento (solo el boletín más reciente)")
    cuencas, fecha, pct_pagina5 = procesar(pdfs[-1])
    if ultima is not None and fecha <= ultima:
        raise comun.SinNovedades(f"el boletín más reciente ({fecha}) no es posterior a {ultima}")

    print("3. ", end="")
    validar(cuencas, fecha, pct_pagina5, ultima, config)

    print("4. Escritura")
    escribir(cuencas, fecha, historico)
    return fecha


if __name__ == "__main__":
    comun.ejecutar(PROCESO, principal)
