import re

import gspread
import numpy as np
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1FwPVTMoDBo-Mc5_XAj5ZUiB6pv7AzM6gADDf8s_MLAo"
TAB_NAME = "Actual"
TAB_COMERCIAL = "Comercial"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]

# La columna Bloque mezcla lotes reales (ej. "2A1", "1M1-2M2") con 4
# categorias especiales que no son costeo por lote y todavia no se
# analizan en este dashboard (a pedido, se ven aparte mas adelante):
# Almacen (insumos en stock), Ganaderia y PRES IND (indirectos/gastos
# generales de la empresa). "Venta" tambien se excluye aca aunque no se
# pidio explicitamente: son filas de ingreso (venta de granos, en signo
# negativo), no de costo, y mezclarlas en un grafico de costos/ha resta
# en vez de sumar. Si hace falta incluirlas para otra vista, se saca de
# este set.
BLOQUES_EXCLUIDOS = {"PRES IND", "ALMACEN", "VENTA", "GANADERIA"}

# Unifica variantes de mayusculas/tildes de Cultivo vistas en la planilla.
# "CN" se deja sin mapear (no se pudo confirmar que cultivo/uso es) para
# que quede visible en vez de adivinar mal.
CULTIVO_MAP = {
    "CEBADA": "Cebada",
    "GIRASOL": "Girasol",
    "MAIZ": "Maíz",
    "MAIZ T": "Maíz Tardío",
    "PASTURA": "Pastura",
    "PASTURA MANTENIMIENTOS": "Pastura (mantenimiento)",
    "SOJA": "Soja",
    "SOJA 2°": "Soja 2ª",
    "TRIGO": "Trigo",
    "VERDEO": "Verdeo",
    "VERDEO DE INVIERNO": "Verdeo de invierno",
    "VI": "Verdeo de invierno",
}


def _parse_numero_uno(v):
    """Con UNFORMATTED_VALUE, gspread ya devuelve numeros como float/int de
    Python para celdas numericas; solo las celdas realmente de texto llegan
    como str. Aplicar el reemplazo de formato argentino (punto de miles,
    coma decimal) sobre un float ya parseado los destruye (92.0 -> "920"),
    asi que cada valor se trata segun su tipo real en vez de forzar todo a
    texto primero."""
    if isinstance(v, bool):
        return np.nan
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if s == "" or s.lower() == "nan":
        return np.nan
    s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return np.nan


def _parse_numero_ar(serie: pd.Series) -> pd.Series:
    return serie.apply(_parse_numero_uno)


@st.cache_resource
def _get_client() -> gspread.Client:
    creds = Credentials.from_service_account_info(
        dict(st.secrets["gcp_service_account"]), scopes=SCOPES
    )
    return gspread.authorize(creds)


