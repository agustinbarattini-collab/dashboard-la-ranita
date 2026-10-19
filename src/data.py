import re
from datetime import datetime

import gspread
import numpy as np
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1FwPVTMoDBo-Mc5_XAj5ZUiB6pv7AzM6gADDf8s_MLAo"
TAB_NAME = "Actual"
TAB_COMERCIAL = "Comercial"
# Lectura + escritura: solo se escribe la pestana "Revisiones OK" (tildes de la
# seccion Revision de datos); igual necesita que la cuenta de servicio sea
# Editora de la planilla, con Lector la lectura sigue andando y escribir da 403.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

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

# Cultivos de 2da: se siembran sobre la misma superficie fisica que el cultivo
# de 1ra, asi que sus hectareas no se suman al total de hectareas del campo
# (su ingreso y su gasto si entran en los totales).
CULTIVOS_SIN_AREA_PROPIA = {"Soja 2ª"}

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


def superficie_cultivo_lote(df: pd.DataFrame) -> pd.DataFrame:
    """Superficie propia de cada cultivo dentro de cada lote y campana
    (Gestion, Bloque, Cultivo). Un mismo lote puede tener 2 cultivos en la
    misma campana (ej. Girasol y Soja en 2A1 25/26, o Trigo y Soja 2ª), y
    cada fila de insumo/labor trae en Sup/cant solo la superficie de ESA
    pasada (herbicidas de barbecho pueden cubrir mas hectareas que las
    realmente sembradas con el cultivo), asi que tomar el Sup maximo del
    grupo sobreestima la superficie del cultivo y subestima su costo/ha.

    Prioridad: 1) suma de Sup en filas Cosecha (real+presupuestada, mismo
    criterio que superficie_cosechada); 2) si el cultivo no tiene Cosecha
    cargada, suma de Sup en filas Siembra; 3) si tampoco, el Sup maximo del
    grupo (pasturas/verdeos sin siembra ni cosecha)."""
    det = detalle_superficie_cultivo_lote(df)
    return det[["Gestion", "Bloque", "Cultivo", "Superficie"]].rename(columns={"Superficie": "Sup"})


def detalle_superficie_cultivo_lote(df: pd.DataFrame) -> pd.DataFrame:
    """Mismo calculo que superficie_cultivo_lote pero mostrando las 3
    candidatas (Cosecha, Siembra, Sup max), la elegida y su Fuente."""
    keys = ["Gestion", "Bloque_norm", "Cultivo"]
    maximo = df.groupby(keys)["Sup"].max()
    det = pd.DataFrame(
        {
            "Cosecha": df[df["Tipo_norm"] == "COSECHA"].groupby(keys)["Sup"].sum().reindex(maximo.index),
            "Siembra": df[df["Tipo_norm"] == "SIEMBRA"].groupby(keys)["Sup"].sum().reindex(maximo.index),
            "Sup máx": maximo,
        }
    )
    cos_ok = det["Cosecha"].where(det["Cosecha"] > 0)
    sie_ok = det["Siembra"].where(det["Siembra"] > 0)
    det["Superficie"] = cos_ok.fillna(sie_ok).fillna(det["Sup máx"])
    det["Fuente"] = np.where(cos_ok.notna(), "Cosecha", np.where(sie_ok.notna(), "Siembra", "Sup máx"))
    return det.reset_index().rename(columns={"Bloque_norm": "Bloque"})


def costo_por_tipo_y_lote(df: pd.DataFrame, moneda: str = "U$") -> pd.DataFrame:
    """Costo total y superficie por (Bloque, Cultivo, Tipo), para
    desglosar el costo/ha de cada lote+cultivo por tipo de gasto. La
    superficie es la misma para todos los Tipo de un mismo lote+cultivo y
    es la propia de ese cultivo en ese lote (ver superficie_cultivo_lote).
    Si hay varias campanas seleccionadas, el costo y la superficie se
    suman entre campanas (cada campana usa su propia superficie)."""
    col = "Costo U$" if moneda == "U$" else "Costo $"
    costo = (
        df.groupby(["Bloque_norm", "Cultivo", "Tipo"], as_index=False)[col]
        .sum()
        .rename(columns={"Bloque_norm": "Bloque", col: "Costo"})
    )
    sup = superficie_cultivo_lote(df).groupby(["Bloque", "Cultivo"], as_index=False)["Sup"].sum()
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


