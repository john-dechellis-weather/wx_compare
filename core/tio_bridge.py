"""The viewer -> server bridge component, declared from a real module.

streamlit's declare_component looks up the calling frame's module; a
page run through st.navigation is exec'd code with no module, so
declaring it from pages/15_Large_Scale_Map.py raised
"module is None. This should never happen." (streamlit.log, 1 Oct) and
the Weather Mapping page never rendered. Declared here it has one.
"""

from pathlib import Path

import streamlit.components.v1 as components

STATIC = Path(__file__).resolve().parent.parent / "static"

tio_bridge = components.declare_component("tio_bridge", path=str(STATIC / "tio_bridge"))