@st.cache_data(ttl=600, show_spinner="Cargando datos de la planilla...")
def load_actual_df() -> pd.DataFrame:
    ws = _get_client().open_by_key(SPREADSHEET_ID).worksheet(TAB_NAME)
    records = ws.get_all_records(value_render_option="UNFORMATTED_VALUE")
    df = pd.DataFrame(records)
    df.columns = df.columns.str.strip()

    df["Bloque_norm"] = (
        df["Bloque"].astype(str).str.strip().str.upper().str.replace(r"\s+", " ", regex=True)
    )
    df = df[~df["Bloque_norm"].isin(BLOQUES_EXCLUIDOS)].copy()

    # OJO: aca NO se filtra por real/presup. Los costos deben usar solo lo
    # real (ver solo_reales()), pero la superficie cosechada se pide con
    # real+presupuestada juntas (varios cultivos todavia no tienen ninguna
    # fila de Cosecha real cargada), asi que cada uso decide que subconjunto
    # de este DataFrame necesita en vez de perder las filas de presupuesto
    # aca arriba para todos los casos.
    df["RealPresup"] = df["real/presup"].astype(str).str.strip()

    df["Gestion"] = df["Gestion"].astype(str).str.strip()
    df["Cultivo_norm"] = df["Cultivo"].astype(str).str.strip().str.upper()
    df["Cultivo"] = df["Cultivo_norm"].map(CULTIVO_MAP).fillna(df["Cultivo_norm"].str.title())
    df["Tipo"] = df["Tipo"].astype(str).str.strip()
    df["Tipo_norm"] = df["Tipo"].str.upper()

    df["Sup"] = _parse_numero_ar(df["Sup/cant"])
    df["Dosis"] = _parse_numero_ar(df["Dosis"])
    df["Tot"] = _parse_numero_ar(df["Tot"])
    df["Costo $"] = _parse_numero_ar(df["$ total"])
    df["Costo U$"] = _parse_numero_ar(df["U$ tot sin IVA"])

    # "U$/ha" a veces quedo mal tipeada en la planilla original y Sheets la
    # auto-formateo como fecha (ej. "30-dic-99"); con UNFORMATTED_VALUE eso
    # vuelve como el numero de serie de esa fecha, no como el U$/ha real.
    # Se descartan valores fuera de un rango plausible en vez de mostrar un
    # numero inventado.
    costo_ha = _parse_numero_ar(df["U$/ha"])
    df["Costo U$/ha"] = costo_ha.where(costo_ha.between(-2000, 2000))

    return df.reset_index(drop=True)


def solo_reales(df: pd.DataFrame) -> pd.DataFrame:
    """Filas reales (no presupuestadas). Usar esto para cualquier calculo
    de COSTO; la superficie cosechada es la unica excepcion que pide sumar
    real+presupuestada (ver superficie_cosechada)."""
    return df[df["RealPresup"] == "realiz"]


def valores_cultivo_no_mapeados(df: pd.DataFrame) -> list[str]:
    """Valores de Cultivo que no estan en CULTIVO_MAP, para auditar si
    aparece algo nuevo en la planilla que convenga sumar al diccionario."""
    return sorted(set(df["Cultivo_norm"].dropna()) - set(CULTIVO_MAP))


def costo_total(df: pd.DataFrame, moneda: str = "U$") -> pd.DataFrame:
    col = "Costo U$" if moneda == "U$" else "Costo $"
    return (
        df.groupby(["Gestion", "Bloque_norm", "Cultivo"], as_index=False)[col]
        .sum()
        .rename(columns={col: f"Costo total ({moneda})", "Bloque_norm": "Bloque"})
    )


def costo_por_tipo(df: pd.DataFrame, moneda: str = "U$") -> pd.DataFrame:
    col = "Costo U$" if moneda == "U$" else "Costo $"
    return (
        df.groupby(["Gestion", "Tipo"], as_index=False)[col]
        .sum()
        .rename(columns={col: f"Costo total ({moneda})"})
    )


def costo_por_ha(df: pd.DataFrame, by: list[str] = ("Gestion", "Cultivo"), moneda: str = "U$") -> pd.DataFrame:
    """Costo por hectarea SEMBRADA, ponderado por superficie: sum(costo) /
    sum(Sup), agrupando por lote+cultivo primero para no duplicar la
    superficie de un mismo lote entre varias filas de insumos/labores."""
    col = "Costo U$" if moneda == "U$" else "Costo $"
    por_lote = (
        df.groupby(list(by) + ["Bloque_norm"], as_index=False)
        .agg(_costo=(col, "sum"), _sup=("Sup", "max"))
    )
    grouped = por_lote.groupby(list(by), as_index=False).agg(
        _costo=("_costo", "sum"), _sup=("_sup", "sum")
    )
    grouped[f"Costo/ha ({moneda})"] = grouped["_costo"] / grouped["_sup"]
    return grouped.drop(columns=["_costo", "_sup"])