TAB_REVISIONES = "Revisiones OK"


class SinPermisoEscritura(Exception):
    """La cuenta de servicio solo tiene permiso de lectura sobre la planilla."""


def clave_hallazgo(titulo: str, fila: pd.Series, id_cols: list[str]) -> str:
    """Identificador estable de un hallazgo de la seccion Revision: el titulo
    del chequeo + los valores de las columnas que lo identifican (no los
    numeros que cambian de un dia al otro, salvo que formen parte del id)."""
    return titulo + "|" + "|".join(str(fila[c]) for c in id_cols)


@st.cache_data(ttl=60, show_spinner=False)
def load_revisiones_ok() -> frozenset[str]:
    """Claves de hallazgos ya marcados como revisados (pestana "Revisiones
    OK"). Si la pestana todavia no existe, no hay ninguno."""
    sheet = _get_client().open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(TAB_REVISIONES)
    except gspread.WorksheetNotFound:
        return frozenset()
    return frozenset(v for v in ws.col_values(1)[1:] if v)


def marcar_revisiones(claves: list[tuple[str, str]]) -> None:
    """Guarda hallazgos como revisados: lista de (clave, titulo del chequeo).
    Crea la pestana la primera vez. Levanta SinPermisoEscritura si la cuenta
    de servicio no es Editora de la planilla."""
    try:
        sheet = _get_client().open_by_key(SPREADSHEET_ID)
        try:
            ws = sheet.worksheet(TAB_REVISIONES)
        except gspread.WorksheetNotFound:
            ws = sheet.add_worksheet(TAB_REVISIONES, rows=1000, cols=3)
            ws.append_row(["Clave", "Chequeo", "Marcado"])
        ahora = datetime.now().isoformat(timespec="seconds")
        ya_guardadas = set(ws.col_values(1))
        nuevas = {c: t for c, t in claves if c not in ya_guardadas}
        if nuevas:
            ws.append_rows([[c, t, ahora] for c, t in nuevas.items()], value_input_option="RAW")
    except gspread.exceptions.APIError as e:
        if e.response.status_code in (401, 403):
            raise SinPermisoEscritura from e
        raise
    load_revisiones_ok.clear()


def desmarcar_revisiones(claves: list[str]) -> None:
    """Quita hallazgos de la lista de revisados (vuelven a aparecer)."""
    try:
        ws = _get_client().open_by_key(SPREADSHEET_ID).worksheet(TAB_REVISIONES)
        quitar = set(claves)
        filas = [i for i, v in enumerate(ws.col_values(1), start=1) if i > 1 and v in quitar]
        for fila in reversed(filas):
            ws.delete_rows(fila)
    except gspread.WorksheetNotFound:
        return
    except gspread.exceptions.APIError as e:
        if e.response.status_code in (401, 403):
            raise SinPermisoEscritura from e
        raise
    load_revisiones_ok.clear()


# Umbrales de los chequeos de revision (ajustables si dan demasiado ruido).
UMBRAL_SIEMBRA_VS_COSECHA = 0.15
UMBRAL_COSTO_HA_LOTE = (0.5, 2.0)
UMBRAL_COSTO_FILA = 5.0
PISO_COSTO_FILA = 100.0
UMBRAL_REAL_VS_PRESUP = 3.0
UMBRAL_HAS_COMERCIAL = 0.02


