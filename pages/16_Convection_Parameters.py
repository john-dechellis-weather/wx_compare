"""Convection Parameters - tomorrow.io's 48 h point forecast of the
convective fields for one station, ten panels (1 Oct).

Data: core/tio_convection.py (one timelines request per station per
hour, shared by every viewer through the persisted cache). The NWS
side of convection stays where it is (SPC on the login page, REFS
and the CAM pods); this is the tomorrow.io view beside it.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Convection Parameters", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

from core import tio_convection as CV
from core import tio_map as TIO
from core.stations import StationResolver

EDGE, PANEL, INK, INK2, MUTED = "#2D3957", "#0A0A0A", "#FFFFFF", "#B8B8B8", "#6E6E6E"
GRID = "#1A2233"
_persistent = Path("/opt/render/project/src/cache")
CACHE_ROOT = _persistent if _persistent.exists() else Path("/tmp/wx_compare_cache")
CACHE_ROOT.mkdir(parents=True, exist_ok=True)
HUBS = ["KJFK", "KBOS", "KFLL", "KMCO", "KEWR", "KLGA", "KDCA", "KLAX",
        "KSFO", "KTPA", "KDJT", "KBDL", "KHPN", "TJSJ"]
PANEL_H = int(os.environ.get("BLUEMET_CONV_PANEL_H", "190"))

st.markdown(
    "<style>"
    f'[data-testid="stVerticalBlockBorderWrapper"]{{'
    f"background:{PANEL} !important;border:1px solid {EDGE} !important;"
    "border-radius:4px !important;padding:8px 12px 6px !important;}"
    ".pod-title{color:#B8B8B8;font-size:11px;font-weight:700;"
    "letter-spacing:.6px;text-transform:uppercase;display:flex;"
    "justify-content:space-between;align-items:baseline;"
    "border-bottom:1px solid #1A2233;padding-bottom:5px;margin-bottom:4px;}"
    ".pod-title span{color:#6E6E6E;font-size:10px;font-weight:400;"
    "text-transform:none;letter-spacing:0}"
    "</style>", unsafe_allow_html=True)

st.markdown(
    '<div style="font-size:16px;font-weight:700;color:#FFFFFF;margin:0 0 2px 0">'
    'CONVECTION PARAMETERS</div>'
    '<div style="font-size:11px;font-weight:700;color:#B8B8B8;margin:0 0 8px 0">'
    f'tomorrow.io point forecast &middot; next {CV.HOURS} h hourly &middot; '
    'one request per station per hour, shared by everyone viewing</div>',
    unsafe_allow_html=True)

# ------------------------------------------------------ station picker
_c1, _c2, _c3, _c4 = st.columns([1.1, 1.1, 0.9, 3.9])
with _c1:
    typed = st.text_input("ENTER ICAO", value="", max_chars=4, placeholder="e.g. KSYR",
                          key="cv_icao_typed", help="Any ICAO; overrides the hub list").strip().upper()
with _c2:
    hub = st.selectbox("Hub", HUBS, index=0, key="cv_hub")
icao = typed if len(typed) == 4 else hub
with _c3:
    st.markdown('<div style="height:28px"></div>', unsafe_allow_html=True)
    force = st.button("Refresh now", key="cv_refresh",
                      help="Fetch again before the hourly refresh (one request)")


@st.cache_resource(show_spinner=False)
def _resolver():
    return StationResolver(cache_dir=CACHE_ROOT / "stations")


stn = _resolver().resolve(icao)
if stn is None:
    st.error(f"{icao}: unknown ICAO.")
    st.stop()

doc, note = CV.fetch(icao, stn.lat, stn.lon, force=force)
if doc is None:
    st.info(f"No data yet for {icao}" + (f" - {note}" if note else ""))
    st.stop()

age = CV.age_min(doc)
_sub = (f"{icao} {stn.name}  |  fetched {doc.get('fetched_iso', '?')} "
        f"({age:.0f} min ago; refreshes after {CV.TTL_MIN} min)")
if note:
    _sub += f"  |  {note}"
if doc.get("dropped"):
    _sub += "  |  not on this plan: " + ", ".join(doc["dropped"])
st.caption(_sub)

# ------------------------------------------------------------- panels
import plotly.graph_objects as go
import pandas as pd

data = doc["data"]
t = pd.to_datetime(data["t"], utc=True)
now = datetime.now(timezone.utc)
have = [f for f in CV.FIELDS if f[0] in data and any(v is not None for v in data[f[0]])]
missing = [f[1] for f in CV.FIELDS if f not in have]


def _panel(field, label, units, colour, kind):
    y = [None if v is None else float(v) for v in data[field]]
    vals = [v for v in y if v is not None]
    fig = go.Figure()
    if kind == "bar":
        fig.add_trace(go.Bar(x=t, y=y, marker_color=colour, opacity=0.85,
                             hovertemplate="%{x|%d/%HZ} %{y:.1f} " + units + "<extra></extra>"))
    else:
        r, g, b = (int(colour[i:i + 2], 16) for i in (1, 3, 5))
        fig.add_trace(go.Scatter(x=t, y=y, mode="lines", line=dict(color=colour, width=2.4),
                                 fill="tozeroy", fillcolor=f"rgba({r},{g},{b},0.18)",
                                 hovertemplate="%{x|%d/%HZ} %{y:.1f} " + units + "<extra></extra>"))
    if field in ("thunderstormProbability", "lightningProbability", "hailProbability",
                 "precipitationProbability"):
        fig.update_yaxes(range=[0, 100])
    elif field == "cin":
        fig.add_hline(y=-50, line=dict(color="#FFD400", width=1, dash="dot"))
        fig.update_yaxes(range=[min(-200, min(vals) if vals else -200), 0])
    elif field == "cape":
        for lvl, c in ((1000, "#FFD400"), (2500, "#FF3B30")):
            fig.add_hline(y=lvl, line=dict(color=c, width=1, dash="dot"))
        fig.update_yaxes(range=[0, max(3000, max(vals) if vals else 0)])
    elif field == "precipitationReflectivity":
        fig.add_hline(y=40, line=dict(color="#FF8A00", width=1, dash="dot"))
        fig.update_yaxes(range=[0, max(60, max(vals) if vals else 0)])
    elif field == "vorticity500":
        fig.add_hline(y=0, line=dict(color="#6E6E6E", width=1))
    else:
        fig.update_yaxes(rangemode="tozero")
    fig.add_vline(x=now, line=dict(color="#00E5FF", width=1))
    # day boundaries
    for d0 in pd.date_range(t.min().floor("D"), t.max(), freq="D"):
        if d0 > t.min():
            fig.add_vline(x=d0, line=dict(color="#2D3957", width=1, dash="dash"))
    fig.update_layout(height=PANEL_H, autosize=True, paper_bgcolor=PANEL,
                      plot_bgcolor="#05070B", showlegend=False,
                      font=dict(color=INK2, size=11, family="Roboto, Arial"),
                      margin=dict(l=44, r=10, t=6, b=24), bargap=0.15)
    fig.update_xaxes(gridcolor=GRID, zerolinecolor=GRID, tickformat="%HZ",
                     dtick=6 * 3600e3, range=[t.min(), t.max()])
    fig.update_yaxes(gridcolor=GRID, zerolinecolor=GRID, title=units)
    peak = (f"max {max(vals):.0f} {units}" if vals and field != "cin"
            else (f"min {min(vals):.0f} {units}" if vals else "no data"))
    return fig, peak


cols = st.columns(2)
for i, (field, label, units, colour, kind) in enumerate(have):
    with cols[i % 2]:
        with st.container(border=True):
            fig, peak = _panel(field, label, units, colour, kind)
            st.markdown(f'<div class="pod-title">{label}<span>{peak}</span></div>',
                        unsafe_allow_html=True)
            st.plotly_chart(fig, use_container_width=True,
                            config={"displayModeBar": False})

if missing:
    st.caption("No values returned for: " + ", ".join(missing)
               + ". Lightning and hail probability stop at +36 h on tomorrow.io; "
                 "a field absent for the whole window is not on the plan.")

with st.expander("tomorrow.io budget", expanded=False):
    _b = TIO.budget(Path(__file__).resolve().parent.parent / "static")
    st.caption(f"Requests today: {_b['used']:,} / {_b['cap']:,}  |  "
               f"{_b['left']:,} left  |  this page: 1 per station per "
               f"{CV.TTL_MIN} min while viewed")
