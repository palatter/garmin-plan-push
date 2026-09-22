"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from pathlib import Path

from . import checks
from .compile import CompileError, compile_plan
from .generate import dump_plan, generate_plan
from .plan import Plan, PlanError
from .profile import Profile, ProfileError, default_save_path, find_profile
from .prompt import build_prompt
from .providers import (
    ProviderError,
    build_provider,
    is_manual,
    key_present,
    load_providers,
    pick_default,
    probe,
    resolve,
)
from .render import render_plan


def _load_profile(explicit: str | None) -> Profile:
    """Resolve the profile.

    Deliberately delegates to profile.find_profile() rather than keeping its
    own search order: when the CLI and the web UI disagree about which file is
    in effect, they silently compile workouts at different paces.
    """
    if explicit:
        return Profile.load(explicit)
    found = find_profile()
    if found is not None:
        return Profile.load(found)
    raise ProfileError(
        "no profile yet.\n"
        "  Run  gpp web    for the graphical setup, or\n"
        "  run  gpp init   to set it up here in the terminal."
    )


def _load(args: argparse.Namespace) -> tuple[Profile, Plan, list]:
    profile = _load_profile(args.profile)
    plan = Plan.load(args.plan)
    return profile, plan, compile_plan(plan, profile)


# --- commands ---------------------------------------------------------------


def cmd_web(args: argparse.Namespace) -> int:
    from .web import serve

    port = args.port if args.port is not None else int(_defaults(args).get("port", 8765))
    serve(
        port=port,
        open_browser=not args.no_browser,
        profile_path=Path(args.profile) if args.profile else None,
    )
    return 0


LOG_PATH = Path.home() / ".config" / "gpp" / "logs" / "gpp.log"


def _version() -> str:
    from importlib import metadata

    try:
        return metadata.version("garmin-plan-push")
    except metadata.PackageNotFoundError:
        return "dev"


def _defaults(args: argparse.Namespace) -> dict:
    """The optional [defaults] table of the profile (#176), or nothing."""
    try:
        return dict(_load_profile(args.profile).raw.get("defaults") or {})
    except (ProfileError, FileNotFoundError):
        return {}


def _emit(args: argparse.Namespace, payload: dict, text: str) -> None:
    """JSON when --json was given, the human text otherwise (#174)."""
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, default=str))
    else:
        print(text)


