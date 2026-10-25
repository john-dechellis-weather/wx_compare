"""Site counters shown on the login board (24 Oct): page views,
successful and failed sign-ins. Kept in mac/site_stats.json beside the
(gitignored) .env so they survive restarts and never land in git;
STATS_FILE overrides the path. A view is one browser session reaching
the login board (not every rerun)."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

_DEFAULT = Path(__file__).resolve().parent.parent / "mac" / "site_stats.json"
PATH = Path(os.environ.get("STATS_FILE", str(_DEFAULT)))
_lock = threading.Lock()
KEYS = ("views", "logins_ok", "logins_fail")


def read() -> dict:
    try:
        d = json.loads(PATH.read_text())
    except Exception:
        d = {}
    return {k: int(d.get(k, 0)) for k in KEYS}


def bump(key: str, n: int = 1) -> dict:
    if key not in KEYS:
        return read()
    with _lock:
        d = read()
        d[key] += n
        try:
            PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(d))
            os.replace(tmp, PATH)
        except Exception:
            pass
    return d


def line() -> str:
    d = read()
    return (f"{d['views']:,} views · {d['logins_ok']:,} sign-ins · "
            f"{d['logins_fail']:,} failed")
