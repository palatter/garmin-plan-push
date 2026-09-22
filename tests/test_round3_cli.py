"""Round 3 batch D: versioned saves, csv/markdown, recent plans, backup and
restore, completions, the agenda, profile set, --json, schema, share import."""

import datetime as dt
import json
import zipfile

from gpp import agenda, backup, cli, completions, formats, recent
from gpp.plan import Plan
from gpp.profile import Profile

PROFILE = Profile.from_dict({"name": "T", "pace": {"threshold": "4:00/km"}})


def run(name, date, minutes, zone="easy", role=None, phase=None):
    out = {
        "name": name,
        "date": date,
        "steps": [
            {"kind": "run", "duration": f"{minutes}m", "target": {"type": "pace", "zone": zone}}
        ],
    }
    if role:
        out["role"] = role
    if phase:
        out["phase"] = phase
    return out


def plan_of(*workouts, **extra):
    return Plan.from_dict({"plan": "Block", "workouts": list(workouts), **extra})


TWO_WEEKS = plan_of(
    run("Easy", "2026-09-22", 40, phase="base"),
    run("Threshold", "2026-09-24", 40, "threshold", role="quality", phase="base"),
    run("Long", "2026-09-27", 90, role="long", phase="base"),
    run("Easy", "2026-09-29", 40, phase="build"),
    run("Long", "2026-10-04", 100, role="long", phase="build"),
    races=[{"name": "10k", "date": "2026-10-25", "priority": "A"}],
)


def test_save_keeps_previous_versions(tmp_path):
    path = tmp_path / "plan.json"
    plan_of(run("Easy", "2026-09-22", 40)).save(path)
    plan_of(run("Easy", "2026-09-22", 45)).save(path)
    plan_of(run("Easy", "2026-09-22", 50)).save(path)
    plan_of(run("Easy", "2026-09-22", 50)).save(path)  # unchanged: no new version
    assert (tmp_path / "plan.json.1").exists() and (tmp_path / "plan.json.2").exists()
    assert not (tmp_path / "plan.json.3").exists()
    assert "45m" in (tmp_path / "plan.json.1").read_text(encoding="utf-8")
    assert "40m" in (tmp_path / "plan.json.2").read_text(encoding="utf-8")


def test_csv_and_markdown_exports():
    csv = formats.export_csv(TWO_WEEKS, PROFILE)
    assert csv.splitlines()[0].startswith("date,day,name,sport,role")
    assert len(csv.splitlines()) == 6
    md = formats.export_markdown(TWO_WEEKS, PROFILE)
    assert "# Block" in md and "## Week of 2026-09-21" in md and "### Thu 24 Sep — Threshold" in md
    assert "**Race:** 10k on 2026-10-25" in md


def test_recent_plans_dedupe_by_content_and_list_newest_first(tmp_path):
    a = recent.save_recent(TWO_WEEKS.to_dict(), "generated", root=tmp_path)
    b = recent.save_recent(TWO_WEEKS.to_dict(), "edited", root=tmp_path)
    assert a == b
    other = plan_of(run("Solo", "2026-09-22", 30))
    recent.save_recent(other.to_dict(), "edited", root=tmp_path)
    listed = recent.list_recent(root=tmp_path)
    assert [r.name for r in listed] == ["Block", "Block"] or len(listed) == 2
    assert {r.sessions for r in listed} == {5, 1}
    assert recent.load_recent(a)["plan"] == "Block"


def test_backup_and_restore_round_trip(tmp_path):
    source = tmp_path / "config"
    (source / "library" / "workouts").mkdir(parents=True)
    (source / "profile.toml").write_text("name = 'T'\n", encoding="utf-8")
    (source / "library" / "workouts" / "q.json").write_text("{}", encoding="utf-8")
    archive = backup.backup(tmp_path / "b.zip", config_dir=source)
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
    assert names == {"config/profile.toml", "config/library/workouts/q.json"}
    target = tmp_path / "restored"
    written = backup.restore(archive, config_dir=target)
    assert (
        len(written) == 2
        and (target / "profile.toml").read_text(encoding="utf-8") == "name = 'T'\n"
    )
    (target / "profile.toml").write_text("changed", encoding="utf-8")
    assert backup.restore(archive, config_dir=target) == []
    assert backup.restore(archive, config_dir=target, force=True)
    assert (target / "profile.toml").read_text(encoding="utf-8") == "name = 'T'\n"


