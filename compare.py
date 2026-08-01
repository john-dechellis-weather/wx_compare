"""Comparison and visualization.

This is the join point. Every model produces rows in the same schema
(see core/schema.py), so comparison is just concat + groupby.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence, TYPE_CHECKING

import pandas as pd

from core.stations import StationResolver, Station
from models.base import ModelSource

if TYPE_CHECKING:
    import matplotlib


# Dark-theme color scheme shared by both the static and interactive plots.
_DARK_BG = "#000000"
_DARK_FG = "#ffffff"
_DARK_GRID = "#3a3a3a"
_MODEL_COLORS = {
    "HRRR":    "#5ec1ea",   # cyan
    "GFS_MOS": "#ff8a3d",   # orange
    "NBM":     "#a4e857",   # bright green
    "TOMORROW_IO":  "#0100FF",   # blue
    # New models: add here. Anything not in this dict falls back to FG (white).
}


def run_comparison(
    sources: list[ModelSource],
    cycle: datetime,
    stations: Iterable[str],
) -> pd.DataFrame:
    """Fetch + parse for one cycle across all sources. Returns a long-form
    DataFrame ready for pivoting or plotting.

    Low-level entry point: callers must have already constructed every
    source (including any station-aware ones like HRRR). For the simpler
    'I just have a list of ICAOs' workflow, use compare_icaos() instead.
    """
    frames = []
    for src in sources:
        df = src.get_forecast(cycle, stations)
        if len(df) > 0:
            frames.append(df)
    if not frames:
        from core.schema import empty_df
        return empty_df()
    return pd.concat(frames, ignore_index=True)


def compare_icaos(
    icaos: Sequence[str],
    cycle: datetime,
    cache_root: Path,
    model_classes: Optional[Sequence[type]] = None,
    hrrr_fhours: Iterable[int] = range(0, 19),
) -> tuple[pd.DataFrame, list[Station], list[str]]:
    """High-level entry point. Takes only ICAOs and a cycle; handles all
    station resolution and source construction internally.

    Returns:
        df: long-form comparison DataFrame (may be empty)
        resolved: Station objects for ICAOs that were recognized
        unresolved: ICAO strings that the resolver couldn't find

    Adding a new model: include its class in model_classes (defaults to
    ALL_MODEL_CLASSES from models/__init__.py). The dispatch below decides
    whether it needs Station metadata or just ICAO strings.
    """
    # Late import to avoid a circular dependency: models/__init__ imports
    # nothing from compare, but tests sometimes import compare first.
    from models import GfsMos, Hrrr, ALL_MODEL_CLASSES

    if model_classes is None:
        model_classes = ALL_MODEL_CLASSES

    cache_root = Path(cache_root)
    resolver = StationResolver(cache_dir=cache_root / "stations")
    resolved, unresolved = resolver.resolve_many(list(icaos))
    if not resolved:
        from core.schema import empty_df
        return empty_df(), [], unresolved

    resolved_icaos = [s.icao for s in resolved]

    # Construct each source. Some need Station objects (HRRR — for point
    # extraction), others only need ICAOs (MOS — looks up by string in
    # the bulletin). New gridded models go in the station-aware branch;
    # new text/bulletin models go in the simple branch.
    sources: list[ModelSource] = []
    for cls in model_classes:
        if cls is Hrrr:
            sources.append(Hrrr(
                cache_dir=cache_root / "hrrr",
                stations=resolved,
                fhours=hrrr_fhours,
            ))
        else:
            # Default: assume the source's constructor takes just cache_dir.
            sources.append(cls(cache_dir=cache_root / cls.name.lower()))

    df = run_comparison(sources, cycle, resolved_icaos)
    return df, resolved, unresolved


def pivot_for_plot(
    df: pd.DataFrame,
    station_id: str,
    variable: str,  # 'vsby_sm' or 'ceiling_ft'
) -> pd.DataFrame:
    """One column per model, indexed by valid_time. Easy to plot."""
    sub = df[df["station_id"] == station_id]
    if len(sub) == 0:
        return pd.DataFrame()
    return (sub
            .pivot_table(index="valid_time", columns="model", values=variable, aggfunc="first")
            .sort_index())


def plot_comparison(
    df: pd.DataFrame,
    station_id: str,
    cycle: Optional[datetime] = None,
    vis_ylim: tuple[float, float] = (0, 10),
    ceiling_ylim: tuple[float, float] = (0, 5000),
) -> "matplotlib.figure.Figure":
    """Two-panel plot for one station: visibility on top, ceiling on bottom.

    Parameters:
        df: long-form comparison DataFrame from run_comparison / compare_icaos
        station_id: ICAO to plot
        cycle: model run cycle (UTC). If omitted, inferred from df['cycle'].
        vis_ylim: visibility axis range in statute miles (default 0-10)
        ceiling_ylim: ceiling axis range in feet AGL (default 0-5000)
    """
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    fig, axes = plt.subplots(2, 1, figsize=(11, 7.5), sharex=True)

    sub = df[df["station_id"] == station_id]
    if cycle is None and len(sub) > 0:
        cycle = pd.to_datetime(sub["cycle"].iloc[0]).to_pydatetime()

    # Use direct ax.plot() so the x-axis stays in matplotlib's standard
    # date2num space — required for the secondary forecast-hour axis math
    # below to come out right.
    _plot_panel(
        axes[0], sub, "vsby_sm",
        ylabel="Visibility (statute miles)",
        title="Visibility", ylim=vis_ylim,
    )
    _plot_panel(
        axes[1], sub, "ceiling_ft",
        ylabel="Ceiling (feet AGL)",
        title="Ceiling", ylim=ceiling_ylim,
    )

    axes[1].set_xlabel("Valid time (UTC)")
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %HZ"))
    axes[1].xaxis.set_major_locator(mdates.AutoDateLocator(minticks=4, maxticks=10))
    fig.autofmt_xdate(rotation=0, ha="center")

    if cycle is not None:
        _add_forecast_hour_axis(axes[0], cycle)

    if cycle is not None:
        cycle_str = pd.to_datetime(cycle).strftime("%Y-%m-%d %HZ")
        fig.suptitle(
            f"{station_id} — VIS/CIG forecast comparison\n"
            f"Model run: {cycle_str}",
            fontsize=12, y=0.995
        )
    else:
        fig.suptitle(f"{station_id} — VIS/CIG forecast comparison", fontsize=12)

    fig.tight_layout()
    return fig


def _plot_panel(ax, sub_df, value_col, ylabel, title, ylim):
    """Plot one model-per-line panel with consistent styling."""
    if len(sub_df) == 0:
        return
    for model_name, group in sub_df.groupby("model", sort=True):
        g = group.sort_values("valid_time")
        # Convert valid_time -> pure Python datetimes for matplotlib.
        xs = pd.to_datetime(g["valid_time"]).dt.to_pydatetime()
        ax.plot(xs, g[value_col].values, marker="o", label=str(model_name))
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.set_ylim(*ylim)
    ax.grid(True, alpha=0.3)
    ax.legend(title="Model", loc="best")


def _add_forecast_hour_axis(ax_lower, cycle: datetime) -> None:
    """Attach a secondary x-axis at the top showing forecast hours (f+N)
    aligned with the lower axis's valid-time ticks."""
    import matplotlib.dates as mdates

    secax = ax_lower.twiny()
    # Mirror the lower axis x-limits exactly so ticks align.
    secax.set_xlim(ax_lower.get_xlim())

    # cycle needs to be in the same numeric space (days since matplotlib
    # epoch) as the lower-axis tick positions.
    cycle_num = mdates.date2num(pd.to_datetime(cycle))
    lower_ticks = ax_lower.get_xticks()
    # (tick - cycle_num) is in days; multiply by 24 for hours.
    fhours = [(t - cycle_num) * 24.0 for t in lower_ticks]
    secax.set_xticks(lower_ticks)
    secax.set_xticklabels([
        f"f+{int(round(h))}" if h >= 0 else "" for h in fhours
    ])
    secax.set_xlabel("Forecast hour")
    secax.tick_params(axis="x", which="both", length=3)


