import streamlit as st
import pandas as pd
import requests

# 1. Configuración del Tablero
st.set_page_config(page_title="Simulador C-Level Suitestensa", layout="wide")

st.title("📊 Tablero de Simulación Comercial: Renovaciones 2027")

# 2. Función para obtener datos dinámicos desde el backend
@st.cache_data(ttl=600)  # Cachea los datos por 10 minutos
def obtener_datos_nodos():
    try:
        # Se asume que el servidor corre en el puerto 8001
        response = requests.get("http://localhost:8001/api/v1/nodos-hospitalarios")
        if response.status_code == 200:
            return pd.DataFrame(response.json())
        else:
            st.error("No se pudieron cargar los datos de los hospitales.")
            return pd.DataFrame()
    except Exception as e:
        st.error(f"Error de conexión: {e}")
        return pd.DataFrame()

# 3. Panel Lateral
st.sidebar.header("Palancas de Costo y Margen")
margen_target = st.sidebar.slider("Margen Bruto Objetivo (%)", 10, 60, 30) / 100
costo_hh = st.sidebar.slider("Costo Hora L1 (USD)", 20.0, 60.0, 38.71)
costo_viatico = st.sidebar.slider("Costo Viático x Visita (USD)", 100.0, 400.0, 217.94)

# 4. Motor de Cálculo
df = obtener_datos_nodos()

if not df.empty:
    horas_mes = 4.0
    visitas_mes = 0.05
    costo_local_mensual_nodo = (horas_mes * costo_hh) + (visitas_mes * costo_viatico)

    df["OPEX Local (USD)"] = df["Meses a Cubrir"] * costo_local_mensual_nodo
    df["Costo Total (USD)"] = df["Costo Software (USD)"] + df["OPEX Local (USD)"]
    df["Precio Venta MSRP (USD)"] = df["Costo Total (USD)"] / (1 - margen_target)
    df["Ganancia Bruta (USD)"] = df["Precio Venta MSRP (USD)"] - df["Costo Total (USD)"]

    # 5. Visualización
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Costo Total Software", f"${df['Costo Software (USD)'].sum():,.0f}")
    col2.metric("OPEX Local Tecnoimagen", f"${df['OPEX Local (USD)'].sum():,.0f}")
    col3.metric("Facturación Target (MSRP)", f"${df['Precio Venta MSRP (USD)'].sum():,.0f}")
    col4.metric("Ganancia Bruta", f"${df['Ganancia Bruta (USD)'].sum():,.0f}")

    st.dataframe(df.style.format({
        "Costo Software (USD)": "${:,.2f}",
        "OPEX Local (USD)": "${:,.2f}",
        "Costo Total (USD)": "${:,.2f}",
        "Precio Venta MSRP (USD)": "${:,.2f}",
        "Ganancia Bruta (USD)": "${:,.2f}"
    }), use_container_width=True)
else:
    st.info("Cargando información de la infraestructura...")