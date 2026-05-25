"""Comparison and visualization."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd

from core.stations import StationResolver, Station
from models.base import ModelSource


def run_comparison(sources, cycle, stations):
    frames = []
    for src in sources:
        df = src.get_forecast(cycle, stations)
        if len(df) > 0:
            frames.append(df)
    if not frames:
        from core.schema import empty_df
        return empty_df()
    return pd.concat(frames, ignore_index=True)


def compare_icaos(icaos, cycle, cache_root, model_classes=None, hrrr_fhours=range(0, 19)):
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
    sources = []
    for cls in model_classes:
        if cls is Hrrr:
            sources.append(Hrrr(cache_dir=cache_root / "hrrr",
                                stations=resolved, fhours=hrrr_fhours))
        else:
            sources.append(cls(cache_dir=cache_root / cls.name.lower()))
    df = run_comparison(sources, cycle, resolved_icaos)
    return df, resolved, unresolved


def pivot_for_plot(df, station_id, variable):
    sub = df[df["station_id"] == station_id]
    if len(sub) == 0:
        return pd.DataFrame()
    return (sub.pivot_table(index="valid_time", columns="model",
                            values=variable, aggfunc="first").sort_index())


def plot_comparison(df, station_id, ax=None):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(11, 7), sharex=True)
    vis = pivot_for_plot(df, station_id, "vsby_sm")
    cig = pivot_for_plot(df, station_id, "ceiling_ft")
    if not vis.empty:
        vis.plot(ax=axes[0], marker="o")
        axes[0].set_ylabel("Visibility (sm)")
        axes[0].set_title(f"{station_id} — Visibility forecast comparison")
        axes[0].grid(True, alpha=0.3); axes[0].legend(title="Model")
    if not cig.empty:
        cig.plot(ax=axes[1], marker="o")
        axes[1].set_ylabel("Ceiling (ft AGL)")
        axes[1].set_title(f"{station_id} — Ceiling forecast comparison")
        axes[1].grid(True, alpha=0.3); axes[1].legend(title="Model")
    axes[1].set_xlabel("Valid time (UTC)")
    fig.tight_layout()
    return fig
