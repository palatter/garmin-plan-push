"""Everything to run before pushing, in place of CI.

    uv run python scripts/check.py

Lint, format, type check, the tests with a coverage floor, and a
vulnerability audit of the locked dependencies. Stops at the first failure.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COVERAGE_FLOOR = 75


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
        run("audit", "uvx", "pip-audit", "-r", handle.name, "--disable-pip", "--no-deps")
    finally:
        Path(handle.name).unlink()


def main() -> None:
    run("lint", "uv", "run", "ruff", "check", ".")
    run("format", "uv", "run", "ruff", "format", "--check", ".")
    run("types", "uv", "run", "ty", "check")
    run(
        "tests",
        "uv", "run", "pytest", "-q", "--cov=gpp", "--cov-report=term:skip-covered",
        f"--cov-fail-under={COVERAGE_FLOOR}",
    )  # fmt: skip
    if "--no-audit" not in sys.argv:
        audit()
    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
