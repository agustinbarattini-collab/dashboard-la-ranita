import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

from src import auth, data

px.defaults.template = "plotly_white"
px.defaults.color_discrete_sequence = px.colors.qualitative.Set2

FILA_PROMEDIO = "Promedio ponderado"
ALTURA_FILA = 35


def altura_tabla(n_filas: int) -> int:
    """Altura en px para que un st.dataframe/data_editor muestre todas sus
    filas (+ header) sin scroll vertical."""
    return ALTURA_FILA * (n_filas + 1) + 12


def heatmap_costo_ha(pivot_costo: pd.DataFrame, pivot_sup: pd.DataFrame):
    """Arma una tabla estilo heatmap: cada celda es costo/ha con el % que
    representa contra el promedio ponderado (sum costo / sum sup) de su
    columna, coloreada segun el desvio contra ese promedio, con una fila
    final mostrando el promedio de cada columna."""
    pivot_val = pivot_costo / pivot_sup
    promedio_pond = pivot_costo.sum(axis=0, skipna=True) / pivot_sup.sum(axis=0, skipna=True)
    pct = pivot_val.div(promedio_pond, axis=1) * 100
    deviation = pivot_val.sub(promedio_pond, axis=1).div(promedio_pond, axis=1)

    def _fmt(v: float, p: float) -> str:
        if pd.isna(v) or pd.isna(p):
            return ""
        return f"{v:,.0f} ({p:,.0f}%)"

    display = pd.DataFrame(
        {col: [_fmt(pivot_val.loc[i, col], pct.loc[i, col]) for i in pivot_val.index] for col in pivot_val.columns},
        index=pivot_val.index,
    )
    display.loc[FILA_PROMEDIO] = {
        col: ("" if pd.isna(promedio_pond[col]) else f"{promedio_pond[col]:,.0f} (100%)")
        for col in pivot_val.columns
    }
    deviation = deviation.reindex(display.index)

    def _estilo_grilla(_: pd.DataFrame) -> pd.DataFrame:
        # OJO: se usa .at (no .loc) para leer/escribir celda por celda. Con
        # una fila extra de label plano ("Promedio ponderado") mezclada en
        # un index que en el resto son tuplas (Cultivo,Bloque), el index
        # deja de ser un MultiIndex de verdad y ".loc[tupla, col]" pandas lo
        # interpreta mal (AssertionError); ".at" no tiene ese problema.
        css = pd.DataFrame("", index=display.index, columns=display.columns)
        for idx in display.index:
            if idx == FILA_PROMEDIO:
                css.loc[idx] = "font-weight: bold; background-color: #e0e0e0; border-top: 2px solid #999999"
                continue
            for col in display.columns:
                dev = deviation.at[idx, col]
                if pd.isna(dev):
                    continue
                if dev > 0.15:
                    css.at[idx, col] = "background-color: #e57373; color: black"
                elif dev < -0.15:
                    css.at[idx, col] = "background-color: #81c784; color: black"
                else:
                    css.at[idx, col] = "background-color: #fff59d; color: black"
        return css

    return display.style.apply(_estilo_grilla, axis=None)

st.set_page_config(page_title="La Ranita · Dashboard", layout="wide", page_icon="🐸")
auth.require_login()

st.sidebar.title("🐸 La Ranita")
seccion = st.sidebar.radio("Sección", ["1. Costos", "2. Resultado", "3. Indirectos", "4. Revisión de datos"])

if st.sidebar.button("↻ Actualizar datos"):
    st.cache_data.clear()
    st.rerun()

df = data.load_actual_df()

no_mapeados = data.valores_cultivo_no_mapeados(df)
if no_mapeados:
    st.sidebar.warning(
        f"Cultivo sin mapear en el diccionario: {', '.join(no_mapeados)}. "
        "Se muestran tal cual aparecen en la planilla."
    )

st.sidebar.divider()
st.sidebar.markdown("#### 🔎 Filtros")

gestiones_disponibles = sorted(df["Gestion"].dropna().unique())
gestiones_sel = st.sidebar.multiselect("📅 Campaña (Gestión)", gestiones_disponibles, default=gestiones_disponibles)

cultivos_disponibles = sorted(df["Cultivo"].dropna().unique())
cultivos_sel = st.sidebar.multiselect("🌱 Cultivo", cultivos_disponibles, default=cultivos_disponibles)

moneda = st.sidebar.radio("Moneda", ["U$", "$"], horizontal=True)

df_f = df[df["Gestion"].isin(gestiones_sel) & df["Cultivo"].isin(cultivos_sel)]
# Costo y superficie cosechada usan real+presupuestada, a pedido.
df_costo = df_f