def revisiones(df: pd.DataFrame, comercial: pd.DataFrame) -> list[dict]:
    """Chequeos de calidad de datos sobre la hoja Actual (ya sin los bloques
    excluidos) y la pestana Comercial. Cada item es un dict con titulo,
    detalle (que mirar / por que importa), nivel ("Revisar" = probable error
    de carga; "Info" = algo que el calculo ya interpreta pero conviene
    conocer) y tabla (DataFrame con lo encontrado; vacio = sin hallazgos)."""
    items: list[dict] = []

    def add(titulo: str, detalle: str, tabla: pd.DataFrame, nivel: str = "Revisar", id_cols: list[str] | None = None) -> None:
        tabla = tabla.reset_index(drop=True)
        claves = [clave_hallazgo(titulo, fila, id_cols or []) for _, fila in tabla.iterrows()] if len(tabla) else []
        items.append({"titulo": titulo, "detalle": detalle, "nivel": nivel, "tabla": tabla, "claves": claves})

    # 1. Nombres de cultivo que no estan en el diccionario
    sin_mapa = df[~df["Cultivo_norm"].isin(CULTIVO_MAP)]
    add(
        "Cultivo con nombre no reconocido",
        "El nombre no está en el diccionario de cultivos (o está vacío), así que aparece como "
        "un cultivo aparte. Si es un error de tipeo, corregilo en la planilla; si es un cultivo "
        "nuevo, hay que sumarlo al diccionario del dashboard.",
        sin_mapa.groupby(["Cultivo_norm", "Gestion"], as_index=False).agg(
            Filas=("Sup", "size"), Costo_U=("Costo U$", "sum")
        ).rename(columns={"Cultivo_norm": "Cultivo (como está escrito)", "Costo_U": "Costo U$"}),
        id_cols=["Cultivo (como está escrito)", "Gestion"],
    )

    # 2. Filas sin Tipo
    sin_tipo = df[df["Tipo"] == ""]
    add(
        "Filas sin Tipo de gasto",
        "No entran en ninguna categoría del desglose por Tipo.",
        sin_tipo[["Gestion", "Bloque_norm", "Cultivo", "Produc/labor", "Sup", "Costo U$"]],
        id_cols=["Gestion", "Bloque_norm", "Cultivo", "Produc/labor", "Sup"],
    )

    # 3. Filas con Tipo pero sin costo
    sin_costo = df[(df["Tipo"] != "") & df["Costo U$"].isna()]
    add(
        "Filas sin costo cargado",
        "Tienen Tipo pero el costo (U$ tot sin IVA) está vacío: no suman nada al costo.",
        sin_costo.groupby(["Gestion", "Bloque_norm", "Cultivo"], as_index=False).agg(Filas=("Tipo", "size")),
        id_cols=["Gestion", "Bloque_norm", "Cultivo"],
    )

    # 4/5. Superficie de cada cultivo en cada lote
    det = detalle_superficie_cultivo_lote(df)
    sin_cosecha = det[det["Fuente"] != "Cosecha"]
    add(
        "Lote + cultivo sin cosecha cargada",
        "No tiene filas de Cosecha (ni real ni presupuestada), así que su costo/ha se divide por "
        "la superficie sembrada o, si tampoco hay siembra, por la superficie máxima de sus filas. "
        "Normal en pasturas/verdeos; en cultivos de grano conviene cargar la cosecha.",
        sin_cosecha[["Gestion", "Bloque", "Cultivo", "Siembra", "Sup máx", "Superficie", "Fuente"]].round(1),
        nivel="Info",
        id_cols=["Gestion", "Bloque", "Cultivo"],
    )

    ambas = det[(det["Cosecha"] > 0) & (det["Siembra"] > 0)].copy()
    ambas["Dif %"] = (ambas["Siembra"] - ambas["Cosecha"]) / ambas["Cosecha"] * 100
    desvio = ambas[ambas["Dif %"].abs() > UMBRAL_SIEMBRA_VS_COSECHA * 100]
    add(
        "Siembra y cosecha no coinciden",
        f"Hectáreas sembradas vs cosechadas del mismo lote + cultivo difieren más de "
        f"{UMBRAL_SIEMBRA_VS_COSECHA:.0%}. Suele ser una siembra cargada dos veces, una resiembra, "
        "o una cosecha incompleta. El dashboard usa las hectáreas de cosecha.",
        desvio[["Gestion", "Bloque", "Cultivo", "Siembra", "Cosecha", "Dif %"]].round(1),
        id_cols=["Gestion", "Bloque", "Cultivo"],
    )

    # 6. Cosecha con filas reales y presupuestadas a la vez
    cos = df[df["Tipo_norm"] == "COSECHA"]
    cos_rp = cos.pivot_table(
        index=["Gestion", "Bloque_norm", "Cultivo"], columns="RealPresup", values="Sup", aggfunc="sum"
    )
    for col in ("realiz", "presup"):
        if col not in cos_rp.columns:
            cos_rp[col] = np.nan
    mixto = cos_rp[(cos_rp["realiz"] > 0) & (cos_rp["presup"] > 0)].reset_index()
    mixto["Suma usada (ha)"] = mixto["realiz"] + mixto["presup"]
    add(
        "Cosecha real y presupuestada a la vez",
        "El dashboard suma real + presupuestada. Si la fila presupuestada ya incluye lo que se "
        "cosechó (en vez de ser lo que falta), la superficie queda duplicada.",
        mixto.rename(columns={"Bloque_norm": "Bloque", "realiz": "Real (ha)", "presup": "Presup (ha)"})[
            ["Gestion", "Bloque", "Cultivo", "Real (ha)", "Presup (ha)", "Suma usada (ha)"]
        ].round(1),
        id_cols=["Gestion", "Bloque", "Cultivo"],
    )

    # 7. Lotes con 2+ cultivos en la misma campana
    multi = det.groupby(["Gestion", "Bloque"]).filter(lambda g: len(g) > 1)
    resumen = (
        multi.assign(txt=multi["Cultivo"] + ": " + multi["Superficie"].round(1).astype(str) + " ha")
        .groupby(["Gestion", "Bloque"], as_index=False)["txt"]
        .agg(" · ".join)
        .rename(columns={"txt": "Cultivos y superficie usada"})
    )
    add(
        "Lotes con 2 cultivos en la misma campaña",
        "El gasto de cada cultivo se divide por la superficie de ESE cultivo en el lote. Revisá "
        "que cada fila de gasto esté cargada con el cultivo correcto.",
        resumen,
        nivel="Info",
        id_cols=["Gestion", "Bloque"],
    )

    # 8. Bloques que no parecen codigo de lote
    raros = df[~df["Bloque_norm"].str.match(r"^\d")]
    add(
        "Lote con nombre que no parece un código de lote",
        "Los lotes empiezan con un número (ej. 2A1, 7B2-7R2). Estos no: pueden ser un cultivo "
        "o una nota cargada en la columna Bloque.",
        raros.groupby(["Gestion", "Bloque_norm"], as_index=False).agg(
            Filas=("Sup", "size"), Cultivos=("Cultivo", lambda s: ", ".join(sorted(set(s)))), Costo_U=("Costo U$", "sum")
        ).rename(columns={"Bloque_norm": "Bloque", "Costo_U": "Costo U$"}).round(0),
        id_cols=["Gestion", "Bloque"],
    )

    # 9. Costo/ha de un lote muy distinto al de su cultivo
    por_lote = df.groupby(["Gestion", "Bloque_norm", "Cultivo"], as_index=False)["Costo U$"].sum().rename(
        columns={"Bloque_norm": "Bloque"}
    )
    por_lote = por_lote.merge(det[["Gestion", "Bloque", "Cultivo", "Superficie"]], on=["Gestion", "Bloque", "Cultivo"])
    por_lote = por_lote[por_lote["Superficie"] > 0]
    por_lote["Costo/ha"] = por_lote["Costo U$"] / por_lote["Superficie"]
    mediana = por_lote.groupby(["Gestion", "Cultivo"])["Costo/ha"].transform("median")
    n_lotes = por_lote.groupby(["Gestion", "Cultivo"])["Costo/ha"].transform("size")
    por_lote["Mediana del cultivo"] = mediana
    por_lote["Veces la mediana"] = por_lote["Costo/ha"] / mediana
    bajo, alto = UMBRAL_COSTO_HA_LOTE
    atipico = por_lote[(n_lotes >= 4) & ((por_lote["Veces la mediana"] > alto) | (por_lote["Veces la mediana"] < bajo))]
    add(
        "Costo/ha de un lote muy distinto al de su cultivo",
        f"El costo/ha total del lote es más de {alto:g}x o menos de {bajo:g}x la mediana de su "
        "cultivo en esa campaña (solo cultivos con 4+ lotes). Puede ser un lote parcial o un "
        "error de carga (precio, cantidad o superficie).",
        atipico[["Gestion", "Bloque", "Cultivo", "Costo U$", "Superficie", "Costo/ha", "Mediana del cultivo", "Veces la mediana"]].round(1),
        id_cols=["Gestion", "Bloque", "Cultivo"],
    )

    # 10. Filas con costo/ha muy por encima de otras filas del mismo insumo/labor
    filas = df[(df["Sup"] > 0) & df["Costo U$"].notna() & (df["Costo U$"] > 0)].copy()
    filas["Producto"] = filas["Produc/labor"].astype(str).str.strip().str.lower()
    filas["Costo U$/ha de la fila"] = filas["Costo U$"] / filas["Sup"]
    g = filas.groupby(["Producto", "Tipo", "RealPresup"])["Costo U$/ha de la fila"]
    filas["Mediana del producto"] = g.transform("median")
    filas["Filas del producto"] = g.transform("size")
    filas["Veces la mediana"] = filas["Costo U$/ha de la fila"] / filas["Mediana del producto"]
    caras = filas[
        (filas["Filas del producto"] >= 4)
        & (filas["Veces la mediana"] > UMBRAL_COSTO_FILA)
        & (filas["Costo U$"] >= PISO_COSTO_FILA)
    ]
    add(
        "Fila con costo/ha muy por encima del mismo producto",
        f"El costo por hectárea de esa fila supera {UMBRAL_COSTO_FILA:g}x la mediana de las demás "
        "filas del mismo producto/labor y del mismo tipo (real con real, presupuesto con "
        f"presupuesto; solo productos con 4+ filas y filas de U$ {PISO_COSTO_FILA:g} o más). "
        "Típico de un precio unitario o una cantidad mal cargados.",
        caras[["Gestion", "Bloque_norm", "Cultivo", "RealPresup", "Tipo", "Produc/labor", "Sup", "Costo U$", "Costo U$/ha de la fila", "Mediana del producto", "Veces la mediana"]]
        .rename(columns={"Bloque_norm": "Bloque", "RealPresup": "Real/Presup"})
        .sort_values("Veces la mediana", ascending=False)
        .round(1),
        id_cols=["Gestion", "Bloque", "Cultivo", "Real/Presup", "Tipo", "Produc/labor", "Sup"],
    )

    # 10b. Mismo producto: costo/ha real muy distinto al presupuestado
    med = filas.groupby(["Producto", "Tipo", "RealPresup"])["Costo U$/ha de la fila"].median().unstack("RealPresup")
    cnt = filas.groupby(["Producto", "Tipo", "RealPresup"]).size().unstack("RealPresup")
    if {"realiz", "presup"} <= set(med.columns):
        rp = med[["realiz", "presup"]].join(cnt[["realiz", "presup"]], rsuffix="_n").dropna()
        rp = rp[(rp["realiz_n"] >= 3) & (rp["presup_n"] >= 3) & (rp["realiz"] > 0) & (rp["presup"] > 0)]
        rp["Presup / Real"] = rp["presup"] / rp["realiz"]
        dist = rp[(rp["Presup / Real"] > UMBRAL_REAL_VS_PRESUP) | (rp["Presup / Real"] < 1 / UMBRAL_REAL_VS_PRESUP)]
        tabla_rp = (
            dist.reset_index()
            .rename(columns={"realiz": "Real (U$/ha, mediana)", "presup": "Presup (U$/ha, mediana)", "realiz_n": "Filas real", "presup_n": "Filas presup"})
            .sort_values("Presup / Real", ascending=False)
            .round(2)
        )
    else:
        tabla_rp = pd.DataFrame()
    add(
        "Mismo producto con costo/ha real muy distinto al presupuestado",
        f"La mediana del costo/ha presupuestado es más de {UMBRAL_REAL_VS_PRESUP:g}x (o menos de "
        f"1/{UMBRAL_REAL_VS_PRESUP:g}) la del real, para un mismo producto/labor (3+ filas de cada "
        "una). Puede ser un presupuesto con otra unidad o precio desactualizado.",
        tabla_rp,
        nivel="Info",
        id_cols=["Producto", "Tipo"],
    )

    # 11/12. Cruce con la pestana Comercial
    if comercial is not None and not comercial.empty:
        com = comercial.pivot_table(index=["Gestion", "Cultivo"], columns="Metrica", values="Valor", aggfunc="first")
        cos_calc = superficie_cosechada(df, ["Gestion", "Cultivo"]).set_index(["Gestion", "Cultivo"])
        cruce = cos_calc.join(com, how="outer")
        cruce = cruce.rename(columns={"Superficie cosechada (ha)": "Has calculadas (Actual)", "Has": "Has en Comercial"})
        if "Has en Comercial" in cruce.columns:
            comp = cruce.dropna(subset=["Has calculadas (Actual)", "Has en Comercial"]).copy()
            comp["Dif %"] = (comp["Has en Comercial"] - comp["Has calculadas (Actual)"]) / comp["Has calculadas (Actual)"] * 100
            dif = comp[comp["Dif %"].abs() > UMBRAL_HAS_COMERCIAL * 100].reset_index()
            tabla_has = dif[["Gestion", "Cultivo", "Has calculadas (Actual)", "Has en Comercial", "Dif %"]].round(1)
        else:
            tabla_has = pd.DataFrame()
        add(
            "Hectáreas de Comercial distintas a las de Actual",
            "La fila 'Has' de la pestaña Comercial debería coincidir con las hectáreas de cosecha "
            f"de Actual (tolerancia {UMBRAL_HAS_COMERCIAL:.0%}). Si no, puede haber cultivos "
            "cruzados o desactualizados en esa pestaña. El dashboard no usa esa fila, pero sí el "
            "Rendimiento y el Precio Neto del mismo bloque.",
            tabla_has,
            id_cols=["Gestion", "Cultivo"],
        )

        faltan = cos_calc.join(com, how="left").reset_index()
        for col in ("Rendimiento", "Precio Neto", "Arrendamiento"):
            if col not in faltan.columns:
                faltan[col] = np.nan
        sin_sup = faltan[
            faltan["Rendimiento"].isna() | (faltan["Rendimiento"] == 0)
            | faltan["Precio Neto"].isna() | (faltan["Precio Neto"] == 0)
            | faltan["Arrendamiento"].isna()
        ]
        add(
            "Cultivos con hectáreas pero sin supuestos comerciales completos",
            "Falta Rendimiento, Precio Neto o Arrendamiento en la pestaña Comercial para esa "
            "campaña + cultivo: en Resultado su Ingreso Neto queda en 0 o el gasto incompleto.",
            sin_sup[["Gestion", "Cultivo", "Superficie cosechada (ha)", "Rendimiento", "Precio Neto", "Arrendamiento"]].round(1),
            id_cols=["Gestion", "Cultivo"],
        )

    return items


