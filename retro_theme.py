"""Injects early-2000s HTML aesthetic into Streamlit.

Call apply_retro_theme() near the top of every page (after set_page_config
but before any UI). One call, styles the whole page.
"""
import streamlit as st


RETRO_CSS = """
<style>
/* --- Netscape gray body background --- */
.stApp {
    background: #C0C0C0 !important;
}

* {
    color: #000000 !important;
}

/* Main content area gets a white box with a black border, centered */
.main .block-container {
    background: #8F8482 !important;
    border: 2px solid #000000 !important;
    max-width: 800px !important;
    padding: 20px 24px !important;
    margin-top: 20px !important;
    font-family: "Times New Roman", Times, serif !important;
    color: #000000 !important;
}

/* --- Typography --- */
html, body, [class*="css"] {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 13px !important;
    color: #000000 !important;
}

h1 {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 22px !important;
    font-weight: bold !important;
    color: #000000 !important;
    margin: 0 0 4px 0 !important;
    padding: 0 !important;
    border-bottom: 3px double #000000 !important;
    padding-bottom: 6px !important;
}

h2 {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 16px !important;
    font-weight: bold !important;
    background: #E0E0E0 !important;
    color: #000000 !important;
    border: 1px solid #000000 !important;
    padding: 3px 8px !important;
    margin: 16px 0 8px 0 !important;
}

h3 {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 14px !important;
    font-weight: bold !important;
    color: #000000 !important;
    margin: 12px 0 4px 0 !important;
    text-decoration: underline !important;
}

p, div, span, label {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 13px !important;
}

/* Streamlit caption text (below titles) */
[data-testid="stCaptionContainer"], .stCaption {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12px !important;
    font-style: italic !important;
    color: #444444 !important;
}

/* --- Links --- */
a, a:link {
    color: #0000FF !important;
    text-decoration: underline !important;
}
a:visited { color: #800080 !important; }
a:hover { color: #FF0000 !important; }

/* --- Sidebar: keep it functional but restyle --- */
[data-testid="stSidebar"] {
    background: #E0E0E0 !important;
    border-right: 2px solid #000000 !important;
    font-family: "Times New Roman", Times, serif !important;
}
[data-testid="stSidebar"] * {
    font-family: "Times New Roman", Times, serif !important;
    color: #000000 !important;
}
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3 {
    background: transparent !important;
    color: #000000 !important;
    border-bottom: 1px solid #000000 !important;
    padding: 2px 0 !important;
    text-decoration: none !important;
}

/* --- Buttons: beveled outset --- */
.stButton > button,
.stDownloadButton > button {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 13px !important;
    font-weight: bold !important;
    background: #C0C0C0 !important;
    color: #000000 !important;
    border: 2px outset #FFFFFF !important;
    border-radius: 0 !important;
    padding: 4px 14px !important;
    box-shadow: none !important;
    cursor: pointer !important;
}
.stButton > button:hover,
.stDownloadButton > button:hover {
    background: #D0D0D0 !important;
}
.stButton > button:active,
.stDownloadButton > button:active {
    border-style: inset !important;
}

/* --- Text inputs --- */
input[type="text"], .stTextInput input {
    font-family: "Courier New", Courier, monospace !important;
    font-size: 13px !important;
    background: #FFFFFF !important;
    color: #000000 !important;
    border: 1px solid #000000 !important;
    border-radius: 0 !important;
    padding: 3px 5px !important;
}

/* Select boxes / dropdowns */
.stSelectbox [data-baseweb="select"] > div {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 13px !important;
    background: #FFFFFF !important;
    color: #000000 !important;
    border: 1px solid #000000 !important;
    border-radius: 0 !important;
}

/* Radio buttons — keep them plain */
.stRadio > div { font-family: "Times New Roman", Times, serif !important; }
.stRadio label {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 13px !important;
    color: #000000 !important;
}

/* --- Info/warning boxes: yellow highlight boxes --- */
.stAlert, [data-testid="stAlert"] {
    font-family: "Times New Roman", Times, serif !important;
    background: #FFFFCC !important;
    border: 1px solid #000000 !important;
    color: #000000 !important;
    border-radius: 0 !important;
    padding: 8px 12px !important;
}

/* Success box → light green */
[data-testid="stAlert"][data-baseweb-severity="success"],
.stSuccess {
    background: #CCFFCC !important;
}

/* Error box → light red */
[data-testid="stAlert"][data-baseweb-severity="error"],
.stError {
    background: #FFCCCC !important;
}

/* Warning box → default yellow, already handled above */

/* --- Data tables (st.dataframe / st.table) --- */
[data-testid="stDataFrame"] table,
[data-testid="stTable"] table {
    font-family: "Courier New", Courier, monospace !important;
    font-size: 12px !important;
    border-collapse: collapse !important;
    border: 1px solid #000000 !important;
}
[data-testid="stDataFrame"] th,
[data-testid="stTable"] th {
    background: #E0E0E0 !important;
    color: #000000 !important;
    border: 1px solid #000000 !important;
    padding: 3px 8px !important;
    font-weight: bold !important;
}
[data-testid="stDataFrame"] th,
[data-testid="stTable"] th {
    background: #E0E0E0 !important;
    color: #000000 !important;
    border: 1px solid #000000 !important;
    padding: 3px 8px !important;
    font-weight: bold !important;
}

/* Metric boxes (the counters at the top) — make them look boxy */
[data-testid="stMetric"] {
    background: #F0F0F0 !important;
    border: 2px inset #FFFFFF !important;
    padding: 6px 10px !important;
    font-family: "Times New Roman", Times, serif !important;
}
[data-testid="stMetricValue"] {
    font-family: "Courier New", Courier, monospace !important;
    font-size: 18px !important;
    font-weight: bold !important;
    color: #A3A379 !important;
}
[data-testid="stMetricLabel"] {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 12px !important;
    color: #000000 !important;
}

/* --- Tabs: chunky bordered tabs --- */
.stTabs [data-baseweb="tab-list"] {
    background: #C0C0C0 !important;
    border-bottom: 2px solid #000000 !important;
    gap: 0 !important;
}
.stTabs [data-baseweb="tab"] {
    font-family: "Times New Roman", Times, serif !important;
    font-size: 13px !important;
    background: #C0C0C0 !important;
    color: #000000 !important;
    border: 1px solid #000000 !important;
    border-bottom: none !important;
    border-radius: 0 !important;
    padding: 4px 12px !important;
    margin-right: 2px !important;
    font-weight: bold !important;
}
.stTabs [aria-selected="true"] {
    background: #8F8482 !important;
    color: #000000 !important;
}

/* Slider — mostly leave alone but soften it */
.stSlider [data-baseweb="slider"] {
    font-family: "Times New Roman", Times, serif !important;
}

/* --- Streamlit header bar (top of every page) --- */
[data-testid="stHeader"] {
    background: transparent !important;
    height: 0 !important;
}
</style>
"""


def apply_retro_theme():
    """Call once per page after set_page_config."""
    st.markdown(RETRO_CSS, unsafe_allow_html=True)
