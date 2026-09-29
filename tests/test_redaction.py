"""Credentials never reach the debug log or the diagnostics bundle."""

from __future__ import annotations

import json
import logging

from gpp import cli


def test_redact_argv_hides_every_credential_form():
    argv = ["icu", "plan.json", "--key", "abc123secret", "--password=hunter22", "--athlete", "i1"]
    assert cli.redact_argv(argv) == [
        "icu",
        "plan.json",
        "--key",
        "***",
        "--password=***",
        "--athlete",
        "i1",
    ]


def test_scrub_removes_old_unredacted_lines_and_known_values():
    line = "gpp 0.2.0: icu plan.json --key abc123secret --athlete i1 token=zzzzzzzz9"
    out = cli.scrub(line, ["zzzzzzzz9"])
    assert "abc123secret" not in out and "zzzzzzzz9" not in out
    assert "--key ***" in out


def test_verbose_log_never_holds_the_key(tmp_path, monkeypatch):
    log = tmp_path / "gpp.log"
    monkeypatch.setattr(cli, "LOG_PATH", log)
    try:
        cli.main(["--verbose", "icu", str(tmp_path / "missing.json"), "--key", "abc123secret"])
    except Exception:  # noqa: S110 - the command failing is fine; the log is the point
        pass
    finally:
        gpp_log = logging.getLogger("gpp")
        for handler in list(gpp_log.handlers):
            handler.close()
            gpp_log.removeHandler(handler)
    text = log.read_text(encoding="utf-8")
    assert "--key ***" in text and "abc123secret" not in text


def test_verbose_leaves_third_party_loggers_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "gpp.log")
    root_level = logging.getLogger().level
    try:
        cli.main(["--verbose", "zones"])
    except (Exception, SystemExit):  # noqa: S110
        pass
    finally:
        gpp_log = logging.getLogger("gpp")
        for handler in list(gpp_log.handlers):
            handler.close()
            gpp_log.removeHandler(handler)
    assert logging.getLogger().level == root_level
    assert logging.getLogger("urllib3").getEffectiveLevel() > logging.DEBUG


def test_bundle_scrubs_secrets_from_the_log_tail(tmp_path, monkeypatch):
    log = tmp_path / "gpp.log"
    log.write_text(
        "old line: icu plan.json --key abc123secret\nprovider said sk-ant-verysecret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli, "LOG_PATH", log)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-verysecret")
    from gpp.profile import Profile
    from gpp.providers import load_providers

    profile = Profile.from_dict({"pace": {"threshold": "4:30/km"}})
    configs, _ = load_providers(profile.raw)
    out = tmp_path / "bundle.json"
    cli.write_bundle(out, profile, configs)
    tail = "\n".join(json.loads(out.read_text(encoding="utf-8"))["log_tail"])
    assert "abc123secret" not in tail and "sk-ant-verysecret" not in tail
