"""
Código compartido por los tres procesos (linea, cuencas, embalses).

No guarda ningún estado: cada proceso lo importa por su cuenta, así que si uno falla
los otros no se ven afectados.
"""

import json
import os
import re
import sys
import tempfile
import traceback
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pdfplumber
import requests
import tabula

RAIZ = Path(__file__).resolve().parent.parent
DIR_GRAFICOS = RAIZ / "graficos"
DIR_HISTORICOS = RAIZ / "historicos"
DIR_REFERENCIAS = RAIZ / "referencias"
RUTA_CONFIG = RAIZ / "config" / "config.json"

# Se puede cambiar con la variable de entorno EMBALSES_URL_BASE (sirve para forzar fallos en pruebas).
URL_BASE = os.environ.get("EMBALSES_URL_BASE", "https://sede.miteco.gob.es/BoleHWeb/accion/cargador_archivo.htm")

DOC_BOLETIN = 40    # Boletín Hidrológico semanal: reserva total (pág. 5) y por ámbitos (pág. 7)
DOC_EMBALSES = 60   # Reserva hidráulica: desglose por embalses

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
         "agosto", "septiembre", "octubre", "noviembre", "diciembre"]


# --- Errores.
class ErrorPaso(Exception):
    """Fallo en un paso concreto (descarga, procesamiento, validación, escritura)."""
    def __init__(self, paso: str, mensaje: str):
        super().__init__(mensaje)
        self.paso = paso


class SinNovedades(Exception):
    """No hay boletín más nuevo que el último dato guardado. No es un error."""


# --- Rutas y configuración.
def ruta_datos(proceso: str) -> Path:
    return DIR_GRAFICOS / proceso / "datos.csv"


def ruta_metadata(proceso: str) -> Path:
    return DIR_GRAFICOS / proceso / "metadata.json"


def ruta_historico(proceso: str) -> Path:
    return DIR_HISTORICOS / f"{proceso}.csv"


def cargar_config() -> dict:
    with open(RUTA_CONFIG, encoding="utf-8") as f:
        return json.load(f)


def leer_csv(ruta: Path, col_fecha: str | None = None) -> pd.DataFrame | None:
    """Lee un CSV del repositorio. None si todavía no existe."""
    if not ruta.exists():
        return None
    df = pd.read_csv(ruta, encoding="utf-8")
    if col_fecha:
        df[col_fecha] = pd.to_datetime(df[col_fecha], format="%Y-%m-%d").dt.date
    return df


# --- Fechas.
def fecha_es(d: date) -> str:
    """date(2026, 10, 5) -> '5 de octubre de 2026'."""
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


def fecha_cierre(anio: int, semana: int) -> date:
    """El boletín nº N cubre hasta el lunes de la semana ISO N+1."""
    return date.fromisocalendar(anio, semana, 1) + timedelta(days=7)


def boletin_de(fecha: date) -> tuple[int, int]:
    """Año y número del boletín que cierra en esa fecha (inverso de fecha_cierre)."""
    anio, semana, _ = (fecha - timedelta(days=7)).isocalendar()
    return anio, semana


# --- Descarga.
def url_documento(anio: int, semana: int, doc: int) -> str:
    return f"{URL_BASE}?file=cache/pdf/{anio}{semana:02d}/{anio}{semana:02d}{doc}_es.pdf&mimetype=application/pdf"


def descargar(anio: int, semana: int, doc: int, carpeta: Path) -> Path | None:
    """Descarga un documento. None si aún no está publicado (el servidor devuelve 0 bytes)."""
    url = url_documento(anio, semana, doc)
    try:
        r = requests.get(url, timeout=60)
    except requests.RequestException as e:
        raise ErrorPaso("descarga", f"El servidor no responde al pedir el documento {doc} de {anio}-{semana:02d}: {e}")
    if r.status_code != 200:
        raise ErrorPaso("descarga", f"HTTP {r.status_code} al pedir el documento {doc} de {anio}-{semana:02d} ({url})")
    if len(r.content) == 0:
        return None
    if not r.content.startswith(b"%PDF"):
        raise ErrorPaso("descarga", f"El documento {doc} de {anio}-{semana:02d} no es un PDF ({len(r.content)} bytes)")
    ruta = carpeta / f"doc{doc}_{anio}_{semana:02d}.pdf"
    ruta.write_bytes(r.content)
    print(f"  Descargado documento {doc} de {anio}-{semana:02d}")
    return ruta


