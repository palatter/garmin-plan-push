"""The winget manifests for a release, ready to submit: the three templates
beside this file with the Windows zip's SHA-256 put in, written to
dist/winget/<version>/.

    python packaging/winget/fill.py             # the .sha256 the build left in dist/
    python packaging/winget/fill.py --release   # the .sha256 on the GitHub release

Then, on Windows, `winget validate dist\\winget\\<version>` and submit that
folder to microsoft/winget-pkgs (packaging/README.md says how).
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PACKAGE = "palatter.GarminPlanPush"
PLACEHOLDER = "ZIP_SHA256"
RELEASES = "https://github.com/palatter/garmin-plan-push/releases/download"


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def zip_name(ver: str) -> str:
    return f"gpp-{ver}-windows.zip"


def parse_sha256(text: str, ver: str) -> str:
    """The digest from a `<sha256>  <file>` line, checked to be for this zip."""
    match = re.fullmatch(r"([0-9a-fA-F]{64})\s+\*?(\S+)\s*", text)
    if not match or match.group(2) != zip_name(ver):
        raise ValueError(f"not a SHA-256 line for {zip_name(ver)}: {text.strip()[:120]!r}")
    return match.group(1).upper()


def fill(ver: str, sha256: str, out: Path) -> list[Path]:
    """Write the manifests for `ver` into `out`; the paths written."""
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for template in sorted(HERE.glob(f"{PACKAGE}*.yaml")):
        text = template.read_text(encoding="utf-8")
        if f"PackageVersion: {ver}\n" not in text:
            raise ValueError(f"{template.name} is not at version {ver}")
        target = out / template.name
        target.write_text(text.replace(PLACEHOLDER, sha256), encoding="utf-8")
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--release", action="store_true", help="read the .sha256 from the GitHub release"
    )
    args = parser.parse_args(argv)
    ver = version()
    name = zip_name(ver) + ".sha256"
    if args.release:
        with urllib.request.urlopen(f"{RELEASES}/v{ver}/{name}", timeout=30) as response:  # noqa: S310
            text = response.read().decode("utf-8")
    else:
        local = ROOT / "dist" / name
        if not local.exists():
            sys.exit(f"{local} is missing: build first, or use --release")
        text = local.read_text(encoding="utf-8")
    out = ROOT / "dist" / "winget" / ver
    for path in fill(ver, parse_sha256(text, ver), out):
        print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