if seccion == "1. Costos":
    st.title("Costos")

    col_costo = "Costo U$" if moneda == "U$" else "Costo $"
    total = df_costo[col_costo].sum()
    c1, c2, c3 = st.columns(3)
    c1.metric(f"Costo total ({moneda})", f"{total:,.0f}")
    c2.metric("Lotes en el filtro", df_costo["Bloque_norm"].nunique())
    c3.metric("Registros", len(df_costo))

    st.subheader(f"Costo total por lote y cultivo ({moneda})")
    ct = data.costo_total(df_costo, moneda).sort_values(f"Costo total ({moneda})", ascending=False)
    fig1 = px.bar(
        ct.head(30),
        x="Bloque",
        y=f"Costo total ({moneda})",
        color="Cultivo",
        barmode="stack",
        facet_col="Gestion" if len(gestiones_sel) > 1 else None,
    )
    st.plotly_chart(fig1, use_container_width=True)

    st.subheader(f"Costo/ha cosechada por cultivo ({moneda}/ha) + superficie cosechada")
    st.caption("Costo y superficie cosechada suman filas reales + presupuestadas.")
    cph_cosechada = data.costo_por_ha_cosechada(
        df_costo, df_f, cost_by=["Gestion", "Cultivo"], sup_by=["Gestion", "Cultivo"], moneda=moneda
    )
    if cph_cosechada.empty:
        st.info("No hay hectáreas cosechadas (reales ni presupuestadas) todavía para el filtro actual.")
    else:
        col_costo_ha = f"Costo/ha cosechada ({moneda})"
        fig_combo = make_subplots(specs=[[{"secondary_y": True}]])
        colores = px.colors.qualitative.Set2
        gestiones_orden = sorted(cph_cosechada["Gestion"].unique())

        # Las barras se agregan primero y las lineas de superficie despues,
        # para que las lineas queden dibujadas por encima de las barras.
        for i, gestion in enumerate(gestiones_orden):
            sub = cph_cosechada[cph_cosechada["Gestion"] == gestion]
            color = colores[i % len(colores)]
            fig_combo.add_trace(
                go.Bar(
                    x=sub["Cultivo"],
                    y=sub[col_costo_ha].round(0),
                    name=f"Costo/ha ({gestion})",
                    marker_color=color,
                    text=sub[col_costo_ha].round(0),
                    texttemplate="%{text:,.0f}",
                    textposition="outside",
                ),
                secondary_y=False,
            )
        simbolos = ["circle", "diamond", "square", "triangle-up"]
        for i, gestion in enumerate(gestiones_orden):
            sub = cph_cosechada[cph_cosechada["Gestion"] == gestion]
            fig_combo.add_trace(
                go.Scatter(
                    x=sub["Cultivo"],
                    y=sub["Superficie cosechada (ha)"],
                    name=f"Ha cosechadas ({gestion})",
                    mode="lines+markers+text",
                    line=dict(color="#1a1a1a", width=3, dash="dot"),
                    marker=dict(size=13, symbol=simbolos[i % len(simbolos)], color="#1a1a1a", line=dict(width=2, color="white")),
                    text=sub["Superficie cosechada (ha)"].round(0),
                    texttemplate="%{text:,.0f} ha",
                    textposition="top center",
                    textfont=dict(color="#1a1a1a", size=11),
                ),
                secondary_y=True,
            )
        fig_combo.update_yaxes(title_text=col_costo_ha, secondary_y=False)
        fig_combo.update_yaxes(title_text="Superficie cosechada (ha)", secondary_y=True)
        fig_combo.update_layout(barmode="group")
        st.plotly_chart(fig_combo, use_container_width=True)

    st.subheader(f"Costo/ha cosechada desglosado por Tipo ({moneda}/ha)")
    cultivos_con_cosecha = sorted(data.superficie_cosechada(df_f, ["Cultivo"])["Cultivo"].unique())
    if not cultivos_con_cosecha:
        st.info("No hay hectáreas cosechadas (reales ni presupuestadas) todavía para el filtro actual.")
    else:
        cultivo_foco = st.selectbox("Cultivo", cultivos_con_cosecha)
        df_cultivo_costo = df_costo[df_costo["Cultivo"] == cultivo_foco]
        df_cultivo_sup = df_f[df_f["Cultivo"] == cultivo_foco]
        cpt_cosechada = data.costo_por_ha_cosechada(
            df_cultivo_costo, df_cultivo_sup, cost_by=["Gestion", "Tipo"], sup_by=["Gestion"], moneda=moneda
        )
        fig_tipo = px.bar(
            cpt_cosechada.sort_values(f"Costo/ha cosechada ({moneda})", ascending=False),
            x="Tipo",
            y=f"Costo/ha cosechada ({moneda})",
            color="Gestion",
            barmode="group",
        )
        st.plotly_chart(fig_tipo, use_container_width=True)

    st.subheader(f"Costo/ha por lote, desglosado por Tipo ({moneda}/ha)")
    st.caption(
        "Un heatmap por cultivo (hacé clic para abrirlo): filas = lote, columnas = Tipo de "
        "gasto + una columna final \"Total\" (todos los Tipo sumados, el costo/ha completo "
        "de ese lote). Si un lote tiene 2 cultivos en la misma campaña, el gasto de cada "
        "cultivo se divide por la superficie de ESE cultivo en el lote (hectáreas "
        "cosechadas; si todavía no hay cosecha cargada, las sembradas). El promedio "
        "ponderado y el color comparan cada lote contra el promedio de ESE cultivo "
        "únicamente (no contra los demás cultivos) — para ver si el desvío viene de un tipo "
        "de gasto puntual en un lote."
    )
    crudo_tipo = data.costo_por_tipo_y_lote(df_costo, moneda)
    if crudo_tipo.empty:
        st.info("No hay datos para el filtro actual.")
    else:
        for cultivo in sorted(crudo_tipo["Cultivo"].dropna().unique()):
            sub = crudo_tipo[crudo_tipo["Cultivo"] == cultivo]
            pivot_costo_c = sub.pivot_table(index="Bloque", columns="Tipo", values="Costo", aggfunc="sum")
            pivot_sup_c = sub.pivot_table(index="Bloque", columns="Tipo", values="Sup", aggfunc="max")
            pivot_costo_c["Total"] = pivot_costo_c.sum(axis=1, skipna=True)
            pivot_sup_c["Total"] = pivot_sup_c.max(axis=1, skipna=True)
            with st.expander(f"🌱 {cultivo} ({pivot_costo_c.shape[0]} lotes)"):
                st.dataframe(
                    heatmap_costo_ha(pivot_costo_c, pivot_sup_c),
                    use_container_width=True,
                    height=altura_tabla(pivot_costo_c.shape[0] + 1),
                )

