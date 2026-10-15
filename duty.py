"""Duty prompts after the password (14 Oct).

Once a session is signed in, before the first page draws, the user
says who they are and - for SOC MET - which shift and whether the
X-shift is on today / scheduled tomorrow. The answers live in
st.session_state["duty"] for the Duty Shift Check List page, and the
session then lands on the JBU CONUS map.

Steps (each one screen, two big buttons):
  1. SOC MET | MetExpert
  2. Select shift: AM Shift | PM Shift            (SOC MET only)
  3. Is X-shift working today?  Yes | No           (SOC MET only)
  4. Is there a scheduled X-shift tomorrow?  Yes | No   (SOC MET only)

Embedded panels (Map Splitter) and token-authenticated reloads of an
already-answered session never see the prompts.
"""

from __future__ import annotations

from datetime import datetime, timezone

import streamlit as st

LANDING = "pages/3_JBU_Weather_Map.py"
_PANEL_PARAMS = ("embed", "table", "layer", "ts", "panel")


def answered() -> bool:
    return bool(st.session_state.get("duty_done"))


def answers() -> dict:
    return dict(st.session_state.get("duty") or {})


def _embedded() -> bool:
    try:
        qp = st.query_params
        return any(qp.get(k) for k in _PANEL_PARAMS) or bool(st.session_state.get("bm_embed"))
    except Exception:
        return False


def _css() -> None:
    st.markdown(
        "<style>"
        ".st-key-duty [data-testid='stButton'] button{"
        "height:84px !important;font-size:22px !important;font-weight:700 !important;"
        "letter-spacing:1px;width:100%;background:#1B2A4A !important;color:#FFFFFF !important;"
        "-webkit-text-fill-color:#FFFFFF !important;border:1px solid #2D3957 !important;"
        "border-radius:8px !important}"
        ".st-key-duty [data-testid='stButton'] button:hover{background:#2D3957 !important;"
        "border-color:#00E5FF !important}"
        ".st-key-duty [data-testid='stButton'] button p,.st-key-duty [data-testid='stButton'] button div{"
        "font-size:24px !important;font-weight:700 !important;color:#FFFFFF !important;"
        "-webkit-text-fill-color:#FFFFFF !important}"
        ".duty-q{font-size:30px !important;font-weight:700;color:#FFFFFF;-webkit-text-fill-color:#FFFFFF;"
        "margin:40px 0 22px 0;text-align:center}"
        ".duty-s{font-size:14px !important;color:#B8B8B8;text-align:center;margin:60px 0 0 0;letter-spacing:2px}"
        ".duty-crumb{font-size:13px !important;color:#6E6E6E;text-align:center;margin-top:22px;letter-spacing:.5px}"
        "</style>", unsafe_allow_html=True)


def _two(label_a: str, label_b: str, key: str) -> str | None:
    with st.container(key="duty"):
        _l, c1, c2, _r = st.columns([1, 1.2, 1.2, 1])
        with c1:
            if st.button(label_a, key=f"{key}_a", use_container_width=True):
                return label_a
        with c2:
            if st.button(label_b, key=f"{key}_b", use_container_width=True):
                return label_b
    return None


def _crumb(d: dict) -> None:
    bits = [d.get("role")]
    if d.get("shift"):
        bits.append(d["shift"])
    if d.get("x_today") is not None:
        bits.append(f"X-shift today: {d['x_today']}")
    if d.get("x_tomorrow") is not None:
        bits.append(f"X-shift tomorrow: {d['x_tomorrow']}")
    bits = [b for b in bits if b]
    if bits:
        st.markdown(f'<div class="duty-crumb">{" &middot; ".join(bits)}</div>',
                    unsafe_allow_html=True)


def gate() -> None:
    """Show the prompts until answered; then land on the CONUS map.
    Returns silently when nothing is needed."""
    if answered() or _embedded():
        return
    d = st.session_state.setdefault("duty", {})
    _css()
    st.markdown('<div class="duty-s">BLUEMET &middot; sign-in</div>', unsafe_allow_html=True)

    if not d.get("role"):
        st.markdown('<div class="duty-q">Who is signing in?</div>', unsafe_allow_html=True)
        pick = _two("SOC MET", "MetExpert", "duty_role")
        if pick:
            d["role"] = pick
            if pick == "MetExpert":
                _finish(d)
            st.rerun()
        st.stop()

    if not d.get("shift"):
        st.markdown('<div class="duty-q">Select Shift</div>', unsafe_allow_html=True)
        pick = _two("AM Shift", "PM Shift", "duty_shift")
        if pick:
            d["shift"] = pick
            st.rerun()
        _crumb(d)
        st.stop()

    if d.get("x_today") is None:
        st.markdown('<div class="duty-q">Is X-shift working today?</div>', unsafe_allow_html=True)
        pick = _two("Yes", "No", "duty_xt")
        if pick:
            d["x_today"] = pick
            st.rerun()
        _crumb(d)
        st.stop()

    if d.get("x_tomorrow") is None:
        st.markdown('<div class="duty-q">Is there a scheduled X-shift tomorrow?</div>',
                    unsafe_allow_html=True)
        pick = _two("Yes", "No", "duty_xm")
        if pick:
            d["x_tomorrow"] = pick
            _finish(d)
            st.rerun()
        _crumb(d)
        st.stop()

    _finish(d)


def _finish(d: dict) -> None:
    d["at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    st.session_state["duty"] = d
    st.session_state["duty_done"] = True
    try:
        st.switch_page(LANDING)
    except Exception:
        pass
