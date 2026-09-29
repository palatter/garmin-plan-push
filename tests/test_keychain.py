"""The OS keychain: keys by their environment variable's name, Garmin
passwords by account, and a missing or broken keychain read as empty."""

import keyring
import pytest
from keyring.backends import fail

from gpp import keychain


def test_a_key_is_read_from_the_environment_first_then_the_keychain(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert keychain.lookup("ANTHROPIC_API_KEY") is None
    assert keychain.source("ANTHROPIC_API_KEY") is None
    keychain.put("ANTHROPIC_API_KEY", "sk-from-keychain")
    assert keychain.lookup("ANTHROPIC_API_KEY") == "sk-from-keychain"
    assert keychain.source("ANTHROPIC_API_KEY") == "keychain"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    assert keychain.lookup("ANTHROPIC_API_KEY") == "sk-from-env"
    assert keychain.source("ANTHROPIC_API_KEY") == "environment"


def test_entries_are_filed_under_the_apps_own_service(keychain_backend):
    keychain.put("ICU_API_KEY", "abc")
    assert keychain_backend.entries == {("garmin-plan-push", "ICU_API_KEY"): "abc"}
    assert keychain.forget("ICU_API_KEY") is True
    assert keychain.forget("ICU_API_KEY") is False
    assert keychain_backend.entries == {}


def test_no_keychain_reads_as_empty_and_saving_says_so(monkeypatch):
    keyring.set_keyring(fail.Keyring())
    assert keychain.available() is False
    assert keychain.get("ANTHROPIC_API_KEY") is None
    assert keychain.forget("ANTHROPIC_API_KEY") is False
    with pytest.raises(keychain.KeychainError, match="no keychain"):
        keychain.put("ANTHROPIC_API_KEY", "x")


def test_a_keychain_that_fails_reads_as_empty(keychain_backend, monkeypatch):
    def locked(*_):
        raise RuntimeError("the keychain is locked")

    monkeypatch.setattr(keychain_backend, "get_password", locked)
    monkeypatch.setattr(keychain_backend, "set_password", locked)
    assert keychain.get("ANTHROPIC_API_KEY") is None
    with pytest.raises(keychain.KeychainError, match="locked"):
        keychain.put("ANTHROPIC_API_KEY", "x")


def test_garmin_passwords_are_kept_per_account_and_all_forgotten_at_once(keychain_backend):
    keychain.save_garmin_password(" Me@Example.com ", "one")
    keychain.save_garmin_password("you@example.com", "two")
    keychain.save_garmin_password("me@example.com", "three")  # a new password replaces the old
    assert keychain.garmin_password("ME@example.com") == "three"
    assert keychain.garmin_password("you@example.com") == "two"
    assert keychain.garmin_password("") is None
    assert keychain.forget_garmin_passwords() == 2
    assert keychain_backend.entries == {}


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ICU_API_KEY", "MY_KEY_2"])
def test_key_names_are_environment_variable_names(name):
    assert keychain.check_key_name(name) == name


@pytest.mark.parametrize("name", ["", "anthropic", "garmin:me@example.com", "A B", "1KEY"])
def test_anything_else_is_not_a_key_name(name):
    with pytest.raises(keychain.KeychainError, match="not a key name"):
        keychain.check_key_name(name)


# --- where keys are read -------------------------------------------------------


def test_a_provider_uses_a_key_saved_in_the_keychain(monkeypatch):
    from gpp.providers import ProviderConfig, _api_key, key_names, key_present, load_providers

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config = ProviderConfig(name="gemini", kind="gemini")
    assert key_present(config) is False
    keychain.put("GEMINI_API_KEY", "g-key")
    assert key_present(config) is True
    assert _api_key(config, "OPENAI_API_KEY") == "g-key"
    configs, _ = load_providers({})
    assert key_names(configs) == ["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY"]


def test_intervals_icu_reads_the_saved_key_and_the_profiles_athlete(monkeypatch):
    from gpp import icu
    from gpp.profile import Profile

    monkeypatch.delenv(icu.KEY_ENV, raising=False)
    monkeypatch.delenv(icu.ATHLETE_ENV, raising=False)
    profile = Profile.from_dict(
        {"pace": {"threshold": "4:30/km"}, "intervals_icu": {"athlete": "i777"}}
    )
    assert icu.api_key() is None
    keychain.put(icu.KEY_ENV, "icu-key")
    assert icu.api_key() == "icu-key"
    assert icu.api_key("typed") == "typed"
    assert icu.athlete_id(None, profile) == "i777"
    assert icu.athlete_id(None, None) is None
    monkeypatch.setenv(icu.ATHLETE_ENV, "i888")
    assert icu.athlete_id(None, profile) == "i888"
    assert icu.athlete_id(" i999 ", profile) == "i999"


def _keys(*argv, stdin=""):
    import io
    import sys

    from gpp import cli

    old = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        return cli.main(["keys", *argv])
    finally:
        sys.stdin = old


def test_gpp_keys_saves_lists_and_clears(keychain_backend, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr("gpp.cli_extra._profile", _no_profile)
    assert _keys("set", "ANTHROPIC_API_KEY", stdin="sk-typed\n") == 0
    assert keychain_backend.entries[("garmin-plan-push", "ANTHROPIC_API_KEY")] == "sk-typed"
    assert _keys() == 0
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out and "ICU_API_KEY" in out
    assert "sk-typed" not in out
    listed = {line.split()[0]: line for line in out.splitlines() if line.startswith("  ")}
    assert "not set" not in listed["ANTHROPIC_API_KEY"]
    assert "not set" in listed["OPENAI_API_KEY"]
    assert _keys("clear", "ANTHROPIC_API_KEY") == 0
    assert keychain_backend.entries == {}


def test_gpp_keys_refuses_a_bad_name_or_an_empty_key(capsys):
    assert _keys("set", "not a name", stdin="x\n") == 2
    assert "not a key name" in capsys.readouterr().err
    assert _keys("set", "ANTHROPIC_API_KEY", stdin="\n") == 2
    assert _keys("set") == 2
    assert "say which key" in capsys.readouterr().err


def test_gpp_keys_says_when_the_environment_wins(monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert _keys("set", "ANTHROPIC_API_KEY", stdin="sk-typed\n") == 0
    assert "uses that one first" in capsys.readouterr().out


def test_the_diagnostics_bundle_scrubs_keys_saved_in_the_keychain(tmp_path, monkeypatch):
    import json

    from gpp import cli
    from gpp.profile import Profile
    from gpp.providers import load_providers

    log = tmp_path / "gpp.log"
    log.write_text("provider said sk-ant-fromkeychain\nicu said icukey123456\n", encoding="utf-8")
    monkeypatch.setattr(cli, "LOG_PATH", log)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    keychain.put("ANTHROPIC_API_KEY", "sk-ant-fromkeychain")
    keychain.put("ICU_API_KEY", "icukey123456")
    profile = Profile.from_dict({"pace": {"threshold": "4:30/km"}})
    configs, _ = load_providers(profile.raw)
    out = tmp_path / "bundle.json"
    cli.write_bundle(out, profile, configs)
    tail = "\n".join(json.loads(out.read_text(encoding="utf-8"))["log_tail"])
    assert "fromkeychain" not in tail and "icukey123456" not in tail


def test_doctor_names_the_keychain(tmp_path, capsys):
    from gpp import cli
    from gpp.profile import Profile

    profile = Profile.from_dict({"pace": {"threshold": "4:30/km"}}).save(tmp_path / "p.toml")
    cli.main(["--profile", str(profile), "doctor"])
    (line,) = [x for x in capsys.readouterr().out.splitlines() if x[6:].startswith("keychain ")]
    assert keychain.where() in line


def _no_profile(args):
    from gpp.profile import ProfileError

    raise ProfileError("no profile yet")
