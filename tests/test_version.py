"""Every place that states the version agrees, so a release cannot ship a
mismatch (0.2.0 went out with packaging still pointing at 0.1.0)."""

import re
import tomllib
from pathlib import Path

import gpp

ROOT = Path(__file__).resolve().parents[1]


def test_the_version_is_the_same_everywhere():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert gpp.__version__ == version
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    # The newest numbered section; an "## [Unreleased]" one may sit above it.
    first = re.search(r"^## \[(\d[^\]]*)\]", changelog, re.M)
    assert first and first.group(1) == version
    assert f"Garmin Plan Push {version}<" in (ROOT / "docs" / "index.html").read_text(
        encoding="utf-8"
    )
    text = (ROOT / "packaging/Formula/garmin-plan-push.rb").read_text(encoding="utf-8")
    assert f"v{version}" in text and not re.search(r"v0\.\d+\.\d+", text.replace(f"v{version}", ""))
    manifests = sorted((ROOT / "packaging/winget").glob("*.yaml"))
    assert len(manifests) == 3
    for manifest in manifests:
        text = manifest.read_text(encoding="utf-8")
        assert f"PackageVersion: {version}\n" in text, manifest.name
        # Any other 0.x version in it (a download or notes link) is stale.
        assert not re.search(r"\b0\.\d+\.\d+", text.replace(version, "")), manifest.name
