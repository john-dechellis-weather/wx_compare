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