elif seccion == "2. Resultado":
    st.title("Resultado")

    comercial = data.load_comercial_df()
    lookup = data.comercial_lookup(comercial)
    gestiones_comercial = sorted(comercial["Gestion"].unique()) if not comercial.empty else []

    st.caption(
        "Rendimiento, Precio Neto, Flete y Arrendamiento se precargan desde la pestaña "
        f"'Comercial' de la planilla para las campañas que estén cargadas ahí "
        f"({', '.join(gestiones_comercial) if gestiones_comercial else 'ninguna todavía'}). "
        "Donde no haya dato en Comercial, completalo a mano. El resto (costo, superficie) "
        "sale de los datos reales/presupuestados como en Costos. Los valores que edites acá "
        "son solo de esta sesión, no se guardan en la planilla."
    )

    base = data.costo_por_ha_cosechada(
        df_costo, df_f, cost_by=["Gestion", "Cultivo"], sup_by=["Gestion", "Cultivo"], moneda="U$"
    )
    if base.empty:
        st.info("No hay hectáreas cosechadas (reales ni presupuestadas) todavía para el filtro actual.")
    else:
        base = base.sort_values(["Gestion", "Cultivo"]).reset_index(drop=True)
        col_costo_ha = "Costo/ha cosechada (U$)"

        if "supuestos_resultado" not in st.session_state:
            st.session_state["supuestos_resultado"] = {}
        supuestos = st.session_state["supuestos_resultado"]

        def _valor_default(gestion: str, cultivo: str, campo: str, metrica_comercial: str) -> float:
            if (gestion, cultivo) in supuestos:
                return supuestos[(gestion, cultivo)].get(campo, 0.0)
            return lookup.get((gestion, cultivo, metrica_comercial), 0.0)

        editable = base[["Gestion", "Cultivo", "Superficie cosechada (ha)", col_costo_ha]].copy()
        editable["Rendimiento (t/ha)"] = [
            _valor_default(g, c, "rend", "Rendimiento") for g, c in zip(editable["Gestion"], editable["Cultivo"])
        ]
        editable["Precio Neto (U$/t)"] = [
            _valor_default(g, c, "precio_neto", "Precio Neto") for g, c in zip(editable["Gestion"], editable["Cultivo"])
        ]
        editable["Flete (U$/t)"] = [
            _valor_default(g, c, "flete", "Flete") for g, c in zip(editable["Gestion"], editable["Cultivo"])
        ]
        editable["Arrendamiento (U$/ha)"] = [
            _valor_default(g, c, "arrendamiento", "Arrendamiento")
            for g, c in zip(editable["Gestion"], editable["Cultivo"])
        ]

        st.markdown("##### Supuestos por cultivo + campaña")
        edited = st.data_editor(
            editable,
            use_container_width=True,
            hide_index=True,
            height=altura_tabla(len(editable)),
            disabled=["Gestion", "Cultivo", "Superficie cosechada (ha)", col_costo_ha],
            column_config={
                "Superficie cosechada (ha)": st.column_config.NumberColumn(format="%.0f"),
                col_costo_ha: st.column_config.NumberColumn(format="%.0f"),
                "Rendimiento (t/ha)": st.column_config.NumberColumn(format="%.2f"),
                "Precio Neto (U$/t)": st.column_config.NumberColumn(format="%.0f"),
                "Flete (U$/t)": st.column_config.NumberColumn(format="%.0f"),
                "Arrendamiento (U$/ha)": st.column_config.NumberColumn(format="%.0f"),
            },
            key="editor_supuestos_resultado",
        )

        for _, fila in edited.iterrows():
            supuestos[(fila["Gestion"], fila["Cultivo"])] = {
                "rend": fila["Rendimiento (t/ha)"],
                "precio_neto": fila["Precio Neto (U$/t)"],
                "flete": fila["Flete (U$/t)"],
                "arrendamiento": fila["Arrendamiento (U$/ha)"],
            }

        resultado = edited.copy()
        resultado["Ingreso Neto (U$/ha)"] = resultado["Rendimiento (t/ha)"] * resultado["Precio Neto (U$/t)"]
        resultado["Gastos totales (U$/ha)"] = resultado[col_costo_ha] + resultado["Arrendamiento (U$/ha)"]
        resultado["Margen Neto (U$/ha)"] = resultado["Ingreso Neto (U$/ha)"] - resultado["Gastos totales (U$/ha)"]
        con_ingreso = resultado["Ingreso Neto (U$/ha)"] != 0
        resultado.loc[con_ingreso, "Rentabilidad (%)"] = (
            resultado.loc[con_ingreso, "Margen Neto (U$/ha)"] / resultado.loc[con_ingreso, "Ingreso Neto (U$/ha)"] * 100
        )

        con_precio_neto = resultado["Precio Neto (U$/t)"] > 0
        resultado.loc[con_precio_neto, "Rinde de indiferencia (t/ha)"] = (
            resultado.loc[con_precio_neto, "Gastos totales (U$/ha)"] / resultado.loc[con_precio_neto, "Precio Neto (U$/t)"]
        )
        con_rendimiento = resultado["Rendimiento (t/ha)"] > 0
        resultado.loc[con_rendimiento, "Precio Neto Indiferencia (U$/t)"] = (
            resultado.loc[con_rendimiento, "Gastos totales (U$/ha)"] / resultado.loc[con_rendimiento, "Rendimiento (t/ha)"]
        )
        resultado["Precio Bruto Indiferencia (U$/t)"] = (
            resultado["Precio Neto Indiferencia (U$/t)"] + resultado["Flete (U$/t)"]
        )

        st.markdown("##### Resultado por cultivo")
        if len(gestiones_sel) != 1:
            st.info(
                "Elegí una sola Campaña en el filtro de la izquierda para un análisis por "
                "cultivo más claro. Con varias campañas seleccionadas, los valores se suman "
                "entre campañas."
            )

        # Desglose de gastos por Tipo, u$/ha cosechada, por Gestion+Cultivo+Tipo
        # (mismo criterio que la seccion Costos: solo filas reales para el
        # costo, superficie real+presupuestada).
        gastos_tipo = data.costo_por_ha_cosechada(
            df_costo, df_f, cost_by=["Gestion", "Cultivo", "Tipo"], sup_by=["Gestion", "Cultivo"], moneda="U$"
        )
        gastos_tipo["_total"] = gastos_tipo["Costo/ha cosechada (U$)"] * gastos_tipo["Superficie cosechada (ha)"]
        gasto_tipo_cultivo = gastos_tipo.groupby(["Cultivo", "Tipo"], as_index=False)["_total"].sum()

        # Colapsar Gestion -> Cultivo sumando TOTALES (no promediando tasas):
        # si hay mas de una campana seleccionada, esto es lo que hace que
        # "se sumen entre campanas" tenga sentido dimensional.
        base_cultivo = resultado.copy()
        for col, total_col in [
            ("Ingreso Neto (U$/ha)", "_ingreso_tot"),
            ("Arrendamiento (U$/ha)", "_arrendamiento_tot"),
            ("Rendimiento (t/ha)", "_rend_tot"),
            ("Precio Neto (U$/t)", "_precio_tot"),
        ]:
            base_cultivo[total_col] = base_cultivo[col] * base_cultivo["Superficie cosechada (ha)"]
        por_cultivo = base_cultivo.groupby("Cultivo", as_index=False).agg(
            Has=("Superficie cosechada (ha)", "sum"),
            _ingreso_tot=("_ingreso_tot", "sum"),
            _arrendamiento_tot=("_arrendamiento_tot", "sum"),
            _rend_tot=("_rend_tot", "sum"),
            _precio_tot=("_precio_tot", "sum"),
        )

        cultivos_presentes = sorted(por_cultivo["Cultivo"].unique())
        tipos_presentes = sorted(t for t in gasto_tipo_cultivo["Tipo"].unique() if t)
        has_map = dict(zip(por_cultivo["Cultivo"], por_cultivo["Has"]))

        def _tasa(total_map: dict, cultivo: str) -> float | None:
            has = has_map.get(cultivo, 0.0)
            if not has:
                return None
            return total_map.get(cultivo, 0.0) / has

        filas: dict[str, dict[str, float | None]] = {"Has": has_map}
        gasto_totales_tot = {c: 0.0 for c in cultivos_presentes}
        for tipo in tipos_presentes:
            tot_map = dict(
                zip(
                    gasto_tipo_cultivo.loc[gasto_tipo_cultivo["Tipo"] == tipo, "Cultivo"],
                    gasto_tipo_cultivo.loc[gasto_tipo_cultivo["Tipo"] == tipo, "_total"],
                )
            )
            filas[tipo] = {c: _tasa(tot_map, c) for c in cultivos_presentes}
            for c in cultivos_presentes:
                gasto_totales_tot[c] += tot_map.get(c, 0.0)

        arrend_tot_map = dict(zip(por_cultivo["Cultivo"], por_cultivo["_arrendamiento_tot"]))
        filas["Arrendamiento"] = {c: _tasa(arrend_tot_map, c) for c in cultivos_presentes}
        for c in cultivos_presentes:
            gasto_totales_tot[c] += arrend_tot_map.get(c, 0.0)

        ingreso_tot_map = dict(zip(por_cultivo["Cultivo"], por_cultivo["_ingreso_tot"]))
        filas["Ingreso Neto"] = {c: _tasa(ingreso_tot_map, c) for c in cultivos_presentes}
        filas["Gastos totales"] = {c: _tasa(gasto_totales_tot, c) for c in cultivos_presentes}
        filas["Margen Neto"] = {
            c: (filas["Ingreso Neto"][c] - filas["Gastos totales"][c])
            if filas["Ingreso Neto"][c] is not None and filas["Gastos totales"][c] is not None
            else None
            for c in cultivos_presentes
        }
        filas["Rentabilidad (%)"] = {
            c: (filas["Margen Neto"][c] / filas["Ingreso Neto"][c] * 100) if filas["Ingreso Neto"].get(c) else None
            for c in cultivos_presentes
        }

        rend_tot_map = dict(zip(por_cultivo["Cultivo"], por_cultivo["_rend_tot"]))
        precio_tot_map = dict(zip(por_cultivo["Cultivo"], por_cultivo["_precio_tot"]))
        filas["Rendimiento (t/ha)"] = {c: _tasa(rend_tot_map, c) for c in cultivos_presentes}
        filas["Precio Neto (U$/t)"] = {c: _tasa(precio_tot_map, c) for c in cultivos_presentes}
        filas["Rinde de indiferencia (t/ha)"] = {
            c: (filas["Gastos totales"][c] / filas["Precio Neto (U$/t)"][c])
            if filas["Precio Neto (U$/t)"].get(c)
            else None
            for c in cultivos_presentes
        }
        filas["Precio Neto Indiferencia (U$/t)"] = {
            c: (filas["Gastos totales"][c] / filas["Rendimiento (t/ha)"][c])
            if filas["Rendimiento (t/ha)"].get(c)
            else None
            for c in cultivos_presentes
        }

        # Columna "General": mismo criterio, ponderando por Has entre TODOS
        # los cultivos seleccionados (no un promedio simple entre cultivos).
        has_general = sum(has_map.values())

        def _general(total_map: dict) -> float | None:
            if not has_general:
                return None
            return sum(total_map.get(c, 0.0) for c in cultivos_presentes) / has_general

        general: dict[str, float | None] = {"Has": has_general}
        for tipo in tipos_presentes:
            tot_map = dict(
                zip(
                    gasto_tipo_cultivo.loc[gasto_tipo_cultivo["Tipo"] == tipo, "Cultivo"],
                    gasto_tipo_cultivo.loc[gasto_tipo_cultivo["Tipo"] == tipo, "_total"],
                )
            )
            general[tipo] = _general(tot_map)
        general["Arrendamiento"] = _general(arrend_tot_map)
        general["Ingreso Neto"] = _general(ingreso_tot_map)
        general["Gastos totales"] = _general(gasto_totales_tot)
        general["Margen Neto"] = (
            (general["Ingreso Neto"] - general["Gastos totales"])
            if general["Ingreso Neto"] is not None and general["Gastos totales"] is not None
            else None
        )
        general["Rentabilidad (%)"] = (
            (general["Margen Neto"] / general["Ingreso Neto"] * 100) if general.get("Ingreso Neto") else None
        )
        general["Rendimiento (t/ha)"] = _general(rend_tot_map)
        general["Precio Neto (U$/t)"] = _general(precio_tot_map)
        general["Rinde de indiferencia (t/ha)"] = (
            (general["Gastos totales"] / general["Precio Neto (U$/t)"]) if general.get("Precio Neto (U$/t)") else None
        )
        general["Precio Neto Indiferencia (U$/t)"] = (
            (general["Gastos totales"] / general["Rendimiento (t/ha)"]) if general.get("Rendimiento (t/ha)") else None
        )

        orden_filas = (
            ["Has"]
            + tipos_presentes
            + [
                "Arrendamiento",
                "Ingreso Neto",
                "Gastos totales",
                "Margen Neto",
                "Rentabilidad (%)",
                "Rendimiento (t/ha)",
                "Rinde de indiferencia (t/ha)",
                "Precio Neto Indiferencia (U$/t)",
            ]
        )
        filas_resaltadas = {"Has", "Ingreso Neto", "Gastos totales", "Margen Neto", "Rentabilidad (%)"}

        def _fmt_cultivo(etiqueta: str, val) -> str:
            if val is None or (isinstance(val, float) and pd.isna(val)):
                return "—"
            if etiqueta == "Rentabilidad (%)":
                return f"{val:.1f}%"
            if etiqueta in ("Rendimiento (t/ha)", "Rinde de indiferencia (t/ha)"):
                return f"{val:,.2f}"
            return f"{val:,.0f}"

        html = ["<div style='overflow-x:auto'>"]
        html.append(
            "<style>"
            ".resultado-cultivo-tabla{border-collapse:collapse;font-size:0.85rem;white-space:nowrap;}"
            ".resultado-cultivo-tabla th,.resultado-cultivo-tabla td{border:1px solid #d0d7de;padding:4px 8px;text-align:right;}"
            ".resultado-cultivo-tabla th{background:#eef3ee;text-align:center;}"
            ".resultado-cultivo-tabla td:first-child,.resultado-cultivo-tabla th:first-child{text-align:left;position:sticky;left:0;background:#fff;}"
            ".resultado-cultivo-tabla th:first-child{background:#eef3ee;}"
            ".resultado-cultivo-tabla tr.resaltada td,.resultado-cultivo-tabla tr.resaltada td:first-child{background:#d9ead3;font-weight:600;}"
            ".resultado-cultivo-tabla td.general-cell,.resultado-cultivo-tabla th.general-cell{font-weight:700;background:#cfe2cf;}"
            ".resultado-cultivo-tabla tr.resaltada td.general-cell{background:#b6d7b6;}"
            "</style>"
        )
        html.append("<table class='resultado-cultivo-tabla'>")
        html.append("<tr><th>Métrica</th>" + "".join(f"<th>{c}</th>" for c in cultivos_presentes) + "<th class='general-cell'>General</th></tr>")
        for etiqueta in orden_filas:
            row_class = "resaltada" if etiqueta in filas_resaltadas else ""
            html.append(f"<tr class='{row_class}'><td>{etiqueta}</td>")
            for c in cultivos_presentes:
                html.append(f"<td>{_fmt_cultivo(etiqueta, filas[etiqueta].get(c))}</td>")
            html.append(f"<td class='general-cell'>{_fmt_cultivo(etiqueta, general.get(etiqueta))}</td>")
            html.append("</tr>")
        html.append("</table></div>")
        st.markdown("".join(html), unsafe_allow_html=True)

        st.caption(
            "Todos los valores (salvo Has, Rentabilidad, Rendimiento y Rinde/Precio de "
            "indiferencia) están en U\\$/ha cosechada. Gastos totales = suma de los Tipo + "
            "Arrendamiento. Margen Neto = Ingreso Neto − Gastos totales. Rentabilidad = "
            "Margen Neto / Ingreso Neto. \"General\" pondera todos los cultivos por hectárea "
            "cosechada (no es un promedio simple entre cultivos)."
        )

