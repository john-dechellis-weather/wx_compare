"""One lock shared by every heavy warmer in the process.

The MRMS decode peaks near 700 MB and a Level II parse a few hundred
more. Each warmer checks RSS before it starts, but two that start
within the same second both pass that check and then both allocate
— the check cannot see an allocation that has not happened yet. This
lock is what actually keeps them from overlapping.

WHO HOLDS IT IS RECORDED. `held_by()` answers "why is my warmer
waiting" from a diagnostics page; `holding(name)` is the context
manager every warmer should use so the record stays true.

Deliberately tiny and dependency-free so any warmer can import it
without pulling in the others.
"""

import contextlib
import threading
import time

heavy = threading.Lock()
_info = {"holder": None, "since": None}


def held_by():
    """(holder, seconds_held) or (None, 0)."""
    h, s = _info["holder"], _info["since"]
    return (h, time.time() - s) if h else (None, 0.0)


@contextlib.contextmanager
def holding(name: str, timeout: float = None):
    """Acquire the heavy lock as `name`. With a timeout, yields False
    instead of blocking forever when it cannot be had."""
    got = heavy.acquire(timeout=timeout) if timeout else heavy.acquire()
    if not got:
        yield False
        return
    _info["holder"], _info["since"] = name, time.time()
    try:
        yield True
    finally:
        _info["holder"], _info["since"] = None, None
        heavy.release()