def cmd_init(args: argparse.Namespace) -> int:
    """Terminal setup, for people who would rather not open a browser."""
    from .estimate import COMMON_RACES, EstimateError, lthr_from_max, threshold_from_race
    from .units import format_pace

    print("\nLet's work out your training paces.\n")

    name = input("Your name [runner]: ").strip() or "runner"
    units = input("Miles or kilometres? [km]: ").strip().lower()
    imperial = units.startswith("mi") or units.startswith("im")

    print(
        "\nEnter a recent race and we'll derive your threshold pace,\n"
        "or press Enter to type the pace directly.\n"
        f"Known distances: {', '.join(COMMON_RACES)}\n"
    )

    threshold: str | None = None
    while threshold is None:
        distance = input("Race distance (e.g. 10k) [skip]: ").strip()
        if not distance:
            break
        time = input("Your finishing time (e.g. 47:30): ").strip()
        try:
            estimate = threshold_from_race(distance, time)
        except EstimateError as exc:
            print(f"  {exc}\n")
            continue
        threshold = format_pace(estimate.threshold_pace, imperial)
        print(f"\n  {estimate.describe(imperial)}\n")

    while threshold is None:
        raw = input("Threshold pace (e.g. 4:30/km): ").strip()
        if not raw:
            print("  A threshold pace is needed to derive your zones.")
            continue
        threshold = raw

    lthr = hr_max = None
    hr_raw = input("Max heart rate, if you know it [skip]: ").strip()
    if hr_raw.isdigit():
        hr_max = int(hr_raw)
        try:
            lthr = lthr_from_max(hr_max)
            print(f"  Estimated threshold HR {lthr} bpm.")
        except EstimateError as exc:
            print(f"  {exc}")
            hr_max = None

    data: dict = {
        "name": name,
        "units": "imperial" if imperial else "metric",
        "pace": {"threshold": threshold},
    }
    if lthr or hr_max:
        data["hr"] = {k: v for k, v in (("lthr", lthr), ("max", hr_max)) if v}

    try:
        profile = Profile.from_dict(data)
    except ProfileError as exc:
        print(f"\nerror: {exc}", file=sys.stderr)
        return 2

    path = Path(args.profile) if args.profile else (find_profile() or default_save_path())
    profile.save(path)

    print(f"\nSaved to {path}\n")
    print(profile.describe())
    print('\nNext:  gpp generate "four weeks to a 10k"   (or: gpp web)')
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    from .mcp_server import describe_tools, serve

    if args.list:
        for tool in describe_tools():
            print(f"  {tool['name']:<20} {tool['description']}")
        return 0
    serve()
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .watch import watch

    def on_change(path: Path) -> None:
        print(f"\n--- {path} changed ---")
        try:
            profile = _load_profile(args.profile)
            plan = Plan.load(path)
            compiled = compile_plan(plan, profile)
            print(render_plan(plan, compiled, profile), end="")
            print(checks.check(plan, profile).text())
            if args.push:
                from .client import GarminClient, PushError

                email = args.email or os.environ.get("GARMIN_EMAIL")
                password = os.environ.get("GARMIN_PASSWORD")
                if not email:
                    print(
                        "set GARMIN_EMAIL (and GARMIN_PASSWORD or a cached login) to push from watch"
                    )
                    return
                client = GarminClient(email, password or None)
                try:
                    client.connect(prompt_mfa=lambda: input("Garmin MFA code: ").strip())
                    client.push(compiled, device_id=args.device, log=print)
                except PushError as exc:
                    print(f"push failed: {exc}")
        except (PlanError, ProfileError, CompileError) as exc:
            print(f"error: {exc}")

    print(f"watching {args.plan} -- Ctrl+C to stop")
    try:
        watch(args.plan, on_change)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    import datetime as dt

    from .enrich import enrich

    profile = _load_profile(args.profile)
    plan = Plan.load(args.plan)
    heat = dt.date.fromisoformat(args.heat) if args.heat else None
    result = enrich(
        plan,
        profile,
        fuelling=not args.no_fuelling,
        heat_race=heat,
        strength_per_week=args.strength,
        durability=not args.no_durability,
        cadence=not args.no_cadence,
    )
    for reason in result.reasons:
        print(f"  {reason}")
    out = result.plan.save(args.output or args.plan)
    print(f"written to {out}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    import json

    from .evaluate import evaluate, load_cases, table, to_dict

    profile = _load_profile(args.profile)
    configs, default = load_providers(profile.raw)
    chosen = args.provider or pick_default(configs, default)
    if chosen not in configs:
        raise ProviderError(
            f"unknown provider {chosen!r}; configured: " + ", ".join(sorted(configs))
        )
    cases = load_cases(args.cases)
    results = evaluate(
        lambda: build_provider(configs[chosen]), cases, attempts=args.attempts, log=print
    )
    if args.json:
        print(json.dumps(to_dict(results), indent=2))
    else:
        print()
        print(table(results))
    if args.output:
        Path(args.output).write_text(
            json.dumps(to_dict(results), indent=2) + "\n", encoding="utf-8"
        )
        print(f"results written to {args.output}")
    return 0 if all(r.ok for r in results) else 1


def _bench(args: argparse.Namespace, configs: dict, default: str | None) -> int:
    from .evaluate import BENCH_CASES, evaluate, table, verdict

    chosen = args.provider or pick_default(configs, default)
    if chosen not in configs:
        raise ProviderError(
            f"unknown provider {chosen!r}; configured: " + ", ".join(sorted(configs))
        )
    print(f"benching {chosen} on {len(BENCH_CASES)} short plans (costs a few thousand tokens)...")
    results = evaluate(lambda: build_provider(configs[chosen]), BENCH_CASES, attempts=3, log=print)
    print()
    print(table(results))
    print(f"verdict for {chosen}: {verdict(results)}")
    return 0


def cmd_recover(args: argparse.Namespace) -> int:
    import datetime as dt

    from .library import post_race_recovery

    profile = _load_profile(args.profile)
    plan = post_race_recovery(dt.date.fromisoformat(args.race), args.distance)
    compiled = compile_plan(plan, profile)
    print(render_plan(plan, compiled, profile), end="")
    if args.output:
        print(f"written to {plan.save(args.output)}")
    return 0


def cmd_pushes(args: argparse.Namespace) -> int:
    from .receipts import list_receipts

    receipts = list_receipts()
    if not receipts:
        print("no push receipts yet; they are written by `gpp push` and the web app")
        return 0
    for receipt in receipts:
        print(f"  {receipt.describe()}")
        print(f"      {receipt.path}")
    print("\nremove exactly one push's workouts with: gpp unpush --receipt <file>")
    return 0


def cmd_fit(args: argparse.Namespace) -> int:
    from .fit import encode_workout

    profile = _load_profile(args.profile)
    plan = Plan.load(args.plan)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    for workout in plan.sorted_workouts():
        slug = (
            "".join(c if c.isalnum() else "-" for c in workout.name.lower()).strip("-") or "workout"
        )
        path = out / f"{workout.date.isoformat()}-{slug}.fit"
        path.write_bytes(encode_workout(workout, profile))
        print(f"  {path}")
    print(
        f"{len(plan.workouts)} file(s). Copy them to the watch's GARMIN/NewFiles folder over USB "
        "(older models: GARMIN/Workouts); they appear under Training > Workouts."
    )
    return 0


def cmd_race_plan(args: argparse.Namespace) -> int:
    from .race import RaceError, race_workout, splits, table

    profile = _load_profile(args.profile)
    try:
        rows = splits(args.distance, args.time, args.strategy, profile.imperial)
    except RaceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{args.distance} in {args.time}, {args.strategy} pacing:\n")
    print(table(rows, profile.imperial))
    print(
        "\nEven pacing is the fastest strategy at every level in the largest split analysis "
        "(PLOS One 2025); most recreational runners go out too fast and positive-split."
    )
    if args.workout:
        goal = {
            "name": args.name,
            "date": args.workout,
            "distance": args.distance,
            "goal_time": args.time,
        }
        workout = race_workout(goal, profile, args.strategy)
        if args.output and Path(args.output).exists():
            plan = Plan.load(args.output)
            plan.workouts = [w for w in plan.workouts if w.date != workout.date or w.role != "race"]
            plan.workouts.append(workout)
        else:
            plan = Plan(plan=f"Race day: {args.name}", workouts=[workout])
        target = args.output or f"race-{args.workout}.json"
        plan.save(target)
        print(f"\nrace session written to {target}")
    return 0


def _plan_arg(args: argparse.Namespace) -> Plan:
    """The plan file given, or the most recent plan this app touched."""
    from .recent import list_recent, load_recent

    if getattr(args, "plan", None):
        return Plan.load(args.plan)
    recent = list_recent()
    if not recent:
        raise PlanError("no plan given and no recent plans; pass a plan file")
    return Plan.from_dict(load_recent(recent[0].path))


def cmd_next(args: argparse.Namespace) -> int:
    from .agenda import next_session, render_next

    profile = _load_profile(args.profile)
    plan = _plan_arg(args)
    workout = next_session(plan)
    _emit(
        args,
        {"next": workout.to_dict() if workout else None},
        render_next(plan, profile),
    )
    return 0


def cmd_week(args: argparse.Namespace) -> int:
    from .agenda import render_week, week_view

    profile = _load_profile(args.profile)
    view = week_view(_plan_arg(args), profile)
    _emit(args, view, render_week(view))
    return 0


def cmd_plans(args: argparse.Namespace) -> int:
    from .recent import list_recent

    recent = list_recent()
    if not recent:
        _emit(args, {"plans": []}, "no recent plans yet; generate, open or save one first")
        return 0
    lines = []
    for r in recent:
        span = f"{r.first} to {r.last}" if r.first else "no dates"
        lines.append(
            f"  {r.saved[:16].replace('T', ' ')}  {r.name:<32} {r.sessions:>3} sessions  {span}  [{r.label}]"
        )
        lines.append(f"      {r.path}")
    _emit(args, {"plans": [r.to_dict() for r in recent]}, "\n".join(lines))
    return 0


def cmd_schema(args: argparse.Namespace) -> int:
    from .plan import EXECUTABLE_KINDS, PHASES, PLAN_SCHEMA, ROLES, SPORTS

    if not args.markdown:
        print(json.dumps(PLAN_SCHEMA, indent=2))
        return 0
    lines = [
        "# The plan DSL",
        "",
        "A plan is `{plan, summary?, race_date?, races?, weeks?, workouts}`. A workout is",
        "`{name, date, sport?, role?, phase?, notes?, steps}`. Every step is one of:",
        "",
        f"- an executable step: `kind` in {', '.join(EXECUTABLE_KINDS)}, with exactly one of",
        '  `duration` ("15m", "90s", "1:05:00"), `distance` ("1km", "800m", "3mi") or `until: "lap"`',
        "  (`count` for an exercise), an optional `target`, an optional `note` (212 characters, shown on",
        "  the watch), and for hills a `grade` in percent;",
        "- a repeat: `{kind: repeat, reps, steps}`, nested at most two deep.",
        "",
        "Targets: `{type: pace, zone}` (recovery, easy, steady, marathon, threshold, interval,",
        "repetition, or Daniels' E/M/T/I/R), `{type: pace, slow, fast}` (slow is the slower pace),",
        "`{type: hr, zone 1-5}` or `{type: hr, low, high}`, `{type: cadence, low, high}`,",
        "`{type: power, zone 1-7}` or `{type: power, low, high}`, `{type: rpe, value 1-10}`, `{type: none}`.",
        "",
        f"Sports: {', '.join(SPORTS)}. Roles: {', '.join(ROLES)}. Phases: {', '.join(PHASES)}.",
        "",
        "`gpp schema` prints the JSON Schema itself; `gpp check plan.json` validates a file.",
    ]
    print("\n".join(lines))
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    from copy import deepcopy

    from .profile import default_save_path, find_profile

    path = Path(args.profile) if args.profile else (find_profile() or default_save_path())
    raw = deepcopy(Profile.load(path).raw) if Path(path).exists() else {}
    keys = args.key.split(".")
    if args.action == "get":
        node = raw
        for key in keys:
            node = node.get(key) if isinstance(node, dict) else None
        _emit(args, {args.key: node}, json.dumps(node) if node is not None else "(not set)")
        return 0
    if args.value is None:
        print("error: set needs a value", file=sys.stderr)
        return 2
    node = raw
    for key in keys[:-1]:
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            print(f"error: {key} is not a table", file=sys.stderr)
            return 2
    node[keys[-1]] = _coerce(args.value)
    Profile.from_dict(raw).save(path)
    print(f"{args.key} = {json.dumps(node[keys[-1]])}  ({path})")
    return 0


def _coerce(value: str):
    lowered = value.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    if lowered.startswith(("[", "{")):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            continue
    return value


def cmd_completions(args: argparse.Namespace) -> int:
    from .completions import script

    print(script(build_parser(), args.shell), end="")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    from .backup import backup

    target = backup(Path(args.output) if args.output else None, with_tokens=args.with_tokens)
    print(
        f"backup written to {target}"
        + (
            ""
            if args.with_tokens
            else " (Garmin login tokens left out; --with-tokens includes them)"
        )
    )
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    from .backup import restore

    written = restore(Path(args.archive), force=args.force)
    for path in written:
        print(f"  {path}")
    print(
        f"{len(written)} file(s) restored"
        + ("" if args.force else "; existing files were kept (use --force to overwrite)")
    )
    return 0


def cmd_zones(args: argparse.Namespace) -> int:
    from .units import format_pace

    profile = _load_profile(args.profile)
    payload = {
        "name": profile.name,
        "threshold": format_pace(profile.threshold_pace, profile.imperial),
        "zone_model": profile.zone_model,
        "zones": {
            name: {
                "slow": format_pace(slow, profile.imperial),
                "fast": format_pace(fast, profile.imperial),
            }
            for name, (slow, fast) in profile.zone_table().items()
        },
        "lthr": profile.lthr,
        "hr_zones": {str(z): list(profile.hr_zone(z)) for z in sorted(profile.hr_zones)}
        if profile.lthr
        else {},
    }
    _emit(args, payload, profile.describe())
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    print(build_prompt(_load_profile(args.profile)))
    return 0


def _key_status(config) -> str:
    present = key_present(config)
    if present is None:
        return "no key needed"
    return f"{config.api_key_env} set" if present else f"{config.api_key_env} NOT set"


def cmd_providers(args: argparse.Namespace) -> int:
    profile = _load_profile(args.profile)
    configs, default = load_providers(profile.raw)
    if getattr(args, "bench", None):
        return _bench(args, configs, default)
    chosen = pick_default(configs, default)
    for name, config in configs.items():
        resolve(config)
        marker = "*" if name == chosen else " "
        target = config.model or "(any model)"
        if config.base_url:
            target += f" @ {config.base_url}"
        print(f" {marker} {name:<12} {config.kind:<18} {target:<48} {_key_status(config)}")
    print("\n* = will be used by default. Configure more under [ai.providers] in your profile.")
    from .providers import LOCAL_MODEL_PRESETS

    print("\nlocal models (kind = ollama), from experience:")
    for preset in LOCAL_MODEL_PRESETS:
        print(
            f"  {preset['model']:<14} {preset['size']:<5} {preset['verdict']:<7} {preset['note']}"
        )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Answer the support questions before they are asked."""
    ok = True

    def line(good: bool | None, label: str, detail: str = "") -> None:
        nonlocal ok
        mark = {True: "ok ", False: "!! ", None: "-- "}[good]
        if good is False:
            ok = False
        print(f"  {mark} {label:<26} {detail}")

    print("\nProfile")
    try:
        profile = _load_profile(args.profile)
    except ProfileError as exc:
        line(False, "profile", str(exc).splitlines()[0])
        print("\nRun  gpp init  or  gpp web  to create one.")
        return 1
    from .units import format_pace

    line(True, "loaded", str(args.profile or find_profile()))
    line(True, "threshold pace", format_pace(profile.threshold_pace, profile.imperial))
    line(
        True if profile.lthr else None,
        "heart-rate zones",
        f"LTHR {profile.lthr} bpm" if profile.lthr else "not set (pace targets only)",
    )

    print("\nAI providers")
    configs, default = load_providers(profile.raw)
    chosen = pick_default(configs, default)
    for name, config in configs.items():
        resolve(config)
        present = key_present(config)
        label = f"{name} ({config.kind})" + ("  <- default" if name == chosen else "")
        if is_manual(config):
            line(True, label, "paste: always works, no key")
            continue
        if present is False:
            # Only the provider that will actually be used is a failure;
            # the others are simply not in play.
            if name == chosen:
                line(False, label, f"{config.api_key_env} not set - set it, or use paste")
            else:
                line(None, label, f"{config.api_key_env} not set (won't be used)")
            continue
        if not args.ping:
            line(True, label, f"{_key_status(config)}; model {config.model}")
            continue
        try:
            reply = probe(config)
        except ProviderError as exc:
            line(False, label, str(exc))
        else:
            line(True, label, f"replied {reply[:20]!r} with model {config.model}")

    print("\nEnvironment")
    import platform
    import shutil

    line(True, "python", f"{platform.python_version()} on {platform.system()} {platform.release()}")
    line(bool(shutil.which("uv")), "uv", shutil.which("uv") or "not on PATH")
    shim = shutil.which("gpp")
    line(
        bool(shim),
        "gpp on PATH",
        shim or "not found - on Windows, `uv tool update-shell` adds the tools folder to PATH",
    )

    print("\nGarmin")
    try:
        from importlib import metadata as _metadata

        line(True, "python-garminconnect", f"installed ({_metadata.version('garminconnect')})")
    except Exception:
        line(False, "python-garminconnect", "missing - run: uv sync")
    if args.ping:
        import socket

        try:
            socket.create_connection(("connect.garmin.com", 443), timeout=5).close()
            line(True, "connect.garmin.com", "reachable")
        except OSError as exc:
            line(False, "connect.garmin.com", f"unreachable: {exc}")
    token_dir = Path.home() / ".garminconnect"
    line(
        True if token_dir.exists() else None,
        "saved login",
        "found - push won't ask for a password"
        if token_dir.exists()
        else "none yet - first push will ask for your Garmin password",
    )
    age = _token_age_days(token_dir)
    if age is not None:
        stale = age > 300
        line(
            not stale,
            "login age",
            f"{age} day(s) old"
            + (
                " - Garmin expires tokens without warning; if push fails to log in, delete the folder and sign in again"
                if stale
                else ""
            ),
        )
    if args.ping and os.environ.get("GARMIN_EMAIL"):
        from .client import GarminClient, PushError

        try:
            garmin = GarminClient(
                os.environ["GARMIN_EMAIL"], os.environ.get("GARMIN_PASSWORD") or None
            )
            garmin.connect()
            line(True, "Garmin login", f"ok via {garmin.transport}")
        except PushError as exc:
            line(False, "Garmin login", str(exc))
    line(
        None,
        "watch support",
        "needs structured workouts: Fenix 6+, Epix, FR 255/265/955/965, Edge 530+",
    )

    print()
    print("Everything looks ready." if ok else "Fix the lines marked !! and run again.")
    if getattr(args, "bundle", None):
        write_bundle(Path(args.bundle), profile, configs)
    return 0 if ok else 1


def _log_tail(lines: int = 200) -> list[str]:
    """The end of the debug log (written by --verbose), for a bug report."""
    try:
        return LOG_PATH.read_text(encoding="utf-8").splitlines()[-lines:]
    except OSError:
        return []


def _token_age_days(token_dir: Path) -> int | None:
    """Days since the newest file in the token cache, or None without one."""
    try:
        newest = max((f.stat().st_mtime for f in token_dir.iterdir() if f.is_file()), default=None)
    except OSError:
        return None
    if newest is None:
        return None
    import time

    return int((time.time() - newest) // 86400)


def write_bundle(path: Path, profile: Profile, configs: dict) -> None:
    """A local diagnostics file for bug reports (#100). Never uploaded."""
    import json
    import platform
    import sys as _sys
    from importlib import metadata

    def version(name: str) -> str:
        try:
            return metadata.version(name)
        except metadata.PackageNotFoundError:
            return "not installed"

    bundle = {
        "gpp": version("garmin-plan-push"),
        "python": _sys.version.split()[0],
        "platform": platform.platform(),
        "packages": {
            n: version(n) for n in ("garminconnect", "jsonschema", "anthropic", "openai", "mcp")
        },
        "profile": {
            "zone_model": profile.zone_model,
            "has_lthr": profile.lthr is not None,
            "has_power": profile.power_cp is not None,
            "availability": bool(profile.availability),
            "goal_race": bool(profile.goal_race),
        },
        "providers": {
            name: {"kind": cfg.kind, "model": cfg.model, "key_env": cfg.api_key_env}
            for name, cfg in configs.items()
        },
        "log_tail": _log_tail(),
    }
    path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    print(f"diagnostics written to {path} (no credentials, no plan contents)")


def cmd_generate(args: argparse.Namespace) -> int:
    profile = _load_profile(args.profile)
    configs, default = load_providers(profile.raw)

    chosen = args.provider or pick_default(configs, default)
    if chosen not in configs:
        raise ProviderError(
            f"unknown provider {chosen!r}; configured: " + ", ".join(sorted(configs))
        )
    provider = build_provider(configs[chosen])

    request = args.request
    if not request:
        print("Describe the plan you want (end with a blank line):")
        lines: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            lines.append(line)
        request = "\n".join(lines).strip()
    if not request:
        print("error: no request given", file=sys.stderr)
        return 2

    previous = Plan.load(args.continue_from) if getattr(args, "continue_from", None) else None
    attempts = (
        args.attempts if args.attempts is not None else int(_defaults(args).get("attempts", 3))
    )
    result = generate_plan(
        provider,
        profile,
        request,
        attempts=attempts,
        log=lambda m: print(m),
        previous=previous,
        chunk_weeks=getattr(args, "chunk_weeks", None),
    )

    text = dump_plan(result.data)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"\nwrote {args.output}")
    else:
        print()
        print(text, end="")

    if args.show:
        print()
        print(render_plan(result.plan, compile_plan(result.plan, profile), profile), end="")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    import datetime as dt

    profile, plan, compiled = _load(args)
    report = checks.check(plan, profile, today=dt.date.today())
    _emit(
        args,
        {"plan": plan.plan, "workouts": len(compiled), "ok": report.ok, "report": report.to_dict()},
        f"OK: {plan.plan} - {len(compiled)} workout(s) valid\n{report.text()}",
    )
    return 0 if report.ok else 1


def cmd_show(args: argparse.Namespace) -> int:
    profile, plan, compiled = _load(args)
    print(render_plan(plan, compiled, profile), end="")
    print()
    print(checks.check(plan, profile).text())
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    _, _, compiled = _load(args)
    payload = [{"date": item.date, "workout": item.payload} for item in compiled]
    text = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(text)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    from .client import GarminClient, PushError

    profile, plan, compiled = _load(args)

    print(render_plan(plan, compiled, profile), end="")
    print()

    if args.dry_run and not args.live:
        print("Dry run: nothing sent to Garmin.")
        return 0

    if args.dry_run and args.live:
        email = (
            args.email or os.environ.get("GARMIN_EMAIL") or input("Garmin Connect email: ").strip()
        )
        password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass(
            "Garmin Connect password (not stored): "
        )
        client = GarminClient(email, password or None, token_dir=args.token_dir)
        try:
            client.connect(prompt_mfa=lambda: input("Garmin MFA code: ").strip())
        except PushError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        print(f"connected via {client.transport}; reading the calendar, writing nothing\n")
        for r in client.preview(compiled):
            print(f"  {r.action:<13} {r.date}  {r.name}  {r.detail}".rstrip())
        for other in client.conflicts(compiled):
            print(
                f"  also there    {other['date']}  {other['title']}  ({other['source']}, left alone)"
            )
        return 0

    if not args.yes:
        answer = input(f"Push {len(compiled)} workout(s) to Garmin Connect? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1

    email = args.email or os.environ.get("GARMIN_EMAIL")
    if not email:
        email = input("Garmin Connect email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD")
    if not password:
        password = getpass.getpass("Garmin Connect password (not stored): ")

    client = GarminClient(email, password, token_dir=args.token_dir)
    try:
        client.connect(prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    except PushError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"connected via {client.transport}")
    for other in client.conflicts(compiled):
        print(f"  also on {other['date']}: {other['title']} ({other['source']}) -- left alone")

    results = client.push(
        compiled,
        replace=args.replace,
        verify=not args.no_verify,
        device_id=args.device or _defaults(args).get("device"),
        log=lambda line: print(line),
    )
    from .receipts import save_receipt

    try:
        print(f"receipt: {save_receipt(plan.plan, results)}")
    except OSError as exc:
        print(f"warning: could not save the push receipt: {exc}")

    failures = [r for r in results if r.action == "failed"]
    warnings = [r for r in results if r.detail and r.action != "failed"]
    print()
    for result in warnings:
        print(f"warning: {result.date} {result.name}: {result.detail}")
    for result in failures:
        print(f"failed:  {result.date} {result.name}: {result.detail}", file=sys.stderr)

    created = sum(1 for r in results if r.action in ("created", "replaced", "updated"))
    print(
        f"{created} pushed, "
        f"{sum(1 for r in results if r.action == 'unchanged')} unchanged, "
        f"{len(failures)} failed"
    )
    if not failures:
        print("Sync your watch (or wait for the overnight sync) to pull them down.")
    return 1 if failures else 0


# --- wiring -----------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gpp",
        description="Compile AI-generated running plans into Garmin workouts.",
    )
    parser.add_argument("--profile", help="path to profile.toml")
    parser.add_argument("--version", action="version", version=f"gpp {_version()}")
    parser.add_argument(
        "--json",
        dest="json",
        action="store_true",
        help="machine-readable output where a command supports it",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help=f"write a debug log to {LOG_PATH} (request metadata, never secrets)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    from .cli_extra import register
    from .cli_history import register as register_history

    register(sub)
    register_history(sub)

    web = sub.add_parser("web", help="open the graphical app in your browser")
    web.add_argument("--port", type=int, default=None, help="default 8765, or [defaults] port")
    web.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    web.set_defaults(func=cmd_web)

    init = sub.add_parser("init", help="set up your profile in the terminal")
    init.set_defaults(func=cmd_init)

    mcp = sub.add_parser("mcp", help="run as an MCP server so any assistant can drive the tool")
    mcp.add_argument("--list", action="store_true", help="print the tools and exit")
    mcp.set_defaults(func=cmd_mcp)

    wa = sub.add_parser("watch", help="re-check (or re-push) a plan file whenever it changes")
    wa.add_argument("plan")
    wa.add_argument("--push", action="store_true", help="push on change instead of just checking")
    wa.add_argument("--email")
    wa.add_argument("--device")
    wa.set_defaults(func=cmd_watch)

    en = sub.add_parser(
        "enrich", help="add fuelling, heat, strength, durability and cadence cues by rule"
    )
    en.add_argument("plan")
    en.add_argument("--heat", metavar="RACE_DATE", help="add a heat block before this race date")
    en.add_argument(
        "--strength", type=int, default=0, metavar="N", help="add N strength sessions a week"
    )
    en.add_argument("--no-fuelling", action="store_true")
    en.add_argument("--no-durability", action="store_true")
    en.add_argument("--no-cadence", action="store_true")
    en.add_argument("-o", "--output", help="write the enriched plan here (default: overwrite)")
    en.set_defaults(func=cmd_enrich)

    ev = sub.add_parser("eval", help="measure plan quality across athlete cases (costs tokens)")
    ev.add_argument("--provider", help="provider name from your profile")
    ev.add_argument("--cases", help="a JSON file of cases; default: three built-in athletes")
    ev.add_argument("--attempts", type=int, default=3)
    ev.add_argument("--json", action="store_true", help="print JSON instead of a table")
    ev.add_argument("-o", "--output", help="write the results JSON here")
    ev.set_defaults(func=cmd_eval)

    rc = sub.add_parser(
        "recover", help="a post-race recovery block (Pfitzinger's weeks after a race)"
    )
    rc.add_argument("--race", required=True, metavar="DATE", help="the race date")
    rc.add_argument("--distance", default="marathon")
    rc.add_argument("-o", "--output", help="write the plan JSON here")
    rc.set_defaults(func=cmd_recover)

    ps = sub.add_parser(
        "pushes", help="list push receipts (what went to Garmin, when, with which ids)"
    )
    ps.set_defaults(func=cmd_pushes)

    ft = sub.add_parser("fit", help="write one FIT workout file per session, for USB sideload")
    ft.add_argument("plan")
    ft.add_argument(
        "-o", "--output", default="fit", help="directory to write into (default: ./fit)"
    )
    ft.set_defaults(func=cmd_fit)

    rp = sub.add_parser(
        "race-plan", help="split targets for a goal time: even, negative or 10-10-10"
    )
    rp.add_argument(
        "--distance", required=True, help="5k, 10k, half, marathon, or a distance like 15km"
    )
    rp.add_argument("--time", required=True, help="goal time, e.g. 1:45:00")
    rp.add_argument("--strategy", choices=["even", "negative", "10-10-10"], default="even")
    rp.add_argument("--workout", metavar="DATE", help="also write a race-day session dated DATE")
    rp.add_argument("--name", default="Race", help="race name for the session")
    rp.add_argument(
        "-o", "--output", help="plan file to write the race session into (with --workout)"
    )
    rp.set_defaults(func=cmd_race_plan)

    nx = sub.add_parser("next", help="the next session on a plan, watch-style")
    nx.add_argument("plan", nargs="?", help="plan file (default: the most recent plan)")
    nx.set_defaults(func=cmd_next)

    wk = sub.add_parser("week", help="this week and next: sessions, volume, phase changes")
    wk.add_argument("plan", nargs="?", help="plan file (default: the most recent plan)")
    wk.set_defaults(func=cmd_week)

    pn = sub.add_parser("plans", help="recent plans this app has opened or written")
    pn.set_defaults(func=cmd_plans)

    sc = sub.add_parser("schema", help="the plan DSL's JSON Schema, or a one-page reference")
    sc.add_argument(
        "--markdown", action="store_true", help="a readable reference instead of the schema"
    )
    sc.set_defaults(func=cmd_schema)

    pr = sub.add_parser("profile", help="read or set one profile field without opening the TOML")
    pr.add_argument("action", choices=["get", "set"])
    pr.add_argument(
        "key", help="dotted key, e.g. pace.threshold, athlete.recent_weekly_km, goal_race.date"
    )
    pr.add_argument("value", nargs="?", help="for set: a number, true/false, JSON list, or text")
    pr.set_defaults(func=cmd_profile)

    co = sub.add_parser("completions", help="print a shell completion script")
    co.add_argument("shell", choices=["bash", "zsh", "fish", "powershell"])
    co.set_defaults(func=cmd_completions)

    bk = sub.add_parser(
        "backup", help="zip the profile, library, history, receipts and recent plans"
    )
    bk.add_argument("-o", "--output", help="zip file to write (default: gpp-backup-<date>.zip)")
    bk.add_argument("--with-tokens", action="store_true", help="include the Garmin login tokens")
    bk.set_defaults(func=cmd_backup)

    rs = sub.add_parser(
        "restore", help="restore a backup zip (existing files are kept unless --force)"
    )
    rs.add_argument("archive")
    rs.add_argument("--force", action="store_true")
    rs.set_defaults(func=cmd_restore)

    zones = sub.add_parser("zones", help="show your resolved training zones")
    zones.set_defaults(func=cmd_zones)

    prompt = sub.add_parser("prompt", help="print the LLM prompt for writing a plan")
    prompt.set_defaults(func=cmd_prompt)

    providers = sub.add_parser("providers", help="list configured AI providers")

    providers.add_argument(
        "--bench", action="store_true", help="measure a provider on two short plans"
    )

    providers.add_argument(
        "--provider", help="which provider to bench (default: the preselected one)"
    )
    providers.set_defaults(func=cmd_providers)

    doctor = sub.add_parser("doctor", help="check profile, AI keys and Garmin setup")
    doctor.add_argument(
        "--ping",
        action="store_true",
        help="actually call each configured AI provider (costs a few tokens)",
    )
    doctor.add_argument("--bundle", help="write a diagnostics JSON file here for a bug report")
    doctor.set_defaults(func=cmd_doctor)

    generate = sub.add_parser("generate", help="have an AI write a plan")
    generate.add_argument(
        "request",
        nargs="?",
        help='e.g. "4 weeks to a 10k, 5 runs a week, one long run Sunday"',
    )
    generate.add_argument("--provider", help="provider name from your profile")
    generate.add_argument("-o", "--output", help="write the plan JSON here")
    generate.add_argument(
        "--continue-from",
        metavar="PLAN",
        help="a previous plan file: the new block carries on from where it ended",
    )
    generate.add_argument(
        "--chunk-weeks",
        type=int,
        help="generate long plans one phase at a time (an outline first); use for 12+ weeks",
    )
    generate.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="how many times to hand validation errors back to the model",
    )
    generate.add_argument("--show", action="store_true", help="also render the plan as text")
    generate.set_defaults(func=cmd_generate)

    for name, help_text, func in (
        ("check", "validate a plan file", cmd_check),
        ("show", "render a plan as text (no network)", cmd_show),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("plan")
        p.set_defaults(func=func)

    compile_p = sub.add_parser("compile", help="emit the raw Garmin workout JSON")
    compile_p.add_argument("plan")
    compile_p.add_argument("-o", "--output")
    compile_p.set_defaults(func=cmd_compile)

    push = sub.add_parser("push", help="upload and schedule on Garmin Connect")
    push.add_argument("plan")
    push.add_argument("--dry-run", action="store_true", help="render only, send nothing")
    push.add_argument(
        "--live",
        action="store_true",
        help="with --dry-run: read the calendar and show what a push would change",
    )
    push.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    push.add_argument(
        "--replace",
        action="store_true",
        default=True,
        help="replace this tool's own earlier workouts on the same day (default)",
    )
    push.add_argument(
        "--no-replace",
        dest="replace",
        action="store_false",
        help="fail instead of replacing",
    )
    push.add_argument("--no-verify", action="store_true", help="skip reading workouts back")
    push.add_argument("--email", help="Garmin account email")
    push.add_argument("--device", help="device id to send to immediately (see: gpp devices)")
    push.add_argument("--token-dir", help="where to cache the auth token")
    push.set_defaults(func=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "verbose", False):
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(
            filename=LOG_PATH,
            level=logging.DEBUG,
            format="%(asctime)s %(name)s %(levelname)s %(message)s",
        )
        logging.getLogger("gpp").debug("gpp %s: %s", _version(), " ".join(argv or sys.argv[1:]))
        print(f"debug log: {LOG_PATH}", file=sys.stderr)
    try:
        return args.func(args)
    except (PlanError, ProfileError, CompileError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