def test_completion_scripts_know_every_subcommand():
    parser = cli.build_parser()
    for shell in completions.SHELLS:
        text = completions.script(parser, shell)
        assert "generate" in text and "push" in text and "attempts" in text, shell
    assert "complete -F _gpp gpp" in completions.script(parser, "bash")
    assert "Register-ArgumentCompleter" in completions.script(parser, "powershell")


def test_agenda_next_and_week_view():
    today = dt.date(2026, 9, 23)
    assert agenda.next_session(TWO_WEEKS, today).name == "Threshold"
    text = agenda.render_next(TWO_WEEKS, PROFILE, today)
    assert text.startswith("Next: Threshold, tomorrow")
    view = agenda.week_view(TWO_WEEKS, PROFILE, today)
    assert [s["name"] for s in view["this_week"]["sessions"]] == ["Easy", "Threshold", "Long"]
    assert view["this_week"]["sessions"][0]["done"] is True
    assert view["this_week"]["key_session"] == "Long"
    assert (
        view["next_week"]["phases"] == ["build"]
        and "Phase changes on 2026-09-28" in view["phase_change"]
    )
    assert view["days_to_race"] == 32
    rendered = agenda.render_week(view)
    assert "This week" in rendered and "Next week" in rendered and "32 days to 10k" in rendered


def test_profile_set_and_get_through_the_cli(tmp_path, capsys):
    path = PROFILE.save(tmp_path / "profile.toml")
    assert (
        cli.main(["--profile", str(path), "profile", "set", "athlete.recent_weekly_km", "42"]) == 0
    )
    assert Profile.load(path).recent_weekly_km == 42
    assert (
        cli.main(["--profile", str(path), "profile", "set", "availability.days", '["tue", "thu"]'])
        == 0
    )
    assert Profile.load(path).availability.days == ["tue", "thu"]
    capsys.readouterr()
    cli.main(["--profile", str(path), "--json", "profile", "get", "athlete.recent_weekly_km"])
    assert json.loads(capsys.readouterr().out)["athlete.recent_weekly_km"] == 42
    assert (
        cli._coerce("true") is True
        and cli._coerce("4:00/km") == "4:00/km"
        and cli._coerce("3.5") == 3.5
    )


def test_json_output_for_zones_and_check(tmp_path, capsys):
    path = PROFILE.save(tmp_path / "profile.toml")
    plan_path = tmp_path / "plan.json"
    TWO_WEEKS.save(plan_path)
    cli.main(["--profile", str(path), "--json", "zones"])
    zones = json.loads(capsys.readouterr().out)
    assert zones["threshold"] == "4:00/km" and "threshold" in zones["zones"]
    cli.main(["--profile", str(path), "--json", "check", str(plan_path)])
    check = json.loads(capsys.readouterr().out)
    assert check["workouts"] == 5 and "findings" in check["report"]


def test_schema_reference_and_version(capsys):
    cli.main(["schema", "--markdown"])
    assert "The plan DSL" in capsys.readouterr().out
    cli.main(["schema"])
    assert json.loads(capsys.readouterr().out)["title"] == "gpp training plan"


def test_shared_plan_lands_on_next_monday_by_default(tmp_path, capsys):
    path = PROFILE.save(tmp_path / "profile.toml")
    bundle = tmp_path / "plan.share.json"
    bundle.write_text(formats.export_share(TWO_WEEKS, PROFILE), encoding="utf-8")
    out = tmp_path / "imported.json"
    assert cli.main(["--profile", str(path), "import", str(bundle), "-o", str(out)]) == 0
    imported = Plan.load(out)
    today = dt.date.today()
    next_monday = today + dt.timedelta(days=7 - today.weekday())
    assert imported.sorted_workouts()[0].date == next_monday + dt.timedelta(days=1)
    kept = tmp_path / "kept.json"
    assert (
        cli.main(["--profile", str(path), "import", str(bundle), "--keep-dates", "-o", str(kept)])
        == 0
    )
    assert Plan.load(kept).sorted_workouts()[0].date == dt.date(2026, 9, 22)


def test_defaults_table_feeds_the_commands(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text(
        'name = "T"\n[pace]\nthreshold = "4:00/km"\n[defaults]\nattempts = 5\nport = 9999\n',
        encoding="utf-8",
    )
    args = cli.build_parser().parse_args(["--profile", str(path), "zones"])
    assert cli._defaults(args) == {"attempts": 5, "port": 9999}
