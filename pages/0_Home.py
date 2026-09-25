"""Home - product selection, where the login lands (24 Sep).

Template A: six big product pods three across, each a button with a
description and a how-to line under it; the secondary tools in a row
below; on the right a static box with the date, Zulu and every US time
zone at 16 pt, and the Text size control for the whole site.

The pods are st.page_link buttons (styled), not plain links: a plain
<a href> reloads the app and drops the session, st.page_link switches
page inside it.
"""

import streamlit as st

st.set_page_config(page_title="BlueMet - Home", layout="wide")

from retro_theme import apply_retro_theme

apply_retro_theme()

from dark_theme import apply_dark_theme

apply_dark_theme()

from auth import check_password

check_password()

# (page path, title, icon colour, description, how-to)
PRODUCTS = [
    ("pages/2_Station_Forecast.py", "Station Forecast", "#3ddc84",
     "Forecast plots, NBM and LAMP grids, METAR/TAF, a radar snapshot "
     "and the JetBlue movement board for one station.",
     "Open <b>Station &amp; plot settings</b> under the title to pick a "
     "station."),
    ("pages/3_JBU_Weather_Map.py", "JBU Weather Map CONUS", "#22d3ee",
     "Live national map: MRMS radar, station conditions, echo-top tags, "
     "fleet positions, holding and diversion alerts.",
     "Hover a station for its METAR. Thresholds are under "
     "<b>Alert thresholds</b>."),
    ("pages/Large_Scale_Map_of_North_America.py",
     "Large Scale Map of North America", "#6aa8ff",
     "Full-size map of North America and the test bench for new radar "
     "products.",
     "Choose a product; <b>Show inventory</b> lists what is live."),
    ("pages/9_HiRes_CAMs.py", "Hi-Res CAMs", "#ffd23f",
     "Deterministic high-resolution models for the Northeast and "
     "Florida.",
     "Pick a product per map, then drag the hour slider."),
    ("pages/11_REFS_Ensemble.py", "REFS Ensemble", "#ff8c1a",
     "Ensemble probabilities and PMMN reflectivity for the Northeast and "
     "Florida, three maps per row.",
     "Each row has its own valid hour. Wheel to zoom, double-click to "
     "reset."),
    ("pages/4_MOS_Tables.py", "MOS Tables", "#ff4fa3",
     "Hourly NBM and GFS LAMP side by side for one airport.",
     "Open <b>Airport</b>, enter an ICAO, then Refresh."),
]
MORE = [
    ("pages/14_L3_Radar_N90.py", "Level III Radar",
     "Super-resolution KOKX and KLWX, hour loop."),
    ("pages/12_L2_Radar_Lab.py", "L2 Radar Lab", "Level II experiments."),
    ("pages/5_Archive_Satellite_Position.py", "Archive Satellite",
     "Aircraft position on archived GOES imagery."),
    ("pages/6_Archive_Radar_Position.py", "Archive Radar",
     "Aircraft position on archived NEXRAD."),
]