def buscar_nuevos(ultima: date | None, doc: int, carpeta: Path, hoy: date | None = None) -> list[Path]:
    """
    Descarga todos los documentos publicados posteriores a 'ultima', en orden.
    Recorre semana a semana hasta la actual; las semanas aún no publicadas se saltan.
    Si 'ultima' es None, busca solo el más reciente de las últimas 6 semanas.
    """
    hoy = hoy or date.today()
    if ultima is None:
        ultima = hoy - timedelta(weeks=6)
    pdfs = []
    cierre = ultima + timedelta(days=7)
    while cierre <= hoy + timedelta(days=7):
        anio, semana = boletin_de(cierre)
        ruta = descargar(anio, semana, doc, carpeta)
        if ruta:
            pdfs.append(ruta)
        cierre += timedelta(days=7)
    return pdfs


# --- Lectura de PDF.
def fecha_portada(ruta_pdf: Path) -> date:
    """Fecha final del periodo que figura en la portada: 'hasta el 5 de octubre de 2026'."""
    with pdfplumber.open(ruta_pdf) as pdf:
        texto = pdf.pages[0].extract_text() or ""
    m = re.search(r"hasta el (\d{1,2}) de (\w+) de (\d{4})", texto)
    if not m or m.group(2).lower() not in MESES:
        raise ErrorPaso("procesamiento", f"No encuentro la fecha 'hasta el ...' en la portada de {ruta_pdf.name}")
    return date(int(m.group(3)), MESES.index(m.group(2).lower()) + 1, int(m.group(1)))


def a_decimal(x) -> float:
    """'53,1' / '53.1' / 53.1 -> 53.1 (porcentajes, sin separador de miles)."""
    if pd.isna(x):
        return np.nan
    if isinstance(x, (int, float, np.number)):
        return float(x)
    try:
        return float(str(x).strip().replace(",", "."))
    except ValueError:
        return np.nan


def leer_tabla(ruta_pdf: Path, pagina: int, **opciones) -> list[pd.DataFrame]:
    """tabula en modo subproceso (funciona con Java 8 o superior)."""
    return tabula.read_pdf(str(ruta_pdf), pages=pagina, multiple_tables=True,
                           force_subprocess=True, silent=True, **opciones)


def porcentajes_pagina5(ruta_pdf: Path) -> dict:
    """Fila TOTAL de la tabla de reserva hidráulica (página 5 del boletín)."""
    try:
        reservas = leer_tabla(ruta_pdf, 5)[0]
        reservas = reservas.rename(columns={"Unnamed: 1": "Capacidad actual", "% año anterior": "Año anterior",
                                            "% Med.5": "Media 5 años", "% Med.10": "Media 10 años"})
        total = reservas[reservas.iloc[:, 0].astype(str).str.strip().str.upper() == "TOTAL"].iloc[0]
        return {col: a_decimal(total[col]) for col in ["Capacidad actual", "Año anterior", "Media 5 años", "Media 10 años"]}
    except ErrorPaso:
        raise
    except Exception as e:
        raise ErrorPaso("procesamiento", f"No se puede leer la tabla de la página 5 de {ruta_pdf.name}: {e!r}")


# --- Validación.
class Validador:
    """Va apuntando cada comprobación; al final lanza un error si alguna ha fallado."""

    def __init__(self):
        self.errores, self.avisos = [], []

    def comprobar(self, ok: bool, descripcion: str, detalle: str = ""):
        print(f"  [{'OK' if ok else 'FALLO'}] {descripcion}{'' if ok or not detalle else ' -> ' + detalle}")
        if not ok:
            self.errores.append(f"{descripcion}{': ' + detalle if detalle else ''}")

    def aviso(self, mensaje: str):
        print(f"  [AVISO] {mensaje}")
        self.avisos.append(mensaje)

    def fecha(self, nueva: date, ultima: date | None, dias_max: int, hoy: date | None = None):
        hoy = hoy or date.today()
        if ultima is not None:
            self.comprobar(nueva > ultima, f"Fecha {nueva} posterior al último dato guardado ({ultima})")
        self.comprobar(nueva >= hoy - timedelta(days=dias_max),
                       f"Fecha {nueva} como mucho {dias_max} días anterior a hoy ({hoy})")

    def rango(self, valores: pd.Series, descripcion: str, minimo=0, maximo=100):
        fuera = valores[(valores < minimo) | (valores > maximo)]
        self.comprobar(fuera.empty, f"{descripcion} entre {minimo} y {maximo}",
                       f"{len(fuera)} fuera de rango: {fuera.head(5).tolist()}")

    def terminar(self):
        if self.errores:
            raise ErrorPaso("validación", " | ".join(self.errores))


