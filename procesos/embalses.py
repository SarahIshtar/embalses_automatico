"""
Mapa de símbolos: agua embalsada en cada embalse.

Fuente: informe "Reserva hidráulica: desglose por embalses" del MITECO (documento 60),
una tabla por cuenca a partir de la página 4. Las coordenadas salen de
referencias/embalses_coordenadas.csv.
Solo se publica el último informe; cada semana se guarda en historicos/embalses.csv.

Uso: python -m procesos.embalses
"""

import gc
import re
import unicodedata
from datetime import date

import camelot
import numpy as np
import pandas as pd

from procesos import comun

PROCESO = "embalses"
COL_FECHA = "Fecha"
RUTA_COORDENADAS = comun.DIR_REFERENCIAS / "embalses_coordenadas.csv"

COLUMNAS_PDF = ["Embalse_PDF", "Río_PDF", "Capacidad", "Actual", "Dif_semana_anterior",
                "Energia_capacidad", "Energia_actual"]
COLUMNAS_GRAFICO = ["Embalses", "Nombre completo", "Ríos", "Cuenca", "Latitude", "Longitude",
                    "Capacidad", "Actual", "Porcentaje_Actual"]

## Límites aproximados de la España peninsular y Baleares.
LAT_MIN, LAT_MAX = 35.8, 44.0
LON_MIN, LON_MAX = -9.6, 4.5

## El PDF escribe algunos nombres distinto que el archivo de coordenadas.
ALIAS_PDF = {
    "Agavanzal, Nª Sª de":    "Nuestra Señora de Agavanzal",
    "Castro, El":             "Castro, El",
    "Loteta (La)":            "La Loteta",
    "Parras (Las)":           "Las Parras",
    "Vellón,El-(Pedrezuela)": "El Vellón (Pedrezuela)",
}


# --- Emparejamiento de nombres.
def clave(nombre) -> str:
    """Sin tildes, minúsculas y solo letras y números: 'Viñuela, La' -> 'vinuelala'."""
    s = unicodedata.normalize("NFKD", str(nombre)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]", "", s)


_ALIAS = {clave(k): v for k, v in ALIAS_PDF.items()}


def nombre_en_coordenadas(nombre: str) -> str:
    """Alias conocidos y 'Viñuela, La' -> 'La Viñuela'."""
    nombre = str(nombre).split("\n")[0].strip()
    if clave(nombre) in _ALIAS:
        return _ALIAS[clave(nombre)]
    m = re.match(r"^(.*), (El|La|Los|Las|A|O|Os|As)$", nombre)
    return f"{m.group(2)} {m.group(1)}" if m else nombre


# --- Limpieza de números.
def parse_num(s):
    if pd.isna(s):
        return np.nan
    s = str(s).split("\n")[0]
    s = re.sub(r"[^\d,.\-]", "", s)
    if not s or s == "-":
        return np.nan
    if "," in s and "." in s:
        i = max(s.rfind(","), s.rfind("."))
        int_part = re.sub(r"[.,]", "", s[:i])
        frac = re.sub(r"[^\d]", "", s[i + 1:])
        s_norm = f"{int_part}.{frac}" if frac != "" else int_part
    elif "," in s:
        if re.search(r",\d{1,2}$", s):
            s_norm = s.replace(".", "").replace(",", ".")
        else:
            s_norm = s.replace(",", "")
    else:
        s_norm = s.replace(".", "")
    try:
        return float(s_norm)
    except ValueError:
        return np.nan


# --- Procesamiento.
def leer_tablas(pdf) -> pd.DataFrame:
    """Todas las tablas con 'Embalses' desde la página 4 hasta el final."""
    try:
        tablas = camelot.read_pdf(str(pdf), pages="4-end", flavor="lattice")
    except Exception as e:
        raise comun.ErrorPaso("procesamiento", f"camelot no puede leer {pdf.name}: {e!r}")
    dfs = [t.df.copy() for t in tablas if t.df.astype(str).apply(lambda x: x.str.contains("Embalses")).any().any()]
    ## camelot deja el PDF abierto; sin esto Windows no puede borrar la carpeta temporal.
    del tablas
    gc.collect()
    if not dfs:
        raise comun.ErrorPaso("procesamiento", f"No hay ninguna tabla de embalses en {pdf.name}")
    malas = [d.shape for d in dfs if d.shape[1] != len(COLUMNAS_PDF)]
    if malas:
        raise comun.ErrorPaso("procesamiento", f"Tablas con un número de columnas inesperado: {malas}")

    embalses = pd.concat(dfs, ignore_index=True)
    embalses.columns = COLUMNAS_PDF
    cabecera = embalses.apply(lambda r: r.astype(str).str.contains("Embalses|Agua Embalsada|Energía Disponible|Capacidad", case=False).any(), axis=1)
    embalses = embalses[~cabecera]
    embalses["Embalse_PDF"] = embalses["Embalse_PDF"].astype(str).str.split("\n").str[0].str.strip()
    embalses = embalses[embalses["Embalse_PDF"] != ""].reset_index(drop=True)
    for col in COLUMNAS_PDF[2:]:
        embalses[col] = embalses[col].apply(parse_num)
        if (embalses[col].dropna() % 1 == 0).all():
            embalses[col] = embalses[col].astype("Int64")
    embalses["Porcentaje_Actual"] = np.where(embalses["Capacidad"] > 0,
                                             embalses["Actual"] / embalses["Capacidad"] * 100, np.nan).round(1)
    return embalses


