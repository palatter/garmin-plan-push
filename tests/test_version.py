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
    # uv.lock records the project's own version: a bump without `uv lock`
    # passes everything above and then fails every `--locked` install.
    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    own = [p["version"] for p in lock["package"] if p["name"] == "garmin-plan-push"]
    assert own == [version], "run `uv lock` after changing the version"


WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def _jobs() -> dict[str, str]:
    """The release workflow's jobs by name, as text (no YAML parser in the lock)."""
    body = WORKFLOW.read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([a-z][a-z-]*):\n", body, flags=re.M)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_the_release_workflow_installs_only_from_the_committed_lock():
    """Without --locked the first `uv run` rewrites a stale lock on the runner
    and the release goes out; the Windows build, which is locked, then fails."""
    text = WORKFLOW.read_text(encoding="utf-8")
    commands = [line.strip() for line in text.splitlines() if re.search(r"\buv (run|sync)\b", line)]
    commands = [line for line in commands if not line.startswith("#")]
    assert len(commands) >= 4
    for line in commands:
        for call in re.findall(r"uv (?:run|sync)\b[^&|;)]*", line):
            assert "--locked" in call, line


def test_nothing_is_published_before_the_windows_build_has_succeeded():
    jobs = _jobs()
    assert set(jobs) == {"release", "windows", "go-public", "publish"}
    # Until the last job, the release is a draft: created as one, and only
    # the job that waits for every build makes it public.
    creates = [name for name, job in jobs.items() if "gh release create" in job]
    assert creates == ["release"] and "--draft" in jobs["release"]
    public = [name for name, job in jobs.items() if "--draft=false" in job]
    assert public == ["go-public"]
    for name in ("go-public", "publish"):  # the release page, and PyPI
        assert "needs: [release, windows]" in jobs[name], name
    assert "-windows.zip" in jobs["go-public"]  # it looks for the zip first