# Body text on this page follows the site text size through
# --bm-dt (set in Homepage.py); titles and boxes do not.
_css = [
    "<style>",
    ".bm-h1{font:700 34px Roboto,sans-serif !important;color:#FFD700 !important;"
    "-webkit-text-fill-color:#FFD700 !important;margin:4px 0 0 0}",
    ".bm-sub{font-family:Roboto,sans-serif !important;"
    "font-size:calc(16px + var(--bm-dt,0pt)) !important;color:#b8b8b8 !important;"
    "-webkit-text-fill-color:#b8b8b8 !important;margin:4px 0 16px 0}",
    ".bm-grp{font:700 18px Roboto,sans-serif !important;color:#FFD700 !important;"
    "-webkit-text-fill-color:#FFD700 !important;border-bottom:1px solid #2a2d33;"
    "padding-bottom:6px;margin:22px 0 10px 0}",
    ".bm-desc{font-family:Roboto,sans-serif !important;font-weight:400 !important;"
    "font-size:calc(16px + var(--bm-dt,0pt)) !important;line-height:1.35;"
    "color:#e8e8e8 !important;-webkit-text-fill-color:#e8e8e8 !important;"
    "margin-top:8px}",
    ".bm-ins{font-family:Roboto,sans-serif !important;font-weight:700 !important;"
    "font-size:calc(14px + var(--bm-dt,0pt)) !important;line-height:1.35;"
    "color:#8a8f98 !important;-webkit-text-fill-color:#8a8f98 !important;"
    "margin:5px 0 16px 0}",
    ".bm-ins b{color:#22d3ee !important;-webkit-text-fill-color:#22d3ee !important}",
    # the pod button: the page link drawn as a big box
    # full column width: the link, its wrapper and element container
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'],"
    "[class*='st-key-bm_more_'] [data-testid='stPageLink'],"
    "[class*='st-key-bm_pod_'] [data-testid='stElementContainer'],"
    "[class*='st-key-bm_more_'] [data-testid='stElementContainer'],"
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] > div,"
    "[class*='st-key-bm_more_'] [data-testid='stPageLink'] > div{"
    "width:100% !important;max-width:none !important}",
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] a{"
    "position:relative;display:flex !important;flex-direction:column;"
    "justify-content:center;align-items:flex-start;min-height:140px;"
    "width:100% !important;box-sizing:border-box;"
    "padding:20px 22px !important;background:#0d0f12 !important;"
    "border:2px solid #3a3f47 !important;border-radius:4px;"
    "text-decoration:none !important;max-width:none !important}",
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] a:hover{"
    "border-color:#22d3ee !important;background:#0f1a1f !important}",
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] a:focus-visible{"
    "outline:2px solid #22d3ee;outline-offset:2px}",
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] a::after{"
    "content:'Open \\2192';position:absolute;right:18px;top:16px;"
    "font:700 15px Roboto,sans-serif;color:#22d3ee}",
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] a p,"
    "[class*='st-key-bm_pod_'] [data-testid='stPageLink'] a span{"
    "font:700 24px/1.15 Roboto,sans-serif !important;color:#fff !important;"
    "-webkit-text-fill-color:#fff !important;text-decoration:none !important;"
    "white-space:normal !important}",
    # the small secondary buttons
    "[class*='st-key-bm_more_'] [data-testid='stPageLink'] a{"
    "display:flex !important;align-items:center;height:62px;"
    "width:100% !important;box-sizing:border-box;"
    "padding:0 16px !important;background:#0d0f12 !important;"
    "border:2px solid #3a3f47 !important;border-radius:4px;"
    "text-decoration:none !important;max-width:none !important}",
    "[class*='st-key-bm_more_'] [data-testid='stPageLink'] a:hover{"
    "border-color:#22d3ee !important}",
    "[class*='st-key-bm_more_'] [data-testid='stPageLink'] a p,"
    "[class*='st-key-bm_more_'] [data-testid='stPageLink'] a span{"
    "font:700 18px Roboto,sans-serif !important;color:#fff !important;"
    "-webkit-text-fill-color:#fff !important;text-decoration:none !important}",
    # the clock box and the text size control inside it
    ".st-key-bm_clockbox{border:2px solid #3a3f47;background:#07080a;"
    "padding:12px 14px 6px 14px !important;gap:6px !important}",
    ".st-key-bm_clockbox [data-testid='stSelectbox'] label p{"
    "font:700 15px Roboto,sans-serif !important;color:#b8b8b8 !important;"
    "-webkit-text-fill-color:#b8b8b8 !important}",
    ".st-key-bm_clockbox [data-testid='stSelectbox'] div:has(> input){"
    "background:#000 !important;border:1px solid #3a3f47 !important}",
    ".st-key-bm_clockbox [data-testid='stSelectbox'] input{"
    "background:#000 !important;color:#fff !important;"
    "-webkit-text-fill-color:#fff !important;"
    "font:700 15px Roboto,sans-serif !important}",
    # the page's own clock box replaces the small fixed one here
    ".stApp div[style*='999998']{display:none !important}",
]
for _i, (_p, _t, _c, _d, _h) in enumerate(PRODUCTS):
    _css.append(
        f"[class*='st-key-bm_pod_{_i}'] [data-testid='stPageLink'] a::before{{"
        f"content:'';display:block;width:44px;height:44px;border-radius:4px;"
        f"background:{_c};margin-bottom:12px}}")
_css.append("</style>")
st.markdown("".join(_css), unsafe_allow_html=True)