# ---------------------------------------------------------------------------
# Interactive Plotly version
# ---------------------------------------------------------------------------
def plot_comparison_interactive(
    df: pd.DataFrame,
    station_id: str,
    cycle: Optional[datetime] = None,
    vis_ylim: tuple[float, float] = (0, 10),
    ceiling_ylim: tuple[float, float] = (0, 5000),
    hours_ahead: float = 48,
    width: int = 1000,
    metars_df: Optional[pd.DataFrame] = None,
):
    """Interactive Plotly version of the comparison plot.

    Features the static plot_comparison() doesn't have:
      - Hover any point for a tooltip with model, valid time, forecast hour,
        vis, and ceiling
      - Box-zoom (drag a rectangle), pan, wheel zoom — both panels share the
        x-axis so zooming one zooms the other
      - Click a model in the legend to hide/show its lines
      - Double-click to reset zoom
      - Save-as-PNG button in the modebar (top-right)

    Returns a plotly.graph_objects.Figure. In Colab/Jupyter, the returned
    figure renders inline automatically when it's the last expression in
    a cell. Or call fig.show() explicitly.
    """
    # Import is local so the rest of the library doesn't pull in plotly.
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    sub = df[df["station_id"] == station_id].copy()
    if cycle is None and len(sub) > 0:
        cycle = pd.to_datetime(sub["cycle"].iloc[0]).to_pydatetime()

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        subplot_titles=("Visibility", "Ceiling"),
        vertical_spacing=0.09,
    )

    for model_name, group in sub.groupby("model", sort=True):
        g = group.sort_values("valid_time")
        color = _MODEL_COLORS.get(str(model_name), _DARK_FG)
        valid_times = pd.to_datetime(g["valid_time"])
        forecast_hours = g["forecast_hour"].tolist()

        hover_text = [
            _format_hover(model_name, vt, fh, vis, cig, unlim)
            for vt, fh, vis, cig, unlim in zip(
                valid_times, forecast_hours,
                g["vsby_sm"], g["ceiling_ft"], g["ceiling_unlimited"],
            )
        ]

        # Top panel: visibility
        fig.add_trace(
            go.Scatter(
                x=valid_times, y=g["vsby_sm"],
                mode="lines+markers", name=str(model_name),
                line=dict(color=color, width=2.5),
                marker=dict(size=8, color=color),
                hovertext=hover_text, hoverinfo="text",
                legendgroup=str(model_name),
            ),
            row=1, col=1,
        )

        # Bottom panel: ceiling. Share legendgroup so toggling hides both panels.
        fig.add_trace(
            go.Scatter(
                x=valid_times, y=g["ceiling_ft"],
                mode="lines+markers", name=str(model_name),
                line=dict(color=color, width=2.5),
                marker=dict(size=8, color=color),
                hovertext=hover_text, hoverinfo="text",
                legendgroup=str(model_name), showlegend=False,
            ),
            row=2, col=1,
        )

    # METAR observations overlay — red markers, ground truth
    if metars_df is not None and len(metars_df) > 0:
        obs = metars_df[metars_df["station_id"] == station_id].sort_values("obs_time")
        if len(obs) > 0:
            obs_times = pd.to_datetime(obs["obs_time"])
            # Top panel: visibility observations
            vis_hover = [
                f"<b>METAR</b><br>{t.strftime('%Y-%m-%d %HZ')}<br>Vis: {v} sm"
                for t, v in zip(obs_times, obs["vsby_sm"])
            ]

            # Visibility connector line — dashed red, only when vis <= 4 sm
            vis_line_x = []
            vis_line_y = []
            for t, v in zip(obs_times, obs["vsby_sm"]):
                if pd.notna(v) and v <= 4:
                    vis_line_x.append(t)
                    vis_line_y.append(v)
                else:
                    # Break the line at any point above threshold or missing
                    if vis_line_x and vis_line_x[-1] is not None:
                        vis_line_x.append(None)
                        vis_line_y.append(None)
            if any(x is not None for x in vis_line_x):
                fig.add_trace(
                    go.Scatter(
                        x=vis_line_x, y=vis_line_y,
                        mode="lines", name="METAR trend",
                        line=dict(color="#FF3333", width=1.5),
                        hoverinfo="skip",
                        legendgroup="METAR", showlegend=False,
                    ),
                    row=1, col=1,
                )

            # Top panel: visibility observations

            fig.add_trace(
                go.Scatter(
                    x=obs_times, y=obs["vsby_sm"],
                    mode="markers", name="METAR obs",
                    marker=dict(size=10, color="#FF3333", symbol="triangle-up",
                                line=dict(color="#FFFFFF", width=0.75)),
                    hovertext=vis_hover, hoverinfo="text",
                    legendgroup="METAR",
                ),
                row=1, col=1,
            )
            # Bottom panel: ceiling observations
            # Clamp unlimited ceilings to the top of the y-axis so they're visible
            ceil_plot = obs.apply(
                lambda r: ceiling_ylim[1] if r["ceiling_unlimited"] else r["ceiling_ft"],
                axis=1,
            )
            ceil_hover = [
                f"<b>METAR</b><br>{t.strftime('%Y-%m-%d %HZ')}<br>"
                f"Ceiling: {'UNL' if u else f'{c:.0f} ft'}"
                for t, c, u in zip(obs_times, obs["ceiling_ft"], obs["ceiling_unlimited"])
            ]

            # Ceiling connector line — dashed red, only when ceiling <= 3000 ft
            # Unlimited ceilings and > 3000 ft break the line
            ceil_line_x = []
            ceil_line_y = []
            for t, c, u in zip(obs_times, obs["ceiling_ft"], obs["ceiling_unlimited"]):
                if not u and pd.notna(c) and c <= 3000:
                    ceil_line_x.append(t)
                    ceil_line_y.append(c)
                else:
                    if ceil_line_x and ceil_line_x[-1] is not None:
                        ceil_line_x.append(None)
                        ceil_line_y.append(None)
            if any(x is not None for x in ceil_line_x):
                fig.add_trace(
                    go.Scatter(
                        x=ceil_line_x, y=ceil_line_y,
                        mode="lines", name="METAR trend",
                        line=dict(color="#FF3333", width=1.5),
                        hoverinfo="skip",
                        legendgroup="METAR", showlegend=False,
                    ),
                    row=2, col=1,
                )

            fig.add_trace(
                go.Scatter(
                    x=obs_times, y=ceil_plot,
                    mode="markers", name="METAR obs",
                    marker=dict(size=10, color="#FF3333", symbol="triangle-up",
                                line=dict(color="#FFFFFF", width=0.75)),
                    hovertext=ceil_hover, hoverinfo="text",
                    legendgroup="METAR", showlegend=False,
                ),
                row=2, col=1,
            )


            fig.add_trace(
                go.Scatter(
                    x=obs_times, y=ceil_plot,
                    mode="markers", name="METAR obs",
                    marker=dict(size=10, color="#FF3333", symbol="triangle-up",
                                line=dict(color="#FFFFFF", width=0.75)),
                    hovertext=ceil_hover, hoverinfo="text",
                    legendgroup="METAR", showlegend=False,
                ),
                row=2, col=1,
            )

    # Layout — dark theme to match the static plot
    title_text = f"{station_id} — VIS/CIG forecast comparison"
    if cycle is not None:
        title_text += (
            f"<br><sub>Model run: "
            f"{pd.to_datetime(cycle).strftime('%Y-%m-%d %HZ')}</sub>"
        )

    fig.update_layout(
        paper_bgcolor=_DARK_BG,
        plot_bgcolor=_DARK_BG,
        font=dict(color=_DARK_FG, size=12),
        title=dict(text=title_text, font=dict(color=_DARK_FG, size=14), x=0.5),
        hovermode="closest",
        height=620,
        margin=dict(l=70, r=30, t=90, b=60),
        legend=dict(
            bgcolor="rgba(0,0,0,0.4)", bordercolor="#888", borderwidth=1,
            font=dict(color=_DARK_FG), title=dict(text="Model"),
        ),
    )

    # Axes
    fig.update_xaxes(
        color=_DARK_FG, gridcolor=_DARK_GRID,
        dtick=3 * 3600 * 1000,
        showgrid=True,
        gridwidth=0.5,
        row=1, col=1,
    )
    # Initial x-axis range: cycle to cycle + hours_ahead.
    x_range = None
    if cycle is not None and hours_ahead is not None:
        from datetime import timedelta as _td
        c = pd.to_datetime(cycle)
        x_range = [c, c + _td(hours=hours_ahead)]

    fig.update_xaxes(
        title_text="Valid time (UTC)",
        color=_DARK_FG, gridcolor=_DARK_GRID,
        tickformat="%m-%d %HZ",
        range=x_range,
        row=2, col=1,
    )
    fig.update_xaxes(
        title_text="Valid time (UTC)",
        color=_DARK_FG, gridcolor=_DARK_GRID,
        tickformat="%m-%d %HZ",
        range=x_range,
        dtick=3 * 3600 * 1000,    # 3 hours in milliseconds
        #tickangle=-45,             # angle labels for readability
        showgrid=True,
        gridwidth=0.5,
        row=2, col=1,
    )
    fig.update_yaxes(
        title_text="Ceiling (feet AGL)",
        range=list(ceiling_ylim),
        color=_DARK_FG, gridcolor=_DARK_GRID,
        row=2, col=1,
    )

    # Color the subplot titles to match
    for ann in fig["layout"]["annotations"]:
        ann["font"] = dict(color=_DARK_FG, size=12)

    return fig
  


