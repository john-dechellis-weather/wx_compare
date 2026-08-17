# static/

Served at `/app/static/` (enabled via `.streamlit/config.toml`).

This folder must exist in the repo for Streamlit to mount the
static route at startup. Its runtime contents are disposable:
the International map writes stitched Tomorrow.io IR mosaics here
as `ir_<YYYYMMDDHHMM>.png` and prunes anything older than an hour
on each cache miss. Nothing here needs to be committed except
this file.
