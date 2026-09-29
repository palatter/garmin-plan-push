"""Everything to run before pushing, in place of CI.

    uv run python scripts/check.py

Installs what the gate needs from the lock (every extra: the type check
reads the mcp module), then lint, format, type check, the tests with a
coverage floor, and a vulnerability audit of the locked dependencies.
Stops at the first failure. A uv.lock that no longer matches
pyproject.toml fails here instead of being quietly rewritten.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_FLOOR = 75
# Pinned like ty, as media-courier pins it: a new release of the auditor
# should not fail a push by itself. Raise it by hand.
PIP_AUDIT = "pip-audit==2.10.1"


def run(title: str, *command: str) -> None:
    print(f"\n== {title}: {' '.join(command)}", flush=True)
    if subprocess.run(command, cwd=ROOT, check=False).returncode != 0:
        sys.exit(f"\n{title} failed")


def audit() -> None:
    """pip-audit over the exported lock (all extras), without installing anything."""
    exported = subprocess.run(
        ["uv", "export", "--all-extras", "--no-hashes", "--no-emit-project", "-q"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as handle:
        handle.write(exported)
    try:
        run("audit", "uvx", PIP_AUDIT, "-r", handle.name, "--disable-pip", "--no-deps")
    finally:
        Path(handle.name).unlink()


def main() -> None:
    run("sync", "uv", "sync", "--all-extras", "--locked")
    run("lint", "uv", "run", "--locked", "ruff", "check", ".")
    run("format", "uv", "run", "--locked", "ruff", "format", "--check", ".")
    run("types", "uv", "run", "--locked", "ty", "check")
    run(
        "tests",
        "uv", "run", "--locked", "pytest", "-q", "--cov=gpp", "--cov-report=term:skip-covered",
        f"--cov-fail-under={COVERAGE_FLOOR}",
    )  # fmt: skip
    if "--no-audit" not in sys.argv:
        audit()
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