TAB_INDIRECTOS = "Indirectos"
GID_INDIRECTOS = 1046227083


@st.cache_data(ttl=600, show_spinner="Cargando pestaña Indirectos...")
def load_indirectos_config() -> pd.DataFrame:
    """Pestana "Indirectos": por cada codigo `presup` (arren, sueld, ...) su
    detalle, un factor "Gestion" (proporcion del importe que se imputa a la
    gestion; ej. ganancias = 0) y el % que se asigna a cada area (Maquinaria,
    Gerenciamiento, estructura, ...). Fila 1 = nombres de area desde la
    columna D; fila 2 = encabezados; datos desde la fila 3. Devuelve formato
    largo: una fila por codigo x area."""
    ws = _get_client().open_by_key(SPREADSHEET_ID).worksheet(TAB_INDIRECTOS)
    values = ws.get_values(value_render_option="UNFORMATTED_VALUE")
    cabecera = [str(c).strip() for c in values[0]]
    areas = [(j, c[:1].upper() + c[1:]) for j, c in enumerate(cabecera) if j >= 3 and c and c.lower() != "total"]
    ancho = len(cabecera)
    filas = []
    for r in values[2:]:
        r = list(r) + [""] * (ancho - len(r))
        codigo = str(r[0]).strip().lower()
        if not codigo:
            continue
        factor = _parse_numero_uno(r[2])
        factor = 0.0 if pd.isna(factor) else factor
        for j, area in areas:
            pct = _parse_numero_uno(r[j])
            filas.append(
                {
                    "Codigo": codigo,
                    "Detalle": str(r[1]).strip(),
                    "Factor gestión": factor,
                    "Área": area,
                    "Pct": 0.0 if pd.isna(pct) else pct,
                }
            )
    return pd.DataFrame(filas, columns=["Codigo", "Detalle", "Factor gestión", "Área", "Pct"])


