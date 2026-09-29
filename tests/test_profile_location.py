"""Where a new profile is saved, and that `gpp backup` carries the one in use.

A first-run save used to write profile.toml into whatever folder the
terminal was in, so `gpp web` started from anywhere else showed setup
again, and `gpp backup`, which zips ~/.config/gpp, left the profile out.
"""

import zipfile
from pathlib import Path

import pytest

from gpp import backup, cli
from gpp.profile import Profile, find_profile
from gpp.web.server import App


@pytest.fixture
def places(tmp_path, monkeypatch):
    terminal = tmp_path / "terminal"
    terminal.mkdir()
    config = tmp_path / "home" / ".config" / "gpp" / "profile.toml"
    home = tmp_path / "home" / "profile.toml"
    monkeypatch.chdir(terminal)
    monkeypatch.setattr("gpp.profile.CONFIG_PROFILE", config)
    monkeypatch.setattr("gpp.profile.DEFAULT_PROFILE_PATHS", (Path("profile.toml"), config, home))
    return terminal, config, home


def test_first_run_setup_saves_to_the_config_folder(places, tmp_path, monkeypatch):
    terminal, config, _ = places
    saved = App(None).save_profile({"name": "Pat", "threshold": "4:30/km"})
    assert saved["saved"] == str(config)
    assert config.exists() and not (terminal / "profile.toml").exists()
    # Started again from another folder, the app finds it.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert App(None).state({})["profile"]["path"] == str(config)


def test_a_profile_an_older_gpp_saved_in_the_home_folder_is_still_found(places):
    _, _, home = places
    home.parent.mkdir(parents=True)
    Profile.from_dict({"name": "Old", "pace": {"threshold": "5:00/km"}}).save(home)
    assert find_profile() == home


def test_backup_carries_the_profile_in_use_and_restore_puts_it_in_the_config_folder(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    (config / "profile.toml").write_text("name = 'not in use'\n", encoding="utf-8")
    in_use = tmp_path / "terminal" / "profile.toml"
    in_use.parent.mkdir()
    in_use.write_text("name = 'in use'\n", encoding="utf-8")
    archive = backup.backup(tmp_path / "b.zip", config_dir=config, profile=in_use)
    with zipfile.ZipFile(archive) as z:
        assert z.namelist() == ["config/profile.toml"]
        assert z.read("config/profile.toml") == b"name = 'in use'\n"
    restored = tmp_path / "restored"
    backup.restore(archive, config_dir=restored)
    assert (restored / "profile.toml").read_text(encoding="utf-8") == "name = 'in use'\n"
    # A profile already inside the config folder goes in once, as it is.
    archive = backup.backup(tmp_path / "c.zip", config_dir=config, profile=config / "profile.toml")
    with zipfile.ZipFile(archive) as z:
        assert z.namelist() == ["config/profile.toml"]
        assert z.read("config/profile.toml") == b"name = 'not in use'\n"


def test_gpp_backup_includes_the_profile_it_is_given(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("gpp.backup.CONFIG_DIR", tmp_path / "empty-config")
    profile = Profile.from_dict({"name": "Pat", "pace": {"threshold": "4:30/km"}})
    path = profile.save(tmp_path / "profile.toml")
    target = tmp_path / "backup.zip"
    assert cli.main(["--profile", str(path), "backup", "-o", str(target)]) == 0
    with zipfile.ZipFile(target) as z:
        assert z.namelist() == ["config/profile.toml"]
