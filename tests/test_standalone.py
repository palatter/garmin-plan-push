"""The standalone Windows download and its winget manifests."""

import importlib.util
from pathlib import Path

import pytest

import gpp

ROOT = Path(__file__).resolve().parents[1]
DIGEST = "ab" * 32


def _fill():
    spec = importlib.util.spec_from_file_location("fill", ROOT / "packaging/winget/fill.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_standalone_build_is_reinstalled_with_winget():
    assert gpp._reinstall(True) == f"winget install --force {gpp.WINGET_ID}"
    assert gpp._reinstall(False).startswith("uv tool install --reinstall ")


def test_the_manifests_are_for_this_package_and_version(tmp_path):
    fill = _fill()
    assert fill.PACKAGE == gpp.WINGET_ID
    written = fill.fill(gpp.__version__, DIGEST.upper(), tmp_path)
    assert sorted(p.name for p in written) == [
        f"{gpp.WINGET_ID}.installer.yaml",
        f"{gpp.WINGET_ID}.locale.en-US.yaml",
        f"{gpp.WINGET_ID}.yaml",
    ]
    for path in written:
        text = path.read_text(encoding="utf-8")
        assert f"PackageIdentifier: {gpp.WINGET_ID}\n" in text
        assert fill.PLACEHOLDER not in text
    installer = (tmp_path / f"{gpp.WINGET_ID}.installer.yaml").read_text(encoding="utf-8")
    assert f"InstallerSha256: {DIGEST.upper()}\n" in installer
    assert f"/v{gpp.__version__}/{fill.zip_name(gpp.__version__)}\n" in installer


def test_the_manifests_refuse_another_version(tmp_path):
    with pytest.raises(ValueError, match=r"not at version 9\.9\.9"):
        _fill().fill("9.9.9", DIGEST, tmp_path)


def test_the_digest_must_be_for_this_zip():
    fill = _fill()
    ver = gpp.__version__
    assert fill.parse_sha256(f"{DIGEST}  gpp-{ver}-windows.zip\n", ver) == DIGEST.upper()
    for wrong in (f"{DIGEST}  gpp-0.0.1-windows.zip", f"{DIGEST[:-1]}  gpp-{ver}-windows.zip", ""):
        with pytest.raises(ValueError, match="not a SHA-256 line"):
            fill.parse_sha256(wrong, ver)


def test_doctor_on_the_standalone_build_needs_no_uv(tmp_path, monkeypatch, capsys):
    from gpp import cli
    from gpp.profile import Profile

    monkeypatch.setattr(cli, "FROZEN", True)
    monkeypatch.setattr("shutil.which", lambda *_: None)
    profile = Profile.from_dict({"pace": {"threshold": "4:30/km"}}).save(tmp_path / "p.toml")
    cli.main(["--profile", str(profile), "doctor"])
    out = capsys.readouterr().out
    assert "not needed: this is the standalone build" in out
    assert "winget puts gpp on PATH" in out
    assert "curl_cffi" in out