def _format_hover(model, vt, fh, vis, cig, unlim) -> str:
    """Build a multi-line hover tooltip with all the context a forecaster
    would want about one data point."""
    vt_str = pd.to_datetime(vt).strftime("%Y-%m-%d %HZ")
    if unlim:
        cig_str = "unlimited"
    elif pd.notna(cig):
        cig_str = f"{cig:.0f} ft"
    else:
        cig_str = "—"
    vis_str = f"{vis:.1f} sm" if pd.notna(vis) else "—"
    return (
        f"<b>{model}</b><br>"
        f"{vt_str}  (f+{int(fh)})<br>"
        f"Visibility: {vis_str}<br>"
        f"Ceiling: {cig_str}"
    )


# ---------------------------------------------------------------------------
# Wind comparison plot (interactive Plotly version)
# ---------------------------------------------------------------------------

def plot_wind_comparison_interactive(
    df: pd.DataFrame,
    station_id: str,
    cycle: Optional[datetime] = None,
    speed_ylim: tuple[float, float] = (0, 40),
    hours_ahead: float = 48,
    width: int = 1000,
    height: int = 720,
    metars_df: Optional[pd.DataFrame] = None,
):
    """Two stacked panels: wind speed (top) and wind direction (bottom).

    Speed panel renders as lines + markers.
    Direction panel uses MARKERS ONLY (no lines) to avoid 360°/0° wrap artifacts.
    Gust shown as dotted line on the speed panel when available.
    METAR observations overlay as red markers when metars_df is provided.
    """
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    sub = df[df["station_id"] == station_id].copy()
    if cycle is None and len(sub) > 0:
        cycle = pd.to_datetime(sub["cycle"].iloc[0]).to_pydatetime()

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        subplot_titles=("Wind speed (kt)", "Wind direction (° from)"),
        vertical_spacing=0.09,
    )

    for model_name, group in sub.groupby("model", sort=True):
        g = group.sort_values("valid_time")
        color = _MODEL_COLORS.get(str(model_name), _DARK_FG)
        valid_times = pd.to_datetime(g["valid_time"])
        forecast_hours = g["forecast_hour"].tolist()

        hover_text = [
            _format_wind_hover(model_name, vt, fh, s, d, gu)
            for vt, fh, s, d, gu in zip(
                valid_times, forecast_hours,
                g["wind_speed_kt"], g["wind_dir_deg"], g["wind_gust_kt"],
            )
        ]

        # Speed panel — sustained wind
        fig.add_trace(
            go.Scatter(
                x=valid_times, y=g["wind_speed_kt"],
                mode="lines+markers", name=str(model_name),
                line=dict(color=color, width=2.5),
                marker=dict(size=8, color=color),
                hovertext=hover_text, hoverinfo="text",
                legendgroup=str(model_name),
            ),
            row=1, col=1,
        )

        # Speed panel — gust overlay (dashed, only models that report it)
        if g["wind_gust_kt"].notna().any():
            fig.add_trace(
                go.Scatter(
                    x=valid_times, y=g["wind_gust_kt"],
                    mode="lines", name=f"{model_name} gust",
                    line=dict(color=color, width=1.5, dash="dot"),
                    hoverinfo="skip",
                    legendgroup=str(model_name),
                    showlegend=False,
                ),
                row=1, col=1,
            )

        # Direction panel — MARKERS ONLY to avoid 360°/0° wrap artifacts
        fig.add_trace(
            go.Scatter(
                x=valid_times, y=g["wind_dir_deg"],
                mode="markers", name=str(model_name),
                marker=dict(size=8, color=color),
                hovertext=hover_text, hoverinfo="text",
                legendgroup=str(model_name), showlegend=False,
            ),
            row=2, col=1,
        )

    # METAR observations overlay — red markers, ground truth
    if metars_df is not None and len(metars_df) > 0:
        obs = metars_df[metars_df["station_id"] == station_id].sort_values("obs_time")
        if len(obs) > 0:
            obs_times = pd.to_datetime(obs["obs_time"])

            def _obs_hover(t, s, d, g):
                spd = f"{s:.0f} kt" if pd.notna(s) else "—"
                if pd.notna(d):
                    dir_str = f"{int(d):03d}° ({_deg_to_cardinal(d)})"
                else:
                    dir_str = "—"
                gst = f"<br>Gust: {g:.0f} kt" if pd.notna(g) else ""
                return (f"<b>METAR</b><br>{t.strftime('%Y-%m-%d %HZ')}<br>"
                        f"Wind: {dir_str} @ {spd}{gst}")

            wind_hover = [
                _obs_hover(t, s, d, g)
                for t, s, d, g in zip(
                    obs_times, obs["wind_speed_kt"],
                    obs["wind_dir_deg"], obs["wind_gust_kt"],
                )
            ]
            
            # Top: speed — sustained wind as circles
            fig.add_trace(
                go.Scatter(
                    x=obs_times, y=obs["wind_speed_kt"],
                    mode="markers", name="METAR obs",
                    marker=dict(size=10, color="#FF3333", symbol="triangle-up",
                                line=dict(color="#FFFFFF", width=1)),
                    hovertext=wind_hover, hoverinfo="text",
                    legendgroup="METAR",
                ),
                row=1, col=1,
            )

            # Top: gust — thin vertical lines from sustained to gust value
            # Build a scatter with x=[t, t, None, t, t, None, ...] and
            # y=[speed, gust, None, speed, gust, None, ...] pattern.
            # None values break the line, creating separate vertical segments.
            gust_mask = obs["wind_gust_kt"].notna()
            if gust_mask.any():
                line_x = []
                line_y = []
                for t, s, g in zip(
                    obs_times[gust_mask],
                    obs.loc[gust_mask, "wind_speed_kt"],
                    obs.loc[gust_mask, "wind_gust_kt"],
                ):
                    line_x.extend([t, t, None])
                    line_y.extend([s, g, None])
                fig.add_trace(
                    go.Scatter(
                        x=line_x, y=line_y,
                        mode="lines", name="METAR gust line",
                        line=dict(color="#FF3333", width=1.5),
                        hoverinfo="skip",
                        legendgroup="METAR", showlegend=False,
                    ),
                    row=1, col=1,
                )

            # Top: gust value — triangle-up markers
            gust_hover = [
                f"<b>METAR GUST</b><br>{t.strftime('%Y-%m-%d %HZ')}<br>Gust: {g:.0f} kt"
                for t, g in zip(obs_times[gust_mask], obs.loc[gust_mask, "wind_gust_kt"])
            ]
            if gust_mask.any():
                fig.add_trace(
                    go.Scatter(
                        x=obs_times[gust_mask],
                        y=obs.loc[gust_mask, "wind_gust_kt"],
                        mode="markers", name="METAR gust",
                        marker=dict(size=12, symbol="triangle-up-open",
                                color="#FF3333",
                                line=dict(color="#FF3333", width=2)),
                        hovertext=gust_hover, hoverinfo="text",
                        legendgroup="METAR", showlegend=False,
                    ),
                    row=1, col=1,
                )

            
            
            # Bottom: direction
            fig.add_trace(
                go.Scatter(
                    x=obs_times, y=obs["wind_dir_deg"],
                    mode="markers", name="METAR obs",
                    marker=dict(size=10, color="#FF3333", symbol="triangle-up",
                                line=dict(color="#FFFFFF", width=1)),
                    hovertext=wind_hover, hoverinfo="text",
                    legendgroup="METAR", showlegend=False,
                ),
                row=2, col=1,
            )

    title_text = f"{station_id} — Wind forecast comparison"
    if cycle is not None:
        title_text += (
            f"<br><sub>Model run: "
            f"{pd.to_datetime(cycle).strftime('%Y-%m-%d %HZ')}  ·  "
            "Sustained = solid, gust = dotted</sub>"
        )

    fig.update_layout(
        paper_bgcolor=_DARK_BG, plot_bgcolor=_DARK_BG,
        font=dict(color=_DARK_FG, size=12),
        title=dict(text=title_text, font=dict(color=_DARK_FG, size=14), x=0.5),
        hovermode="closest",
        height=height, width=width,
        margin=dict(l=70, r=30, t=90, b=60),
        legend=dict(
            bgcolor="rgba(0,0,0,0.4)", bordercolor="#888", borderwidth=1,
            font=dict(color=_DARK_FG), title=dict(text="Model"),
        ),
    )

    # Time range
    x_range = None
    if cycle is not None and hours_ahead is not None:
        from datetime import timedelta as _td
        c = pd.to_datetime(cycle)
        x_range = [c, c + _td(hours=hours_ahead)]

    fig.update_xaxes(color=_DARK_FG, gridcolor=_DARK_GRID, showgrid=True, gridwidth=0.5, row=1, col=1)
    fig.update_xaxes(
        title_text="Valid time (UTC)",
        color=_DARK_FG, gridcolor=_DARK_GRID,
        tickformat="%m-%d %HZ", range=x_range,
        dtick=3 * 3600 * 1000, tickangle=-45,
        showgrid=True, gridwidth=0.5,
        row=2, col=1,
    )
    fig.update_yaxes(
        title_text="Speed (kt)", range=list(speed_ylim),
        color=_DARK_FG, gridcolor=_DARK_GRID, row=1, col=1,
    )
    fig.update_yaxes(
        title_text="Direction (° from)",
        range=[0, 360],
        tickvals=[0, 90, 180, 270, 360],
        ticktext=["N (0)", "E (90)", "S (180)", "W (270)", "N (360)"],
        color=_DARK_FG, gridcolor=_DARK_GRID,
        row=2, col=1,
    )

    for ann in fig["layout"]["annotations"]:
        ann["font"] = dict(color=_DARK_FG, size=12)


    # Time range
    x_range = None
    if cycle is not None and hours_ahead is not None:
        from datetime import timedelta as _td
        c = pd.to_datetime(cycle)
        x_range = [c, c + _td(hours=hours_ahead)]

    fig.update_xaxes(
        color=_DARK_FG, 
        gridcolor=_DARK_GRID, 
        tickformat="%m-%d %HZ",
        range=x_range,
        dtick=3 * 3600 * 1000,
        showgrid=True,
        gridwidth=0.5,
        row=1, col=1
    )
    
    fig.update_xaxes(
        title_text="Valid time (UTC)",
        color=_DARK_FG, gridcolor=_DARK_GRID,
        tickformat="%m-%d %HZ",
        range=x_range,
        dtick=3 * 3600 * 1000,    # 3 hours in milliseconds
        #tickangle=-45,             # angle labels for readability
        showgrid=True,
        gridwidth=0.5,
        row=2, col=1,
    )
    fig.update_yaxes(
        title_text="Speed (kt)", range=list(speed_ylim),
        color=_DARK_FG, gridcolor=_DARK_GRID, row=1, col=1,
    )
    # Direction axis: 0-360 with N/E/S/W tick labels
    fig.update_yaxes(
        title_text="Direction (° from)",
        range=[0, 360],
        tickvals=[0, 90, 180, 270, 360],
        ticktext=["N (0)", "E (90)", "S (180)", "W (270)", "N (360)"],
        color=_DARK_FG, gridcolor=_DARK_GRID,
        row=2, col=1,
    )

    for ann in fig["layout"]["annotations"]:
        ann["font"] = dict(color=_DARK_FG, size=12)

    return fig


def _format_wind_hover(model, vt, fh, speed_kt, dir_deg, gust_kt) -> str:
    """Hover tooltip for wind points."""
    vt_str = pd.to_datetime(vt).strftime("%Y-%m-%d %HZ")
    speed_str = f"{speed_kt:.0f} kt" if pd.notna(speed_kt) else "—"
    if pd.notna(dir_deg):
        cardinal = _deg_to_cardinal(dir_deg)
        dir_str = f"{int(dir_deg):03d}° ({cardinal})"
    else:
        dir_str = "—"
    gust_str = f"<br>Gust: {gust_kt:.0f} kt" if pd.notna(gust_kt) else ""
    return (
        f"<b>{model}</b><br>"
        f"{vt_str}  (f+{int(fh)})<br>"
        f"Wind: {dir_str} @ {speed_str}{gust_str}"
    )


def _deg_to_cardinal(deg: float) -> str:
    """Convert degrees to 16-point cardinal direction."""
    directions = [
        "N", "NNE", "NE", "ENE",
        "E", "ESE", "SE", "SSE",
        "S", "SSW", "SW", "WSW",
        "W", "WNW", "NW", "NNW",
    ]
    idx = int((deg + 11.25) // 22.5) % 16
    return directions[idx]
