# wx_compare

Compare visibility and ceiling forecasts from multiple weather models, side by side, for any airport.

[![Open In Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/john-dechellis-weather/wx_compare/blob/main/wx_compare.ipynb)

![Dark-themed plot of visibility and ceiling with five model lines](https://img.shields.io/badge/models-5-blue) ![Forecast horizon](https://img.shields.io/badge/horizon-72hr-blue) ![Platform](https://img.shields.io/badge/runs%20on-Google%20Colab-orange)

## What it does

For any ICAO airport code, fetches the latest visibility and ceiling forecasts from five different sources, aligns them to the same time axis, and renders an interactive comparison plot. Useful for aviation forecasting, model evaluation, and learning about how different forecast systems disagree.

All output is normalized to **statute miles** (visibility) and **feet AGL** (ceiling).

## Getting started

### 1. Open the notebook in Colab

Click the "Open in Colab" badge above, or:
- Go to [colab.research.google.com](https://colab.research.google.com)
- File → Open notebook → GitHub tab
- Paste this repo's URL, select `wx_compare.ipynb`

### 2. (Optional) Add a Tomorrow.io API key

The four NOAA models work without any setup. If you also want Tomorrow.io, you'll need a free API key from [tomorrow.io](https://app.tomorrow.io/development/keys) (free tier: 500 calls/day, 25/hour).

To use it in Colab:
1. Click the **🔑 key icon** in Colab's left sidebar to open Secrets
2. Click **+ Add new secret**
3. Name: `TOMORROWIO_API_KEY`
4. Value: your API key
5. Toggle **Notebook access** to ON

The key is stored per-Colab-account and never enters the notebook file. If you skip this step, the notebook will simply skip Tomorrow.io and run with the four NOAA models.

### 3. Run

Run all cells. When prompted:
- Enter ICAO codes (e.g. `KJFK, KORD, KSEA`)
- Choose `auto` for the latest complete forecast cycle, or a specific hour (`00`, `06`, `12`, `18`)

You'll see an interactive Plotly chart with one line per model, hover tooltips showing exact values, and zoom/pan controls.

## Default behavior

- **Cycle selection.** "auto" walks back through 00/06/12/18Z cycles until it finds one where all NOAA models have fully posted. This usually means 4-6 hours of latency relative to real time.
- **X-axis.** Defaults to 48 hours from cycle time. Zoom in/out via the Plotly toolbar.
- **Y-axes.** Visibility 0-10 sm, ceiling 0-5000 ft. These cover the operationally interesting range; very clear conditions or very high ceilings will appear clipped.
- **Tomorrow.io's f+0 hour is dropped** because their nowcast hour often disagrees with current METAR observations.

## Caching

Downloaded model output is cached in your Google Drive at `MyDrive/wx_compare_cache/`. Subsequent runs of the same cycle are nearly instant. This also keeps you under NOMADS rate limits.

To clear the cache:
```python
import shutil
from pathlib import Path
shutil.rmtree(Path('/content/drive/MyDrive/wx_compare_cache'))
```

## Project structure

```
wx_compare/
├── wx_compare.ipynb        # The notebook you run
├── compare.py              # High-level entry point + plotting
├── core/
│   ├── schema.py           # ForecastRecord — common output shape
│   ├── units.py            # MOS category ↔ sm/ft conversions
│   ├── stations.py         # ICAO → lat/lon/elev via OpenFlights
│   └── cycle_select.py     # Detect latest complete model run
└── models/
    ├── base.py             # ModelSource abstract base class
    ├── hrrr.py             # GRIB2 byte-range fetch + cfgrib parse
    ├── gfs_mos.py          # MAV text bulletin parser
    ├── gfs_lamp.py         # LAV text bulletin parser
    ├── nbm.py              # NBH + NBS merged
    └── tomorrow_io.py      # Tomorrow.io v4 API client
```

Every model produces rows in the same schema (see `core/schema.py`), so the comparison and plotting code never has to know what the source was. To add a new model, drop a file in `models/`, subclass `ModelSource`, register it in `models/__init__.py`. The plot picks it up automatically.

## Known limitations

- **No ground truth.** This tool compares forecasts against each other but does not (yet) include METAR observations for verification.
- **NOMADS rate limits.** NOAA throttles aggressively; running many cycles in quick succession may get you temporarily blocked. The Drive cache mitigates this.

## Customizing

The notebook can be edited freely — it's mostly thin orchestration over the modules in `core/` and `models/`. A few common changes:

```python
# Different y-axis range
plot_comparison_interactive(df, 'KJFK', vis_ylim=(0, 6), ceiling_ylim=(0, 3000)).show()

# Different time window (24 hours instead of 48)
plot_comparison_interactive(df, 'KJFK', hours_ahead=24).show()

# Wider figure
plot_comparison_interactive(df, 'KJFK', width=1400).show()

# Static matplotlib plot instead of interactive
from compare import plot_comparison
plot_comparison(df, 'KJFK')
```

## License

MIT.

## Acknowledgments

Forecast data from NOAA / NCEP / MDL via [NOMADS](https://nomads.ncep.noaa.gov/). Airport metadata from [OpenFlights](https://openflights.org/data.html). Optional commercial forecast from [Tomorrow.io](https://www.tomorrow.io/).