def procesar(pdf) -> tuple[pd.DataFrame, date]:
    """Tabla con todos los embalses del PDF y, si las hay, sus coordenadas."""
    fecha = comun.fecha_portada(pdf)
    embalses = leer_tablas(pdf)

    coordenadas = pd.read_csv(RUTA_COORDENADAS, encoding="utf-8")
    coordenadas["clave"] = coordenadas["Embalses"].map(clave)
    embalses["clave"] = embalses["Embalse_PDF"].map(nombre_en_coordenadas).map(clave)
    embalses = embalses.merge(coordenadas, on="clave", how="left").drop(columns=["clave"])
    print(f"  {fecha}: {len(embalses)} embalses leídos")
    return embalses, fecha


def validar(embalses: pd.DataFrame, fecha: date, ultima: date | None, n_anterior: int | None,
            config: dict, hoy: date | None = None) -> pd.DataFrame:
    """Devuelve las filas que irán al mapa (las que tienen coordenadas)."""
    print("Validación:")
    v = comun.Validador()
    reglas = config["validacion"]
    v.fecha(fecha, ultima, reglas["dias_max_antiguedad"], hoy)

    v.comprobar(embalses["Embalse_PDF"].is_unique, "Sin embalses repetidos en el PDF",
                str(embalses.loc[embalses["Embalse_PDF"].duplicated(), "Embalse_PDF"].tolist()))
    v.comprobar(embalses["Porcentaje_Actual"].notna().all(), "Porcentaje calculado en todos los embalses",
                str(embalses.loc[embalses["Porcentaje_Actual"].isna(), "Embalse_PDF"].tolist()))
    v.rango(embalses["Porcentaje_Actual"].dropna(), "Porcentaje de cada embalse")

    sin_coordenadas = embalses.loc[embalses["Latitude"].isna(), "Embalse_PDF"].tolist()
    if sin_coordenadas:
        v.aviso(f"{len(sin_coordenadas)} embalse(s) del PDF sin coordenadas, no saldrán en el mapa: {sin_coordenadas}")
    mapa = embalses[embalses["Latitude"].notna()]

    fuera = mapa[~mapa["Latitude"].between(LAT_MIN, LAT_MAX) | ~mapa["Longitude"].between(LON_MIN, LON_MAX)]
    conocidos = set(reglas["embalses_coordenadas_conocidas_mal"])
    fuera_conocidos = fuera[fuera["Embalses"].isin(conocidos)]["Embalses"].tolist()
    fuera_nuevos = fuera[~fuera["Embalses"].isin(conocidos)]["Embalses"].tolist()
    if fuera_conocidos:
        v.aviso(f"Coordenadas fuera de España ya conocidas: {fuera_conocidos}")
    v.comprobar(not fuera_nuevos, "Ninguna coordenada fuera de España (salvo las ya conocidas)", str(fuera_nuevos))

    if n_anterior:
        caida = (n_anterior - len(mapa)) / n_anterior * 100
        v.comprobar(caida <= reglas["embalses_caida_max_porcentaje"],
                    f"Embalses en el mapa: {len(mapa)} (antes {n_anterior}, "
                    f"caída {max(caida, 0):.1f} % <= {reglas['embalses_caida_max_porcentaje']} %)")
    else:
        print(f"  [--] Sin ejecución anterior con la que comparar ({len(mapa)} embalses en el mapa)")
    v.terminar()
    return mapa


def escribir(embalses: pd.DataFrame, mapa: pd.DataFrame, fecha: date, historico: pd.DataFrame | None):
    nuevas = embalses.copy()
    nuevas.insert(0, COL_FECHA, fecha)
    comun.escribir_atomico({
        comun.ruta_datos(PROCESO): comun.a_csv(mapa[COLUMNAS_GRAFICO]),
        comun.ruta_metadata(PROCESO): comun.metadata(PROCESO, fecha),
        comun.ruta_historico(PROCESO): comun.a_csv(comun.anadir_historico(historico, nuevas, COL_FECHA)),
    })


def principal(carpeta_tmp):
    config = comun.cargar_config()
    historico = comun.leer_csv(comun.ruta_historico(PROCESO), COL_FECHA)
    datos = comun.leer_csv(comun.ruta_datos(PROCESO))
    ultima = historico[COL_FECHA].max() if historico is not None else None

    print(f"1. Descarga (último dato guardado: {ultima})")
    pdfs = comun.buscar_nuevos(ultima, comun.DOC_EMBALSES, carpeta_tmp)
    if not pdfs:
        raise comun.SinNovedades(f"no hay informe posterior a {ultima}")

    print("2. Procesamiento (solo el informe más reciente)")
    embalses, fecha = procesar(pdfs[-1])
    if ultima is not None and fecha <= ultima:
        raise comun.SinNovedades(f"el informe más reciente ({fecha}) no es posterior a {ultima}")

    print("3. ", end="")
    mapa = validar(embalses, fecha, ultima, len(datos) if datos is not None else None, config)

    print("4. Escritura")
    escribir(embalses, mapa, fecha, historico)
    return fecha


if __name__ == "__main__":
    comun.ejecutar(PROCESO, principal)
