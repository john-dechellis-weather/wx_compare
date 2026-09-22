"""BlueMet - Ops Black.

Target path: dark_theme.py (repo root, beside retro_theme.py)

Call apply_dark_theme() once per page, at the top, in place of
apply_retro_theme(). Keep the monospace family from the retro theme;
what changes is the palette and the type floor.

    Body text      12 px minimum, #FFFFFF
    Secondary      #B8B8B8 - labels, captions, column headers
    Muted          #6E6E6E - rules, disabled, placeholder
    Background     #000000, panels #0A0A0A, borders #333333

Nothing here sets a colour on the alert values themselves. Those come
from core/station_status.py and from whatever the TAF board already
emits, so the rules stay in one place.
"""

from __future__ import annotations

import streamlit as st

# ---------------------------------------------------------------- palette

BG        = "#000000"
PANEL     = "#0A0A0A"
PANEL_ALT = "#121212"
BORDER    = "#333333"
RULE      = "#1E1E1E"

TEXT      = "#FFFFFF"
TEXT_2    = "#B8B8B8"
MUTED     = "#6E6E6E"

CYAN      = "#00E5FF"
GREEN     = "#00FF7F"
YELLOW    = "#FFD400"
ORANGE    = "#FF8A00"
PINK      = "#FF00C8"
RED       = "#FF3B30"

#: Aircraft stay JetBlue blue on every surface. Do not theme these.
JBU_BLUE  = "#4DA3FF"

# Type scale. Body is the floor; headings step up from it.
FS_BODY   = "12px"
FS_TABLE  = "12px"
FS_H3     = "16px"
FS_H2     = "20px"
FS_H1     = "26px"

MONO = ('"DejaVu Sans Mono", "SFMono-Regular", Menlo, Consolas, '
        '"Liberation Mono", monospace')


