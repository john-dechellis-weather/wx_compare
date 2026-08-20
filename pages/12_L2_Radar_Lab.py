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
    region = st.selectbox("Region", list(L2.REGIONS), index=0)
    sites = st.multiselect(
        "Radar sites", L2.REGIONS[region],
        default=L2.REGIONS[region][:1],
        help="Start with one site. Each added site costs ~20 s and "
             "widens coverage. The grid centres itself on the first "
             "volume that loads, so any region works.",
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
    smooth_on = st.toggle(
        "Smoothing", value=True,
        help="On: Gaussian render smoothing plus seam matching at "
             "coverage handovers. Off: the raw gridded field, which "
             "shows gate wedges and a hard edge where one radar's "
             "coverage ends — useful for judging what the blend is "
             "actually doing.")

# Region defaults, overridable. The box is centred on the REGION,
# not on whichever radar loaded first — a KMLB-centred MCO box put
# its west edge on Leesburg and clipped the storms.
_clat, _clon, _hx, _hy = L2.REGION_VIEW.get(
    region, (None, None, 200, 200))
b1, b2, b3 = st.columns([1, 1, 2])
with b1:
    half_x = st.number_input("Half-width E-W (km)", 60, 500, _hx, 10)
    algo = st.selectbox("Merge algorithm", list(L2.COMBINERS), index=0,
                        help="How overlapping radars are combined.")
with b2:
    half_y = st.number_input("Half-width N-S (km)", 60, 500, _hy, 10)
with b3:
    st.caption(
        f"Centre {_clat:.2f}, {_clon:.2f}. Level II range is ~230 km "
        f"and beyond ~96 km the 0.5 deg beam is wider than the MRMS "
        f"grid, so extra width past that buys coverage, not "
        f"resolution."
        if _clat is not None else "No region centre; using first radar."
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
    # Smoothing is two separate stages and the toggle drives both:
    # the Gaussian on the finished field, and the gradient-keyed
    # seam match at coverage handovers.
    L2.SMOOTH_SIGMA = 1.0 if smooth_on else 0.0
    L2.RES_MATCH = bool(smooth_on)
    L2.COMBINE_FN = L2.COMBINERS.get(algo)
    L2.HALF_X_M = float(half_x) * 1000.0
    L2.HALF_Y_M = float(half_y) * 1000.0
    diag = {}
    if _clat is not None:
        L2.GRID_CENTER = (_clat, _clon)
        diag["center"] = [_clat, _clon]
        diag["center_fixed"] = True

    prog = st.progress(0.0, "Fetching volumes...")
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
        if comp is None:
            # A failed build used to have nowhere to say why: the
            # diagnostics expander only rendered on success.
            st.error("No site produced a grid. Per-site reasons:")
            st.json(diag)
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
