"""garmin-plan-push: AI-written running plans -> Garmin structured workouts."""

__version__ = "0.2.1"

# What to tell someone whose install is missing a library: the same command
# the guide installs with, plus --reinstall. Not `uv sync`, which only means
# something inside a clone of the repository.
REINSTALL = "uv tool install --reinstall git+https://github.com/palatter/garmin-plan-push"