def _clock_box():
    """Date, Zulu and every US zone, ticking in the browser (the
    offsets and DST come from the browser's own zone tables)."""
    import streamlit.components.v1 as _components
    _components.html("""
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;700&family=Roboto+Mono:wght@700&display=swap" rel="stylesheet">
<style>
 html,body{margin:0;background:#07080a;color:#fff;font-family:Roboto,Arial,sans-serif;font-weight:700}
 .d{font-size:16pt;color:#FFD700;margin:0 0 8px 0}
 table{border-collapse:collapse;width:100%}
 td{font-size:16pt;padding:2px 0} td.t{text-align:right;font-family:'Roboto Mono',monospace}
 tr.z td{color:#22d3ee;border-bottom:1px solid #2a2d33;padding-bottom:6px}
 tr.z + tr td{padding-top:6px}
</style>
<div class="d" id="d"></div><table id="tb"></table>
<script>
const Z=[["Atlantic (SJU)","America/Puerto_Rico"],["Eastern","America/New_York"],
 ["Central","America/Chicago"],["Mountain","America/Denver"],["Arizona","America/Phoenix"],
 ["Pacific","America/Los_Angeles"],["Alaska","America/Anchorage"],["Hawaii","Pacific/Honolulu"]];
const M=["January","February","March","April","May","June","July","August","September","October","November","December"];
function ord(n){const v=n%100; if(v>=11&&v<=13) return n+'th'; return n+({1:'st',2:'nd',3:'rd'}[n%10]||'th');}
function hm(d,tz){return new Intl.DateTimeFormat('en-GB',{timeZone:tz,hour:'2-digit',minute:'2-digit',hour12:false}).format(d);}
function ab(d,tz){const p=new Intl.DateTimeFormat('en-US',{timeZone:tz,timeZoneName:'short'}).formatToParts(d);
  const n=(p.find(x=>x.type==='timeZoneName')||{}).value||''; return n.replace('GMT-4','AST').replace('GMT-10','HST');}
function tick(){
  const d=new Date();
  document.getElementById('d').textContent=M[d.getUTCMonth()]+' '+ord(d.getUTCDate())+', '+d.getUTCFullYear();
  let h='<tr class="z"><td>Zulu</td><td class="t">'+hm(d,'UTC')+'Z</td></tr>';
  for(const [n,tz] of Z) h+='<tr><td>'+n+'</td><td class="t">'+hm(d,tz)+' '+ab(d,tz)+'</td></tr>';
  document.getElementById('tb').innerHTML=h;
}
tick(); setInterval(tick,1000);
</script>""", height=340)


# Site-wide text size. The widget's own key is dropped by Streamlit on
# pages where the widget is not drawn, so the choice is copied to a
# plain session key that Homepage.py reads on every page.
_SIZES = ["Smaller", "Medium (default)", "Large"]


def _keep_size():
    st.session_state["bm_text_size"] = st.session_state["bm_text_size_w"]


main, side = st.columns([3.3, 1], gap="large")

with side:
    with st.container(key="bm_clockbox"):
        _clock_box()
        _cur = st.session_state.get("bm_text_size", "Medium (default)")
        st.selectbox("Text size", _SIZES, index=_SIZES.index(_cur)
                     if _cur in _SIZES else 1,
                     key="bm_text_size_w", on_change=_keep_size)

with main:
    st.markdown('<div class="bm-h1">Choose a product</div>'
                '<div class="bm-sub">JetBlue System Operations weather</div>',
                unsafe_allow_html=True)
    for row in (PRODUCTS[:3], PRODUCTS[3:]):
        cols = st.columns(3, gap="medium")
        for col, (path, title, _c, desc, how) in zip(cols, row):
            i = PRODUCTS.index((path, title, _c, desc, how))
            with col:
                with st.container(key=f"bm_pod_{i}"):
                    st.page_link(path, label=title)
                st.markdown(f'<div class="bm-desc">{desc}</div>'
                            f'<div class="bm-ins">{how}</div>',
                            unsafe_allow_html=True)

    st.markdown('<div class="bm-grp">More tools</div>',
                unsafe_allow_html=True)
    cols = st.columns(4, gap="small")
    for j, (col, (path, title, desc)) in enumerate(zip(cols, MORE)):
        with col:
            with st.container(key=f"bm_more_{j}"):
                st.page_link(path, label=title)
            st.markdown(f'<div class="bm-desc" style="margin-top:6px">'
                        f'{desc}</div>', unsafe_allow_html=True)