elif seccion == "3. Indirectos":
    st.title("Indirectos")
    st.caption(
        "Filas de la base con Produc/labor = \"indirecto\" (bloques PRES IND y Ganadería), "
        "asignadas a cada área según la pestaña \"Indirectos\" de la planilla: cada código "
        "(columna `presup`: arren, sueld, combu, ...) tiene un factor \"Gestion\" y un % por "
        "área. Importe imputado = importe del código × factor Gestión × % del área. Usa el "
        "filtro de Campaña y la Moneda de la izquierda."
    )

    config_ind = data.load_indirectos_config()
    ind = data.load_indirectos_df()
    areas = list(dict.fromkeys(config_ind["Área"]))

    st.link_button(
        "✏️ Editar la pestaña Indirectos en la planilla",
        f"https://docs.google.com/spreadsheets/d/{data.SPREADSHEET_ID}/edit#gid={data.GID_INDIRECTOS}",
    )
    with st.expander("Ver la tabla de asignación que se está usando"):
        ancho = (
            config_ind.pivot_table(index=["Codigo", "Detalle", "Factor gestión"], columns="Área", values="Pct", aggfunc="sum")
            .reindex(columns=areas)
            .reset_index()
            .rename(columns={"Codigo": "Código"})
        )
        pct_vacio = lambda v: "" if pd.isna(v) or v == 0 else f"{v:.0%}"
        st.dataframe(
            ancho.style.format({c: pct_vacio for c in ["Factor gestión"] + areas}),
            use_container_width=True,
            hide_index=True,
            height=altura_tabla(min(len(ancho), 15)),
        )
        st.caption(
            "Se lee en vivo de la pestaña Indirectos: si cambiás un % o un factor en la planilla, "
            "apretá \"↻ Actualizar datos\" (a la izquierda) y se recalcula; si no, se refresca "
            "sola cada 10 minutos."
        )

    col_a, col_b = st.columns([2, 1])
    modo_rp = col_a.radio(
        "Importes a considerar",
        ["Real + presupuestado", "Solo real", "Solo presupuestado"],
        horizontal=True,
        help="En 26/27 el real cubre los meses ya ejecutados y el presupuesto los que faltan.",
    )
    aplicar_factor = col_b.toggle(
        "Aplicar factor \"Gestion\"",
        value=True,
        help="Multiplica cada código por su factor de la pestaña Indirectos (ej. ganancias = 0, "
        "gastos bancarios = 0,9). Apagalo para ver los importes sin ese ajuste.",
    )
    incluir = {
        "Real + presupuestado": ("realiz", "presup"),
        "Solo real": ("realiz",),
        "Solo presupuestado": ("presup",),
    }[modo_rp]

    ind_f = ind[ind["Gestion"].isin(gestiones_sel)]
    asig, no_asig, total_filas = data.asignar_indirectos(ind_f, config_ind, moneda, incluir, aplicar_factor)

    campanas = [g for g in gestiones_sel if g in set(ind_f["Gestion"])]
    imputado = asig["Imputado"].sum()
    sin_asignar = no_asig["Importe"].sum()
    excluido_factor = total_filas - imputado - sin_asignar
    if abs(excluido_factor) < 0.5:
        excluido_factor = 0.0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"Total filas indirecto ({moneda})", f"{total_filas:,.0f}")
    c2.metric("Imputado a áreas", f"{imputado:,.0f}")
    c3.metric("Excluido por factor Gestión", f"{excluido_factor:,.0f}")
    c4.metric("Sin asignar", f"{sin_asignar:,.0f}")
    st.caption("Total filas = imputado + excluido por factor + sin asignar.")

    if asig.empty:
        st.info("No hay indirectos asignables para el filtro actual.")
    else:
        def _formato_importes(tabla: pd.DataFrame, cols: list[str]):
            return tabla.style.format({c: "{:,.0f}" for c in cols}, na_rep="—")

        st.subheader(f"Resumen por área y campaña ({moneda})")
        resumen = (
            asig.pivot_table(index="Área", columns="Gestion", values="Imputado", aggfunc="sum")
            .reindex(index=areas, columns=campanas)
            .fillna(0.0)
        )
        if len(campanas) > 1:
            resumen["Total"] = resumen.sum(axis=1)
        resumen.loc["TOTAL"] = resumen.sum(axis=0)
        resumen = resumen.reset_index()

        def _resaltar_total(row: pd.Series) -> list[str]:
            return ["font-weight: bold; background-color: #e0e0e0"] * len(row) if row["Área"] == "TOTAL" else [""] * len(row)

        st.dataframe(
            _formato_importes(resumen, [c for c in resumen.columns if c != "Área"]).apply(_resaltar_total, axis=1),
            use_container_width=True,
            hide_index=True,
            height=altura_tabla(len(resumen)),
        )

        st.subheader(f"Detalle por área ({moneda})")
        st.caption(
            "Hacé clic en un área para ver qué códigos la componen. \"% al área\" es el porcentaje "
            "de cada código que va a esa área según la pestaña Indirectos; \"Factor\" es el factor "
            "Gestión del código."
        )
        for area in areas:
            sub = asig[asig["Área"] == area]
            total_area = sub["Imputado"].sum()
            with st.expander(f"{area} — {total_area:,.0f}"):
                if sub.empty:
                    st.write("Ningún código tiene % asignado a esta área.")
                    continue
                det = sub.pivot_table(
                    index=["Detalle", "Codigo", "Pct", "Factor"], columns="Gestion", values="Imputado", aggfunc="sum"
                ).reindex(columns=campanas).fillna(0.0)
                if len(campanas) > 1:
                    det["Total"] = det.sum(axis=1)
                det = det.reset_index().rename(columns={"Pct": "% al área", "Codigo": "Código"})
                det = det.sort_values(det.columns[-1], key=lambda s: s.abs(), ascending=False)
                importes = [c for c in det.columns if c not in ("Detalle", "Código", "% al área", "Factor")]
                st.dataframe(
                    det.style.format({**{c: "{:,.0f}" for c in importes}, "% al área": "{:.0%}", "Factor": "{:.2f}"}, na_rep="—"),
                    use_container_width=True,
                    hide_index=True,
                    height=altura_tabla(min(len(det), 15)),
                )

    st.subheader(f"Sin asignar ({moneda})")
    st.caption(
        "Filas \"indirecto\" que no se pudieron repartir: el código no está en la pestaña "
        "Indirectos (ej. los sem*, ganadería, retiros, inversiones) o la fila no es de clase Gtos "
        "(Ing/Ret/Inv). Para asignarlas, agregá el código con sus % en la pestaña Indirectos."
    )
    if no_asig.empty:
        st.write("Todas las filas indirecto quedaron asignadas.")
    else:
        st.dataframe(
            no_asig.rename(columns={"Codigo": "Código", "Gestion": "Campaña"}).style.format({"Importe": "{:,.0f}"}),
            use_container_width=True,
            hide_index=True,
            height=altura_tabla(min(len(no_asig), 12)),
        )

