"""Level II Radar Lab — N90 super-resolution mosaic (EXPERIMENTAL).

Deliberately isolated. Nothing on this page is imported by any other
page and it writes only to its own static/ filenames, so a failure
here cannot take down the CONUS or International maps. Once the
mosaic is trusted it moves into the warmer and onto page 3; until
then this is the only place it runs.

Everything is manual. No fragment, no run_every, no prefetch: a
build reads four Level II volumes off S3 and grids them, which costs
~1.4-1.8 GB and 75-95 s. Nothing that expensive should ever fire on
page load.
"""

import os
import time
import traceback
from pathlib import Path

import streamlit as st

from auth import check_password

check_password()

st.set_page_config(page_title="L2 Radar Lab", layout="wide")
st.title("Level II Radar Lab — N90")
st.caption(
    "Experimental super-resolution multi-radar mosaic. 250 m gates vs "
    "MRMS 840 m, which wins inside ~52 nm of a site and is 2-5x better "
    "inside 30 nm. Outside that radius MRMS is still the better "
    "product."
)

try:
    from core import radar_l2 as L2
except Exception as exc:  # arm_pyart missing is the likely cause
    st.error(
        f"Could not import core.radar_l2 — {type(exc).__name__}: {exc}\n\n"
        "If this is a ModuleNotFoundError for pyart, add `arm_pyart` "
        "to requirements.txt and redeploy."
    )
    st.stop()

# ---------------------------------------------------------------------------
# Controls
# ---------------------------------------------------------------------------
c1, c2, c3, c4 = st.columns([2, 1, 1, 1])
with c1:
    sites = st.multiselect(
        "Radar sites", list(L2.N90_SITES),
        default=["KOKX"],
        help="Start with KOKX alone — it sits inside the terminal "
             "area. Each added site costs ~20 s and widens coverage.",
    )
with c2:
    tilts = st.selectbox("Tilts", [2, 4, 6, 8], index=1,
                         help="Lowest N sweeps. Peak memory and wall "
                              "time both scale with this; 8 tilts is "
                              "~1.8 GB and ~95 s for four sites.")
with c3:
    levels = st.selectbox("Levels", [1, 3, 5, 6], index=1,
                          help="Vertical grid levels from 500 m up, "
                               "1 km apart. Composite = max over them.")
with c4:
    res_m = st.selectbox("Grid", [250, 500], index=0,
                         format_func=lambda v: f"{v} m")

half_km = st.slider(
    "Box half-width (km)", 60, 250, 120, 10,
    help="Level II range is ~230 km. Beyond ~96 km the 0.5 deg beam "
         "is wider than the MRMS grid spacing, so the resolution "
         "advantage is gone.",
)

run = st.button("Build mosaic", type="primary")

st.divider()

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
_OUT = Path(__file__).resolve().parent.parent / "static"


def _abs_url(name: str):
    """pydeck's BitmapLayer image prop must be an ABSOLUTE https URL.

    pydeck.types.Image passes URL-looking strings straight through but
    treats anything else as a local file path, opens it, and inlines
    it as base64 — which then dies in deck.gl's JS expression parser
    on the colon in "data:". Both failure modes were hit live 8/17.
    """
    base = (os.environ.get("RENDER_EXTERNAL_URL")
            or os.environ.get("PUBLIC_BASE_URL") or "").rstrip("/")
    return f"{base}/app/static/{name}" if base else None


if run:
    if not sites:
        st.warning("Pick at least one site.")
        st.stop()
    L2.TILTS = int(tilts)
    L2.LEVELS = int(levels)
    L2.RES_M = float(res_m)
    L2.HALF_M = float(half_km) * 1000.0

    prog = st.progress(0.0, "Fetching volumes from S3...")
    diag = {}
    t0 = time.time()
    try:
        comp, diag = L2.build_mosaic(sites=sites, diag=diag)
        prog.progress(0.75, "Rendering...")
        if comp is not None:
            name = f"l2_{int(time.time())}.png"
            L2.render_png(comp, _OUT / name, diag)
            # prune old lab renders; this page can generate a lot
            for old in sorted(_OUT.glob("l2_*.png"))[:-4]:
                try:
                    old.unlink()
                except OSError:
                    pass
            st.session_state["_l2"] = {
                "name": name, "diag": diag,
                "bounds": list(L2.bounds()),
            }
        prog.empty()
    except Exception:
        prog.empty()
        st.error("Mosaic build failed — full traceback below.")
        st.code(traceback.format_exc(), language="text")
        diag["error"] = "exception during build"
    st.caption(f"Total wall time {time.time() - t0:.1f}s")

# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------
state = st.session_state.get("_l2")
if not state:
    st.info(
        "No mosaic built yet. Start with KOKX, 4 tilts, 3 levels, "
        "250 m, 120 km — that is about 20 s and proves the S3 path "
        "before spending 95 s on four sites."
    )
else:
    diag = state["diag"]
    url = _abs_url(state["name"])

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Wall time", f"{diag.get('wall_s', '?')} s")
    m2.metric("Coverage", f"{diag.get('coverage_pct', '?')}%")
    m3.metric("Scan spread", f"{diag.get('scan_spread_s', '?')} s",
              help="Gap between earliest and latest site scan. This "
                   "is the mosaic's real time error — a 40 kt storm "
                   "moves ~2 nm in 4 min, so a large spread means "
                   "the sites disagree about where things are.")
    m4.metric("PNG", f"{diag.get('png_bytes', 0) / 1000:.0f} KB")

    if url:
        try:
            import pydeck as pdk

            w, s, e, n = state["bounds"]
            st.pydeck_chart(pdk.Deck(
                layers=[pdk.Layer(
                    "BitmapLayer", data=None, image=url,
                    bounds=[w, s, e, n], opacity=0.75,
                )],
                initial_view_state=pdk.ViewState(
                    latitude=L2.GRID_CENTER[0],
                    longitude=L2.GRID_CENTER[1],
                    zoom=6.5, min_zoom=4, max_zoom=12,
                ),
                map_style="light",
            ), height=680)
        except Exception as exc:
            st.warning(f"Map render failed — {type(exc).__name__}: {exc}")
        st.caption(f"Bitmap: {url}")
    else:
        st.warning(
            "RENDER_EXTERNAL_URL / PUBLIC_BASE_URL not set — cannot "
            "build an absolute URL for the map layer. Raw render "
            "below instead."
        )
        st.image(str(_OUT / state["name"]))

    with st.expander("Diagnostics", expanded=True):
        st.caption(
            "Per-site lines show the volume filename, tilt count, "
            "input gate count and fetch time. A site that fails names "
            "itself here rather than silently vanishing."
        )
        st.json(diag)

    with st.expander("Known limitations"):
        st.markdown(
            "- **Max-merge seams.** Sites are combined with an "
            "element-wise max, which treats a gate 10 nm from KOKX "
            "at 600 ft as equal to one 90 nm from KENX sampling "
            "11,000 ft. Expect hard edges where coverage ends. "
            "Range- and height-weighted blending replaces this.\n"
            "- **No advection correction.** Volumes are up to a few "
            "minutes apart; fast storms will show displacement "
            "between sites. Watch the scan-spread metric.\n"
            "- **No inter-radar calibration.** Sites differ by a "
            "dB or two, which shows as steps in overlap regions.\n"
            "- **No TDWR yet.** C-band attenuates behind heavy "
            "cores, so max-merging it with S-band would make the "
            "airport radars read low exactly when it matters."
        )
