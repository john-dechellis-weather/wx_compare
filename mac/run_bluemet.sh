#!/bin/zsh
# BlueMet on the Mac mini: start Streamlit with the site's environment.
# launchd runs this (see org.bluemet.streamlit.plist); you can also run
# it by hand:  ~/bluemet/wx_compare/mac/run_bluemet.sh
set -e
[ -z "$HOME" ] && export HOME="/Users/$(whoami)"
REPO="$HOME/bluemet/wx_compare"
CONDA="$HOME/miniforge3"
cd "$REPO"
# Secrets and keys live in mac/.env (never committed). One KEY=value per line.
if [ -f "$REPO/mac/.env" ]; then
  set -a; source "$REPO/mac/.env"; set +a
fi
source "$CONDA/etc/profile.d/conda.sh"
conda activate bluemet
exec streamlit run Homepage.py \
  --server.address 127.0.0.1 \
  --server.port 8501 \
  --server.headless true \
  --browser.gatherUsageStats false