elif seccion == "4. Revisión de datos":
    st.title("Revisión de datos")
    st.caption(
        "Chequeos automáticos sobre las pestañas Actual y Comercial de la planilla, para "
        "detectar posibles errores de carga. Se recalculan con cada actualización de datos y "
        "no usan los filtros de la izquierda. ⚠️ = probable error a revisar, ℹ️ = algo que el "
        "dashboard ya interpreta pero conviene conocer, ✅ = sin pendientes. Tildá los puntos "
        "que ya revisaste y están bien y apretá \"Guardar tildes\": no vuelven a aparecer."
    )

    items = data.revisiones(df, data.load_comercial_df())
    ok_sesion: set[str] = st.session_state.setdefault("revisiones_ok_sesion", set())
    ok = set(data.load_revisiones_ok()) | ok_sesion
    st.session_state.setdefault("rev_version", 0)

    if st.session_state.get("rev_aviso_permiso"):
        email_sa = st.secrets["gcp_service_account"].get("client_email", "la cuenta de servicio")
        st.warning(
            "Los tildes de esta sesión **no se están guardando en la planilla**: la cuenta de "
            f"servicio ({email_sa}) solo tiene permiso de lectura y al recargar la página se "
            "pierden. Para que queden guardados (pestaña nueva \"Revisiones OK\"), compartí la "
            "planilla con ese mail como **Editor**."
        )

    mostrar_revisados = st.toggle("Mostrar también los ya revisados", value=False)

    for it in items:
        it["pendiente"] = [c not in ok for c in it["claves"]]
    n_revisar = sum(1 for it in items if it["nivel"] == "Revisar" and any(it["pendiente"]))
    n_info = sum(1 for it in items if it["nivel"] == "Info" and any(it["pendiente"]))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("⚠️ Para revisar", n_revisar)
    c2.metric("ℹ️ Informativos", n_info)
    c3.metric("✅ Sin pendientes", sum(1 for it in items if not any(it["pendiente"])))
    c4.metric("☑️ Puntos marcados", sum(1 for it in items for p in it["pendiente"] if not p))

    def _orden_revision(par: tuple[int, dict]) -> int:
        it = par[1]
        if not any(it["pendiente"]):
            return 2
        return 0 if it["nivel"] == "Revisar" else 1

    def _guardar_tildes(nuevos: list[tuple[str, str]], quitar: list[str]) -> None:
        try:
            if nuevos:
                data.marcar_revisiones(nuevos)
            if quitar:
                data.desmarcar_revisiones(quitar)
        except data.SinPermisoEscritura:
            ok_sesion.update(k for k, _ in nuevos)
            ok_sesion.difference_update(quitar)
            st.session_state["rev_aviso_permiso"] = True
        st.session_state["rev_version"] += 1
        st.rerun()

    for i, it in sorted(enumerate(items), key=_orden_revision):
        n_total = len(it["claves"])
        n_pend = sum(it["pendiente"])
        n_rev = n_total - n_pend
        icono = "✅" if n_pend == 0 else ("⚠️" if it["nivel"] == "Revisar" else "ℹ️")
        etiqueta = f"{icono} {it['titulo']} ({n_pend})" + (f" · {n_rev} revisados" if n_rev else "")
        with st.expander(etiqueta):
            st.caption(it["detalle"].replace("$", "\\$"))
            if n_total == 0:
                st.write("Sin hallazgos.")
                continue
            if n_pend == 0 and not mostrar_revisados:
                st.write("Todo revisado. Activá \"Mostrar también los ya revisados\" para verlos o desmarcarlos.")
                continue

            vista = it["tabla"].copy()
            vista.insert(0, "Revisado", [not p for p in it["pendiente"]])
            claves_vista = list(it["claves"])
            if not mostrar_revisados:
                pend = np.array(it["pendiente"])
                vista = vista[pend].reset_index(drop=True)
                claves_vista = [k for k, p in zip(claves_vista, pend) if p]

            with st.form(f"form_rev_{i}_{st.session_state['rev_version']}"):
                editado = st.data_editor(
                    vista,
                    use_container_width=True,
                    hide_index=True,
                    height=altura_tabla(min(len(vista), 12)),
                    disabled=[c for c in vista.columns if c != "Revisado"],
                    column_config={
                        "Revisado": st.column_config.CheckboxColumn(
                            "✔ Revisado", help="Tildá los puntos que ya revisaste y están bien: no te los vuelvo a mostrar."
                        )
                    },
                    key=f"rev_{i}_{int(mostrar_revisados)}_{st.session_state['rev_version']}",
                )
                guardar = st.form_submit_button("Guardar tildes")
            if guardar:
                antes = list(vista["Revisado"])
                despues = list(editado["Revisado"])
                nuevos = [(k, it["titulo"]) for k, a, d in zip(claves_vista, antes, despues) if d and not a]
                quitar = [k for k, a, d in zip(claves_vista, antes, despues) if a and not d]
                if nuevos or quitar:
                    _guardar_tildes(nuevos, quitar)