@st.cache_data(ttl=600, show_spinner="Cargando indirectos...")
def load_indirectos_df() -> pd.DataFrame:
    """Filas de "Actual" cuyo Produc/labor es "indirecto" (en cualquier
    Bloque: PRES IND y Ganaderia), con el codigo `presup` normalizado."""
    ws = _get_client().open_by_key(SPREADSHEET_ID).worksheet(TAB_NAME)
    df = pd.DataFrame(ws.get_all_records(value_render_option="UNFORMATTED_VALUE"))
    df.columns = df.columns.str.strip()
    df = df[df["Produc/labor"].astype(str).str.strip().str.lower() == "indirecto"].copy()
    df["Gestion"] = df["Gestion"].astype(str).str.strip()
    df["Codigo"] = df["presup"].astype(str).str.strip().str.lower()
    df["RealPresup"] = df["real/presup"].astype(str).str.strip()
    df["Clase"] = df["Gtos/Ingr/Ret/Inv"].astype(str).str.strip()
    df["Bloque_norm"] = df["Bloque"].astype(str).str.strip().str.upper()
    df["Costo U$"] = _parse_numero_ar(df["U$ tot sin IVA"])
    df["Costo $"] = _parse_numero_ar(df["$ total"])
    return df.reset_index(drop=True)