def costo_y_sup_por_lote(df: pd.DataFrame, moneda: str = "U$") -> pd.DataFrame:
    """Costo total y superficie SEMBRADA crudos, uno por (Bloque, Cultivo)
    sin agregar entre lotes (a diferencia de costo_por_ha, que suma todos
    los lotes de un cultivo). Se devuelven ambos componentes por separado
    (no ya divididos) para poder calcular despues un promedio ponderado
    por columna (sum(Costo)/sum(Sup)) en vez de solo el ratio por lote."""
    col = "Costo U$" if moneda == "U$" else "Costo $"
    por_lote = df.groupby(["Bloque_norm", "Cultivo"], as_index=False).agg(
        Costo=(col, "sum"), Sup=("Sup", "max")
    )
    return por_lote.rename(columns={"Bloque_norm": "Bloque"})


def costo_por_tipo_y_lote(df: pd.DataFrame, moneda: str = "U$") -> pd.DataFrame:
    """Costo total y superficie sembrada por (Bloque, Cultivo, Tipo), para
    desglosar el costo/ha de cada lote+cultivo por tipo de gasto. La
    superficie es la misma para todos los Tipo de un mismo lote+cultivo
    (viene de Sup/cant, constante en todas sus filas de insumos/labores)."""
    col = "Costo U$" if moneda == "U$" else "Costo $"
    costo = (
        df.groupby(["Bloque_norm", "Cultivo", "Tipo"], as_index=False)[col]
        .sum()
        .rename(columns={"Bloque_norm": "Bloque", col: "Costo"})
    )
    sup = (
        df.groupby(["Bloque_norm", "Cultivo"], as_index=False)["Sup"]
        .max()
        .rename(columns={"Bloque_norm": "Bloque"})
    )
    return costo.merge(sup, on=["Bloque", "Cultivo"], how="left")


