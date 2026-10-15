"""Duty Shift Check List (14 Oct): the shift's checklist. Items to
follow; for now the page shows who signed in and how (duty.py)."""

import streamlit as st

st.set_page_config(page_title="Duty Shift Check List", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

import duty

st.title("Duty Shift Check List")
d = duty.answers()
if d:
    bits = [d.get("role", "")]
    if d.get("shift"):
        bits.append(d["shift"])
    if d.get("x_today") is not None:
        bits.append(f"X-shift today: {d['x_today']}")
    if d.get("x_tomorrow") is not None:
        bits.append(f"X-shift tomorrow: {d['x_tomorrow']}")
    st.caption(" \u00b7 ".join(b for b in bits if b) + f"  \u00b7  signed in {d.get('at', '')}")
else:
    st.caption("No duty details for this session.")
if st.button("Change duty details", key="duty_reset"):
    st.session_state["duty"] = {}
    st.session_state["duty_done"] = False
    try:
        del st.query_params["d"]
    except Exception:
        pass
    st.rerun()
st.markdown("<div style='color:#6E6E6E;margin-top:24px'>Checklist items to follow.</div>",
            unsafe_allow_html=True)
