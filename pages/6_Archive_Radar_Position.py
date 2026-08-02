"""Minimal test — verify page loads."""
import streamlit as st

st.set_page_config(page_title="BlueMet — Test", layout="wide")

from retro_theme import apply_retro_theme
apply_retro_theme()

from auth import check_password
check_password()

st.title("Test Page 6")
st.write("If you can see this, the file is loading.")