# --- Escritura.
def a_csv(df: pd.DataFrame) -> str:
    """CSV con punto decimal, fechas AAAA-MM-DD y huecos vacíos."""
    df = df.copy()
    for col in df.columns:
        no_nulos = df[col].dropna()
        if not no_nulos.empty and isinstance(no_nulos.iloc[0], date):
            df[col] = df[col].map(lambda d: d.isoformat() if isinstance(d, date) else "")
    return df.to_csv(index=False, lineterminator="\n")


def metadata(proceso: str, fecha: date) -> str:
    """{"annotate": {"notes": "..."}} plano: así es como Datawrapper lo lee."""
    nota = cargar_config()["notas"][proceso].format(fecha=fecha_es(fecha))
    return json.dumps({"annotate": {"notes": nota}}, ensure_ascii=False, indent=2) + "\n"


def anadir_historico(historico: pd.DataFrame | None, nuevas: pd.DataFrame, col_fecha: str) -> pd.DataFrame:
    """Añade filas al histórico sin sobrescribir nunca fechas que ya existen."""
    if historico is None or historico.empty:
        return nuevas.reset_index(drop=True)
    nuevas = nuevas[~nuevas[col_fecha].isin(set(historico[col_fecha]))]
    return pd.concat([historico, nuevas], ignore_index=True)


def escribir_atomico(archivos: dict[Path, str]):
    """
    Escribe primero todos los archivos en temporales junto a su destino y, solo si
    todos se han escrito bien, los sustituye de golpe con os.replace.
    """
    temporales = {}
    try:
        for destino, contenido in archivos.items():
            destino.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=f".{destino.name}.", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(contenido)
            temporales[destino] = Path(tmp)
    except Exception as e:
        for tmp in temporales.values():
            tmp.unlink(missing_ok=True)
        raise ErrorPaso("escritura", f"No se han podido escribir los archivos temporales: {e!r}")
    for destino, tmp in temporales.items():
        os.replace(tmp, destino)
        print(f"  Escrito {destino.relative_to(RAIZ)}")


# --- Ejecución.
def _salida_github(**valores):
    """Pasa valores al workflow (fecha del dato para el mensaje de commit)."""
    ruta = os.environ.get("GITHUB_OUTPUT")
    if ruta:
        with open(ruta, "a", encoding="utf-8") as f:
            for k, v in valores.items():
                f.write(f"{k}={v}\n")


def ejecutar(proceso: str, principal):
    """
    Ejecuta principal(carpeta_temporal) -> fecha del dato publicado.
    Los PDF se descargan en una carpeta temporal que se borra al terminar.
    Sale con código 0 si todo va bien o no hay novedades, y 1 si algo falla.
    """
    print(f"===== {proceso} =====")
    carpeta = tempfile.TemporaryDirectory(prefix=f"embalses_{proceso}_", ignore_cleanup_errors=True)
    try:
        with carpeta as tmp:
            fecha = principal(Path(tmp))
    except SinNovedades as e:
        print(f"[{proceso}] Sin novedades: {e}. No se cambia ningún archivo.")
        _salida_github(cambios="no")
        sys.exit(0)
    except ErrorPaso as e:
        mensaje = f"[{proceso}] FALLO en el paso '{e.paso}': {e}. No se ha cambiado ningún archivo."
        print(f"::error title={proceso} - fallo en {e.paso}::{e}")
        print(mensaje, file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        traceback.print_exc()
        print(f"::error title={proceso} - error inesperado::{e!r}")
        print(f"[{proceso}] FALLO inesperado: {e!r}. No se ha cambiado ningún archivo.", file=sys.stderr)
        sys.exit(1)
    finally:
        if Path(carpeta.name).exists():
            print(f"[{proceso}] AVISO: no se ha podido borrar la carpeta temporal {carpeta.name}")
    print(f"[{proceso}] OK. Datos a {fecha.isoformat()}.")
    _salida_github(cambios="si", fecha=fecha.isoformat())
