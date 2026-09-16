import streamlit as st


def require_login() -> None:
    if st.session_state.get("autenticado"):
        return

    st.title("La Ranita · Dashboard")
    password = st.text_input("Contraseña", type="password")
    if st.button("Ingresar") or password:
        if password == st.secrets.get("APP_PASSWORD"):
            st.session_state["autenticado"] = True
            st.rerun()
        elif password:
            st.error("Contraseña incorrecta")

    if not st.session_state.get("autenticado"):
        st.stop()
