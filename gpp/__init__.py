"""garmin-plan-push: AI-written running plans -> Garmin structured workouts."""

import sys

__version__ = "0.2.2"

# The Windows download (and so winget) is gpp frozen with its own Python:
# no uv, and a different way to reinstall.
FROZEN = bool(getattr(sys, "frozen", False))
WINGET_ID = "palatter.GarminPlanPush"


def _reinstall(frozen: bool) -> str:
    """What to tell someone whose install is missing a library: the command
    they installed with, reinstalling. Never `uv sync`, which only means
    something inside a clone of the repository."""
    if frozen:
        return f"winget install --force {WINGET_ID}"
    return "uv tool install --reinstall git+https://github.com/palatter/garmin-plan-push"


REINSTALL = _reinstall(FROZEN)
