"""Comparison and visualization.

This is the join point. Every model produces rows in the same schema
(see core/schema.py), so comparison is just concat + groupby.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional, Sequence

import pandas as pd

from core.stations import StationResolver, Station
from models.base import ModelSource


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
