"""Cycle selection.

Each ModelSource declares a `is_cycle_complete(cycle)` method that does cheap
HTTP HEAD checks against NOMADS to determine whether a given run is fully
posted. `find_latest_complete()` walks backward through candidate cycles
from "now" until it finds one where every source reports complete, or gives
up after max_lookback_hours.

This is intentionally conservative: a cycle is "complete" only if ALL models
have published everything we'd want from it. That can mean returning a cycle
4–6 hours behind real time, which is the right default for forecast
verification work where consistency matters more than freshness.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from models.base import ModelSource


# How many candidate cycles to probe before giving up. With cycles every
# 6 hours for MOS and the constraint that we need a cycle covered by every
# source, 8 cycles = 48 hours of lookback is plenty in practice.
DEFAULT_LOOKBACK_CYCLES = 8


def find_latest_complete(
    sources: Iterable[ModelSource],
    now: Optional[datetime] = None,
    candidate_cycle_hours: tuple[int, ...] = (0, 6, 12, 18),
    max_lookback_cycles: int = DEFAULT_LOOKBACK_CYCLES,
    verbose: bool = True,
) -> Optional[datetime]:
    """Walk backward from `now` through candidate cycles. Return the most
    recent one where every source's is_cycle_complete() returns True.

    candidate_cycle_hours defaults to MOS's cycle times (00/06/12/18). HRRR
    runs hourly but we constrain to these so we only return cycles where
    MOS is even possible. If you later swap out MOS for an hourly model,
    pass `tuple(range(24))`.
    """
    sources = list(sources)
    if not sources:
        return None
    if now is None:
        now = datetime.now(timezone.utc)
    now = now.replace(minute=0, second=0, microsecond=0)

    # Enumerate candidate cycles in descending order (newest first), starting
    # from the most recent cycle hour that's already passed.
    candidates = _candidate_cycles(now, candidate_cycle_hours, max_lookback_cycles)

    for cycle in candidates:
        ok_for_all = True
        for src in sources:
            try:
                complete = src.is_cycle_complete(cycle)
            except NotImplementedError:
                # Source didn't implement the check — assume complete and
                # trust its fetch() to handle missing data gracefully.
                complete = True
            except Exception as e:
                if verbose:
                    print(f"  [{src.name}] probe error for {cycle:%Y-%m-%d %HZ}: {e}")
                complete = False
            if verbose:
                status = "✓" if complete else "✗"
                print(f"  {status} {src.name} @ {cycle:%Y-%m-%d %HZ}")
            if not complete:
                ok_for_all = False
                break
        if ok_for_all:
            if verbose:
                print(f"→ Latest complete cycle: {cycle:%Y-%m-%d %HZ}")
            return cycle

    if verbose:
        print(f"⚠ No complete cycle found within {max_lookback_cycles} probes.")
    return None


def _candidate_cycles(
    now: datetime,
    hours: tuple[int, ...],
    max_count: int,
) -> list[datetime]:
    """Generate cycle datetimes at the given UTC hours, newest first,
    starting from the most recent one at or before `now`."""
    candidates: list[datetime] = []
    # Walk back hour by hour from `now`, pick cycles whose hour is in `hours`.
    cursor = now
    while len(candidates) < max_count:
        if cursor.hour in hours:
            candidates.append(cursor)
        cursor = cursor - timedelta(hours=1)
        # Safety: bail after 7 days to avoid pathological infinite loop.
        if cursor < now - timedelta(days=7):
            break
    return candidates