def asignar_indirectos(
    ind: pd.DataFrame,
    config: pd.DataFrame,
    moneda: str = "U$",
    incluir: tuple[str, ...] = ("realiz", "presup"),
    aplicar_factor: bool = True,
) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Reparte los indirectos entre las areas segun la pestana Indirectos:
    Imputado = Importe del codigo x Factor gestion x % del area.

    Solo se asignan filas de clase Gtos cuyo codigo esta en la pestana y
    tiene algun % de area; el resto (codigos que no estan en la pestana,
    como los sem*, ganaderia, retiros o inversiones, o clases Ing/Ret/Inv)
    queda en `no_asignado` con el motivo. Devuelve (asignado, no_asignado,
    importe total de las filas consideradas)."""
    col = "Costo U$" if moneda == "U$" else "Costo $"
    base = ind[ind["RealPresup"].isin(incluir)]
    cfg_pct = config[config["Pct"] > 0]
    codigos_cfg = set(config["Codigo"])
    codigos_con_pct = set(cfg_pct["Codigo"])
    asignable = base["Codigo"].isin(codigos_con_pct) & (base["Clase"] == "Gtos")

    suma = (
        base[asignable].groupby(["Gestion", "Codigo"], as_index=False)[col].sum().rename(columns={col: "Importe"})
    )
    asig = suma.merge(cfg_pct, on="Codigo")
    asig["Factor"] = asig["Factor gestión"] if aplicar_factor else 1.0
    asig["Imputado"] = asig["Importe"] * asig["Factor"] * asig["Pct"]

    resto = base[~asignable].copy()
    resto["Motivo"] = np.select(
        [~resto["Codigo"].isin(codigos_cfg), ~resto["Codigo"].isin(codigos_con_pct)],
        ["Código no está en la pestaña Indirectos", "Sin % de área en la pestaña Indirectos"],
        default="Clase " + resto["Clase"] + " (solo se asignan Gtos)",
    )
    no_asig = (
        resto.groupby(["Gestion", "Codigo", "Clase", "Motivo"], as_index=False)
        .agg(Filas=(col, "size"), Importe=(col, "sum"))
        .sort_values(["Gestion", "Importe"], ascending=[True, False])
    )
    return asig, no_asig, float(base[col].sum())