def superficie_cosechada(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    """Hectareas cosechadas: suma de Sup/cant en filas con Tipo = Cosecha,
    sumando REAL + PRESUPUESTADA (a pedido: la mayoria de los cultivos
    todavia no tienen ninguna fila de Cosecha real cargada, asi que exigir
    solo real dejaria casi todo en 0). Ojo: para ~1 de cada 5 lotes que
    tienen ambas, la fila real NO es un subconjunto de la presupuestada
    (a veces la real es varias veces mas grande que la presupuestada, ej.
    lote 8B1/Soja 2ª: presup=11,23 ha vs realiz=252,77 ha) - sumarlas
    puede sobreestimar la superficie en esos casos. Pasa `df` ya filtrado
    con solo_reales() si en algun momento se necesita la version solo-real."""
    cosecha = df[df["Tipo_norm"] == "COSECHA"]
    return (
        cosecha.groupby(list(by), as_index=False)["Sup"]
        .sum()
        .rename(columns={"Sup": "Superficie cosechada (ha)"})
    )


def costo_por_ha_cosechada(
    df_costo: pd.DataFrame,
    df_sup: pd.DataFrame,
    cost_by: list[str],
    sup_by: list[str],
    moneda: str = "U$",
) -> pd.DataFrame:
    """Costo (solo real, agrupado por cost_by) sobre hectareas cosechadas
    (real+presupuestada, agrupadas por sup_by via superficie_cosechada;
    sup_by debe ser subconjunto de cost_by: ej. cost_by=["Gestion","Tipo"]
    y sup_by=["Gestion"] reparte la misma superficie de la campana entre
    cada Tipo de gasto). df_costo debe venir ya filtrado con solo_reales().
    Un merge "inner" contra la superficie descarta combinaciones sin
    ninguna fila de Cosecha en vez de mostrar una division por cero."""
    col = "Costo U$" if moneda == "U$" else "Costo $"
    costo = df_costo.groupby(list(cost_by), as_index=False)[col].sum().rename(columns={col: "_costo"})
    sup = superficie_cosechada(df_sup, sup_by)
    merged = costo.merge(sup, on=list(sup_by), how="inner")
    merged[f"Costo/ha cosechada ({moneda})"] = merged["_costo"] / merged["Superficie cosechada (ha)"]
    return merged.drop(columns=["_costo"])


_CAMPANA_COMERCIAL_RE = re.compile(r"^(\d{4})-(\d{4})$")


def _gestion_desde_campana_comercial(label: str) -> str | None:
    m = _CAMPANA_COMERCIAL_RE.match(str(label).strip())
    if not m:
        return None
    return f"{m.group(1)[-2:]}/{m.group(2)[-2:]}"


@st.cache_data(ttl=600, show_spinner="Cargando pestaña Comercial...")
def load_comercial_df() -> pd.DataFrame:
    """Pestaña "Comercial": uno o mas bloques por campana, UNO AL LADO DEL
    OTRO en columnas (ej. "2026-2027" arranca en la columna A y "2025-2026"
    en la J, no una debajo de la otra). Cada bloque tiene su fila de
    campana ("2026-2027"), la fila siguiente con los nombres de cultivo, y
    despues filas de metrica (Rendimiento, Precio Neto, Arrendamiento,
    Flete, ...) en formato ancho (una columna por cultivo). No es una
    tabla plana como "Actual", asi que se parsea a mano en vez de
    get_all_records (que ademas falla aca por headers duplicados vacios)."""
    ws = _get_client().open_by_key(SPREADSHEET_ID).worksheet(TAB_COMERCIAL)
    values = ws.get_values(value_render_option="UNFORMATTED_VALUE")
    ancho = max((len(r) for r in values), default=0)
    values = [r + [""] * (ancho - len(r)) for r in values]

    filas = []
    bloques: list[tuple[str, int, int]] = []  # (gestion, col_etiqueta, col_fin_exclusivo)
    cultivos_por_bloque: dict[int, list[str]] = {}
    i = 0
    while i < len(values):
        row = values[i]
        inicios = sorted(
            (j, g) for j, v in enumerate(row) if (g := _gestion_desde_campana_comercial(v))
        )
        if inicios:
            limites = [col for col, _ in inicios] + [ancho]
            bloques = [(gestion, col, limites[k + 1]) for k, (col, gestion) in enumerate(inicios)]
            i += 1
            fila_cultivos = values[i] if i < len(values) else [""] * ancho
            cultivos_por_bloque = {col: fila_cultivos[col + 1 : fin] for _, col, fin in bloques}
            i += 1
            continue
        for gestion, col, fin in bloques:
            etiqueta = str(row[col]).strip() if col < len(row) else ""
            if not etiqueta:
                continue
            for k, cultivo_raw in enumerate(cultivos_por_bloque.get(col, [])):
                col_idx = col + 1 + k
                cultivo_raw = str(cultivo_raw).strip()
                val = row[col_idx] if col_idx < len(row) else ""
                if not cultivo_raw or val == "" or val is None:
                    continue
                filas.append(
                    {"Gestion": gestion, "Cultivo_raw": cultivo_raw, "Metrica": etiqueta, "Valor": val}
                )
        i += 1

    df = pd.DataFrame(filas, columns=["Gestion", "Cultivo_raw", "Metrica", "Valor"])
    if df.empty:
        return df
    df["Cultivo_norm"] = df["Cultivo_raw"].astype(str).str.strip().str.upper()
    df["Cultivo"] = df["Cultivo_norm"].map(CULTIVO_MAP).fillna(df["Cultivo_norm"].str.title())
    df["Valor"] = df["Valor"].apply(_parse_numero_uno)
    return df


def comercial_lookup(df_comercial: pd.DataFrame) -> dict[tuple[str, str, str], float]:
    """dict {(Gestion, Cultivo, Metrica): Valor} para completar los
    supuestos de la seccion Resultado con lo que ya esta cargado en la
    pestaña Comercial (Rendimiento, Precio Neto, Arrendamiento, Flete)."""
    if df_comercial.empty:
        return {}
    return {
        (r.Gestion, r.Cultivo, r.Metrica): r.Valor for r in df_comercial.itertuples(index=False)
    }