_CSS = f"""
<style>
:root {{
  --bm-bg: {BG};
  --bm-panel: {PANEL};
  --bm-panel-alt: {PANEL_ALT};
  --bm-border: {BORDER};
  --bm-rule: {RULE};
  --bm-text: {TEXT};
  --bm-text-2: {TEXT_2};
  --bm-muted: {MUTED};
  --bm-cyan: {CYAN};
  --bm-green: {GREEN};
  --bm-yellow: {YELLOW};
  --bm-orange: {ORANGE};
  --bm-pink: {PINK};
  --bm-red: {RED};
  --bm-mono: {MONO};
}}

/* ---------------------------------------------------------- surfaces */

html, body, .stApp,
[data-testid="stAppViewContainer"],
[data-testid="stHeader"],
[data-testid="stToolbar"],
[data-testid="stBottom"] {{
  background: var(--bm-bg) !important;
  color: var(--bm-text) !important;
  font-family: var(--bm-mono) !important;
}}

[data-testid="stSidebar"],
[data-testid="stSidebarContent"] {{
  background: var(--bm-panel) !important;
  border-right: 1px solid var(--bm-border) !important;
}}

[data-testid="stHeader"] {{ border-bottom: 1px solid var(--bm-rule) !important; }}

/* ------------------------------------------------------------- type */

.stApp, .stApp p, .stApp li, .stApp span, .stApp div,
.stApp label, [data-testid="stMarkdownContainer"] {{
  color: var(--bm-text) !important;
  font-size: {FS_BODY};
}}

.stApp h1 {{ color: var(--bm-text) !important; font-size: {FS_H1};
             letter-spacing: .5px; }}
.stApp h2 {{ color: var(--bm-text) !important; font-size: {FS_H2}; }}
.stApp h3, .stApp h4, .stApp h5, .stApp h6 {{
  color: var(--bm-text) !important; font-size: {FS_H3};
}}

/* ------------------------------------------------- sidebar navigation */

/* Page links are anchors, so without these they take the browser's
   default link and VISITED colours - blue and purple on black, which
   is what makes an already-visited page unreadable. Pin every state. */
[data-testid="stSidebar"] a,
[data-testid="stSidebar"] a:link,
[data-testid="stSidebar"] a:visited,
[data-testid="stSidebar"] a:hover,
[data-testid="stSidebar"] a:active,
[data-testid="stSidebar"] a span,
[data-testid="stSidebar"] a p,
[data-testid="stSidebarNav"] a,
[data-testid="stSidebarNav"] a *,
[data-testid="stSidebarNav"] span,
[data-testid="stSidebarNav"] p {{
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
  text-decoration: none !important;
  font-size: {FS_BODY} !important;
}}

[data-testid="stSidebar"] a:hover,
[data-testid="stSidebarNav"] a:hover {{ background: #161616 !important; }}

[data-testid="stNavSectionHeader"],
[data-testid="stNavSectionHeader"] * {{
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
  font-weight: 700 !important;
}}

/* The selected-page pill. */
[data-testid="stSidebarNav"] li [aria-current="page"],
[data-testid="stSidebarNav"] li a[aria-selected="true"] {{
  background: #1C1C22 !important;
}}

.stApp small,
[data-testid="stCaptionContainer"],
[data-testid="stCaptionContainer"] * {{
  color: var(--bm-text-2) !important;
  -webkit-text-fill-color: var(--bm-text-2) !important;
  font-size: {FS_BODY} !important;
}}

.stApp a, .stApp a:visited {{ color: var(--bm-cyan) !important; }}
.stApp hr {{ border-color: var(--bm-rule); }}

/* Anything still carrying an explicit dark colour from an older
   stylesheet. Alert values set their own colour inline and are not
   caught by this, which is the point. */
.stApp [style*="color: rgb(0, 0, 0)"],
.stApp [style*="color:#000"],
.stApp [style*="color: #000"],
.stApp [style*="color: black"] {{ color: var(--bm-text) !important; }}

/* ----------------------------------------------------------- tables */

/* Markdown / HTML tables: the TAF alert board and the METAR strip.
   12 px, bold, white on black, thin rules. */
.stApp table {{
  border-collapse: collapse;
  background: var(--bm-panel);
  color: var(--bm-text);
  font-family: var(--bm-mono);
  font-size: {FS_TABLE};
}}
.stApp table th {{
  color: var(--bm-text);
  background: var(--bm-panel-alt);
  font-size: {FS_TABLE};
  font-weight: 700;
  text-align: left;
  padding: 5px 9px;
  border-bottom: 1px solid var(--bm-border);
  white-space: nowrap;
}}
.stApp table td {{
  color: var(--bm-text);
  font-size: {FS_TABLE};
  font-weight: 700;
  padding: 4px 9px;
  border-bottom: 1px solid var(--bm-rule);
}}
.stApp table tr:hover td {{ background: #141414; }}

/* st.dataframe / st.data_editor draw through a canvas grid that only
   reads the config.toml theme, so the rules above cannot reach them.
   These two keep the frame and toolbar from staying light. */
[data-testid="stDataFrame"],
[data-testid="stDataFrameResizable"] {{
  border: 1px solid var(--bm-border) !important;
  background: var(--bm-panel) !important;
}}
[data-testid="stElementToolbar"] {{
  background: var(--bm-panel-alt) !important;
  border: 1px solid var(--bm-border) !important;
}}

/* ------------------------------------------------- alerts and banners */

/* st.info / st.success / st.warning / st.error draw pale pastel
   panels with near-black text, which is unreadable on this theme.
   Streamlit renders them through baseweb, so the fill lives on an
   inner container - every level of the nesting is pinned or the
   pastel shows through at the edges. */
[data-testid="stAlert"],
[data-testid="stAlertContainer"],
[data-testid="stNotification"],
[data-testid="stAlert"] [data-baseweb="notification"],
[data-testid="stAlert"] > div,
[data-testid="stNotificationContentInfo"],
[data-testid="stNotificationContentSuccess"],
[data-testid="stNotificationContentWarning"],
[data-testid="stNotificationContentError"] {{
  background: var(--bm-panel-alt) !important;
  border-radius: 3px !important;
  box-shadow: none !important;
}}

[data-testid="stAlert"] *,
[data-testid="stAlertContainer"] *,
[data-testid="stNotification"] * {{
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
  font-size: {FS_BODY} !important;
}}

/* The severity is carried by a left edge instead of a fill, so the
   four levels still read apart at a glance. */
[data-testid="stAlertContainer"] {{
  border: 1px solid var(--bm-border) !important;
  border-left: 4px solid var(--bm-cyan) !important;
  padding: 10px 14px !important;
}}
[data-testid="stAlert"]:has([data-testid="stNotificationContentSuccess"])
  [data-testid="stAlertContainer"] {{
  border-left-color: var(--bm-green) !important;
}}
[data-testid="stAlert"]:has([data-testid="stNotificationContentWarning"])
  [data-testid="stAlertContainer"] {{
  border-left-color: var(--bm-yellow) !important;
}}
[data-testid="stAlert"]:has([data-testid="stNotificationContentError"])
  [data-testid="stAlertContainer"] {{
  border-left-color: var(--bm-red) !important;
}}

/* Icons ship as SVG that inherits currentColor. */
[data-testid="stAlert"] svg {{ fill: var(--bm-text) !important; }}

/* st.exception and st.toast use the same pastel family. */
[data-testid="stException"],
[data-testid="stToast"] {{
  background: var(--bm-panel-alt) !important;
  border: 1px solid var(--bm-border) !important;
}}
[data-testid="stException"] *,
[data-testid="stToast"] * {{
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
}}

/* ---------------------------------------------------------- widgets */

.stApp [data-testid="stWidgetLabel"] label,
.stApp [data-testid="stWidgetLabel"] p {{
  color: var(--bm-text-2) !important;
  font-size: {FS_BODY} !important;
  font-weight: 700;
}}

.stApp [data-baseweb="select"] > div,
.stApp [data-baseweb="input"] > div,
.stApp input, .stApp textarea {{
  background: var(--bm-panel) !important;
  border-color: var(--bm-border) !important;
  color: var(--bm-text) !important;
  font-family: var(--bm-mono) !important;
  font-size: {FS_BODY} !important;
}}
.stApp input::placeholder {{ color: var(--bm-muted) !important; }}

/* Dropdown panels render in a portal outside .stApp. */
[data-baseweb="popover"] [role="listbox"],
[data-baseweb="menu"] {{
  background: var(--bm-panel) !important;
  border: 1px solid var(--bm-border) !important;
}}
[data-baseweb="popover"] [role="option"],
[data-baseweb="menu"] li {{
  color: var(--bm-text) !important;
  font-family: var(--bm-mono) !important;
  font-size: {FS_BODY} !important;
}}
[data-baseweb="popover"] [role="option"]:hover,
[data-baseweb="menu"] li:hover {{ background: #161616 !important; }}

.stApp [data-testid="stCheckbox"] label span,
.stApp [data-testid="stRadio"] label span {{
  color: var(--bm-text) !important;
  font-size: {FS_BODY} !important;
}}

.stApp button[kind="primary"] {{
  background: var(--bm-cyan) !important;
  color: #000000 !important;
  border: none !important;
  font-weight: 700;
}}
.stApp button[kind="secondary"] {{
  background: var(--bm-panel) !important;
  color: var(--bm-text) !important;
  border: 1px solid var(--bm-border) !important;
  font-weight: 700;
}}

/* Slider: the radar-opacity and similar controls. */
.stApp [data-testid="stSlider"] [data-baseweb="slider"] div[role="slider"] {{
  background: var(--bm-cyan) !important;
}}

/* ------------------------------------------- choice controls */

/* Region pickers and similar. Streamlit draws these as radios,
   segmented controls, pills or a button group depending on version
   and call, and each keeps its own dark-text default, so all four
   are pinned rather than guessing which one a page used. */
[data-testid="stRadio"] label,
[data-testid="stRadio"] label *,
[data-testid="stRadio"] div[role="radiogroup"] *,
[data-testid="stSegmentedControl"] *,
[data-testid="stButtonGroup"] *,
[data-testid="stPills"] *,
[data-testid="stMultiSelect"] *,
[data-testid="stSelectbox"] * {{
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
}}

/* The unselected segments read as panels; the selected one lifts. */
[data-testid="stSegmentedControl"] button,
[data-testid="stButtonGroup"] button,
[data-testid="stPills"] button {{
  background: var(--bm-panel) !important;
  border: 1px solid var(--bm-border) !important;
}}
[data-testid="stSegmentedControl"] button[aria-checked="true"],
[data-testid="stSegmentedControl"] button[aria-pressed="true"],
[data-testid="stButtonGroup"] button[aria-checked="true"],
[data-testid="stButtonGroup"] button[aria-pressed="true"],
[data-testid="stPills"] button[aria-checked="true"] {{
  background: #1C1C22 !important;
  border-color: var(--bm-cyan) !important;
}}

/* Multiselect chips. */
[data-testid="stMultiSelect"] [data-baseweb="tag"] {{
  background: #1C1C22 !important;
  border: 1px solid var(--bm-border) !important;
}}

/* Tabs. */
[data-testid="stTabs"] button[role="tab"],
[data-testid="stTabs"] button[role="tab"] * {{
  color: var(--bm-text-2) !important;
  -webkit-text-fill-color: var(--bm-text-2) !important;
}}
[data-testid="stTabs"] button[role="tab"][aria-selected="true"],
[data-testid="stTabs"] button[role="tab"][aria-selected="true"] * {{
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
}}

/* --------------------------------------------------- panels, expanders */

.stApp [data-testid="stExpander"] {{
  background: var(--bm-panel) !important;
  border: 1px solid var(--bm-border) !important;
}}
.stApp [data-testid="stExpander"] summary,
.stApp [data-testid="stExpander"] summary * {{
  color: var(--bm-text) !important;
  font-size: {FS_BODY} !important;
}}

.stApp [data-testid="stMetricValue"] {{ color: var(--bm-text) !important; }}
.stApp [data-testid="stMetricLabel"] {{ color: var(--bm-text-2) !important; }}

.stApp pre, .stApp code {{
  background: var(--bm-panel) !important;
  color: var(--bm-text) !important;
  border: 1px solid var(--bm-rule) !important;
  font-size: {FS_BODY} !important;
}}

/* The warmer-log expander on the Home page. */
.stApp [data-testid="stExpander"] pre {{ color: var(--bm-text-2) !important; }}

/* ------------------------------------------------ rerun behaviour */

/* While a script reruns, Streamlit keeps the previous page on screen
   dimmed ("stale") until the new one arrives. After sign-in that is
   the login board ghosting behind the first page for as long as it
   takes to render; on a slow page it is the old frame. Hide stale
   elements instead. */
.stApp [data-stale="true"] {{
  opacity: 0 !important;
  pointer-events: none !important;
}}

/* A loading banner for every rerun. The status widget exists only
   while the script is running, so this pins a fixed banner to the top
   of the page for exactly that long - no JavaScript, nothing a page
   has to call. */
.stApp:has([data-testid="stStatusWidget"])::before {{
  content: "Loading \2026  refresh the page after 1 min if it does not render";
  position: fixed;
  top: 0; left: 0; right: 0;
  z-index: 999999;
  padding: 7px 0;
  text-align: center;
  background: #1C1C22;
  color: var(--bm-text);
  border-bottom: 1px solid var(--bm-cyan);
  font-family: var(--bm-mono);
  font-size: {FS_BODY};
  font-weight: 700;
  letter-spacing: .3px;
}}

/* ------------------------------------------ sidebar collapse arrows */

/* retro_theme.py draws its own « » arrows and a "Click arrows to view
   weather products" label over the sidebar toggle, and in current
   Streamlit that lands on top of the button's own text ("Page View
   Toggle"). The arrows go: the sidebar stays open and the toggle is
   hidden. Should the sidebar ever be collapsed anyway, the expand
   control keeps its real icon (icon font restored) and nothing else. */
[data-testid="stSidebarCollapseButton"] {{ display: none !important; }}

[data-testid="stSidebarCollapseButton"] span::after,
[data-testid="stHeader"] span[data-testid="stIconMaterial"]::before,
[data-testid="stHeader"] span[data-testid="stIconMaterial"]::after,
[data-testid="stSidebarCollapsedControl"] span[data-testid="stIconMaterial"]::before,
[data-testid="stSidebarCollapsedControl"] span[data-testid="stIconMaterial"]::after,
[data-testid="collapsedControl"] span[data-testid="stIconMaterial"]::before,
[data-testid="collapsedControl"] span[data-testid="stIconMaterial"]::after {{
  content: none !important;
}}

/* Material icons everywhere: the mono font-family forced on .stApp
   otherwise turns the icon ligatures into their names as text. */
span[data-testid="stIconMaterial"],
.material-symbols-rounded, .material-symbols-outlined {{
  font-family: "Material Symbols Rounded", "Material Symbols Outlined" !important;
  font-size: 1.25rem !important;
  color: var(--bm-text) !important;
  -webkit-text-fill-color: var(--bm-text) !important;
}}

/* ------------------------------------------------------------- deck */

/* The map canvas sits on black; kill the white gutter around it. */
.stApp [data-testid="stDeckGlJsonChart"],
.stApp .deckgl-wrapper, .stApp .mapboxgl-map {{
  background: var(--bm-bg) !important;
}}
.stApp .mapboxgl-ctrl-attrib,
.stApp .mapboxgl-ctrl-attrib a {{
  background: rgba(0,0,0,.6) !important;
  color: var(--bm-muted) !important;
}}
</style>
"""


def apply_dark_theme() -> None:
    """Inject the Ops Black stylesheet. Call once, at the top of a page."""
    st.markdown(_CSS, unsafe_allow_html=True)


# --------------------------------------------------------------- helpers

def panel_open(title: str | None = None) -> str:
    """Opening HTML for a bordered panel, for pages that build raw HTML."""
    head = (f'<div style="color:{TEXT_2};font-size:{FS_BODY};font-weight:700;'
            f'padding:8px 12px 0 12px">{title}</div>') if title else ""
    return (f'<div style="background:{PANEL};border:1px solid {BORDER};'
            f'border-radius:4px">{head}')


PANEL_CLOSE = "</div>"
