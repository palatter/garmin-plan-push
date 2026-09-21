"""`gpp watch`: re-run a command when a plan file changes.

No dependency: it polls the file's mtime. Edit the plan in any editor, save,
and the checks (or the push) run again. Push is the obvious use; check is
the safer default, so that is what it does unless told otherwise.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path


def watch(
    path: str | Path, on_change: Callable[[Path], None], interval: float = 1.0, once: bool = False
) -> None:
    """Call `on_change(path)` now, then every time the file's mtime changes."""
    target = Path(path)
    last = None
    while True:
        try:
            stamp = target.stat().st_mtime_ns
        except FileNotFoundError:
            stamp = None
        if stamp != last:
            last = stamp
            if stamp is not None:
                on_change(target)
        if once:
            return
        time.sleep(interval)
