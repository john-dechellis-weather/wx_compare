"""Comparison and visualization.

This is the join point. Every model produces rows in the same schema
(see core/schema.py), so comparison is just concat + groupby.
"""
from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pandas as pd

from models.base import ModelSource


def run_comparison(
    sources: list[ModelSource],
    cycle: datetime,
    stations: Iterable[str],
) -> pd.DataFrame:
    """Fetch + parse for one cycle across all sources. Returns a long-form
    DataFrame ready for pivoting or plotting."""
    frames = []
    for src in sources:
        df = src.get_forecast(cycle, stations)
        if len(df) > 0:
            frames.append(df)
    if not frames:
        from core.schema import empty_df
        return empty_df()
    return pd.concat(frames, ignore_index=True)


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


def plot_comparison(df: pd.DataFrame, station_id: str, ax=None):
    """Plot vsby and ceiling side-by-side for one station, one cycle.

    Requires matplotlib. Kept import-local so the rest of the library
    doesn't pull it in.
    """
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)

    vis = pivot_for_plot(df, station_id, "vsby_sm")
    cig = pivot_for_plot(df, station_id, "ceiling_ft")

    if not vis.empty:
        vis.plot(ax=axes[0], marker="o")
        axes[0].set_ylabel("Visibility (sm)")
        axes[0].set_title(f"{station_id} — Visibility forecast comparison")
        axes[0].grid(True, alpha=0.3)
        axes[0].legend(title="Model")

    if not cig.empty:
        cig.plot(ax=axes[1], marker="o")
        axes[1].set_ylabel("Ceiling (ft AGL)")
        axes[1].set_title(f"{station_id} — Ceiling forecast comparison")
        axes[1].grid(True, alpha=0.3)
        axes[1].legend(title="Model")

    axes[1].set_xlabel("Valid time (UTC)")
    fig.tight_layout()
    return fig
