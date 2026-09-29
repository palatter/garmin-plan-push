"""Build the standalone Windows download: gpp with its own Python, zipped.

    uv run --locked --with pyinstaller==6.22.3 python packaging/windows/build.py

The release workflow runs it on Windows for each tag and attaches
dist/gpp-<version>-windows.zip and its .sha256 to the GitHub Release; the
winget manifests in packaging/winget point at that zip. It builds on macOS
and Linux too, which is how it is tested, but only the Windows zip is
published.

A folder rather than one self-extracting file: it starts faster, and
antivirus tools flag unpacking executables more often. Then it runs the
build's own `gpp` on a few commands that touch every library it carries,
so a module or data file left out fails here, not on someone's computer.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
BUILD = ROOT / "build" / "pyinstaller"

# Libraries whose data files or plug-ins PyInstaller does not find by itself.
COLLECT_DATA = [
    "gpp",  # the web app's pages
    "ua_generator",  # the browser identities garminconnect signs in with
    "jsonschema_specifications",  # the schemas jsonschema validates against
]
COLLECT_ALL = ["curl_cffi"]  # its compiled half, which Garmin's sign-in uses
COLLECT_SUBMODULES = ["keyring.backends"]  # found by entry point, not import
COPY_METADATA = ["garmin-plan-push", "garminconnect", "keyring", "anthropic", "openai"]


def version() -> str:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]


def build() -> Path:
    import PyInstaller.__main__

    args = [
        str(ROOT / "packaging" / "windows" / "entry.py"),
        "--name=gpp",
        "--onedir",
        "--console",
        "--noconfirm",
        "--clean",
        f"--distpath={DIST}",
        f"--workpath={BUILD}",
        f"--specpath={BUILD}",
        "--exclude-module=mcp",
    ]
    args += [f"--collect-data={name}" for name in COLLECT_DATA]
    args += [f"--collect-all={name}" for name in COLLECT_ALL]
    args += [f"--collect-submodules={name}" for name in COLLECT_SUBMODULES]
    args += [f"--copy-metadata={name}" for name in COPY_METADATA]
    PyInstaller.__main__.run(args)
    return DIST / "gpp"


def smoke(folder: Path) -> None:
    """The build's own gpp, on commands that load every library it carries."""
    exe = folder / ("gpp.exe" if os.name == "nt" else "gpp")
    plan = ROOT / "examples" / "week.json"
    with tempfile.TemporaryDirectory() as home:
        env = {**os.environ, "HOME": home, "USERPROFILE": home, "NO_COLOR": "1"}
        for name in ("GARMIN_EMAIL", "GARMIN_PASSWORD"):
            env.pop(name, None)
        profile = Path(home) / "profile.toml"
        profile.write_text('[pace]\nthreshold = "4:30/km"\n[hr]\nlthr = 170\n', encoding="utf-8")

        def run(*argv: str, expect: int | None = 0) -> str:
            done = subprocess.run(
                [str(exe), *argv],
                input="",  # never a terminal, so nothing waits on a prompt
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=120,
            )
            out = done.stdout + done.stderr
            if expect is not None and done.returncode != expect:
                sys.exit(f"`gpp {' '.join(argv)}` exited {done.returncode}:\n{out}")
            print(f"ok  gpp {' '.join(argv)}")
            return out

        assert version() in run("--version")
        run("schema")
        run("--profile", str(profile), "check", str(plan))
        run("--profile", str(profile), "compile", str(plan))
        assert "Goblet Squat" in run("exercises", "goblet")
        run("--profile", str(profile), "keys")
        doctor = run("--profile", str(profile), "doctor", expect=None)  # gpp is not on PATH here
        for needed in ("python-garminconnect", "curl_cffi", "keychain"):
            line = next((x for x in doctor.splitlines() if needed in x), "")
            if not line.strip().startswith(("ok", "--")) or "missing" in line:
                sys.exit(f"doctor: {line or needed + ' not reported'}")
        # A live dry run with no saved login, no password and no terminal to
        # ask for one: garminconnect, curl_cffi and the keychain all load, and
        # it stops at the sign-in, before the network. A dry run writes
        # nothing even if it got further.
        out = run(
            "--profile", str(profile), "push", str(plan), "--dry-run", "--live",
            "--email", "smoke@example.com", expect=2,
        )  # fmt: skip
        if "no saved Garmin sign-in yet; there is no terminal" not in out:
            sys.exit(f"push did not reach the sign-in:\n{out}")
        print("ok  gpp push stopped at the sign-in, as it should")


def package(folder: Path) -> Path:
    system = {"Windows": "windows", "Darwin": "macos"}.get(platform.system(), "linux")
    target = DIST / f"gpp-{version()}-{system}.zip"
    # gpp.exe at the top of the zip, where the winget manifest names it.
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            archive.write(path, path.relative_to(folder))
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_name(target.name + ".sha256").write_text(f"{digest}  {target.name}\n")
    print(f"{target.name}  {target.stat().st_size / 1e6:.1f} MB  sha256 {digest}")
    return target


if __name__ == "__main__":
    built = build()
    smoke(built)
    package(built)
