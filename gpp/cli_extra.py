"""The second tier of commands: library, templates, formats, diffs, adaptation.

Kept out of cli.py so the front door stays short. Everything here is a thin
wrapper over a module that is tested on its own.
"""

from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

from . import (
    adapt,
    analysis,
    checks,
    diff,
    environment,
    formats,
    library,
    models,
    oneline,
    transpile,
)
from .compile import compile_plan
from .plan import Plan
from .profile import Profile, ProfileError, find_profile
from .render import render_plan, render_workout
from .units import format_duration, format_pace, parse_distance, parse_duration, parse_pace


def _profile(args) -> Profile:
    if getattr(args, "profile", None):
        return Profile.load(args.profile)
    found = find_profile(getattr(args, "athlete", None))
    if found is None:
        raise ProfileError("no profile yet. Run  gpp web  or  gpp init  first.")
    return Profile.load(found)


def _date(text: str | None, default: dt.date | None = None) -> dt.date:
    if not text:
        return default or dt.date.today()
    return dt.date.fromisoformat(text)


def _write_or_print(text: str, output: str | None) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"wrote {output}")
    else:
        sys.stdout.write(text)


# --- library ----------------------------------------------------------------


def cmd_library(args) -> int:
    if args.action == "list":
        for item in library.list_workouts():
            print(f"  {item['slug']:<28} {item['name']:<32} {item['source']}")
        plans = library.list_plans()
        if plans:
            print("\nplan templates:")
            for item in plans:
                print(f"  {item['slug']:<28} {item['name']:<32} {item['workouts']} workouts")
        return 0
    if args.action == "save":
        plan = Plan.load(args.file)
        if args.workout:
            match = [
                w
                for w in plan.workouts
                if w.name == args.workout or w.date.isoformat() == args.workout
            ]
            if not match:
                print(f"error: no workout {args.workout!r} in {args.file}", file=sys.stderr)
                return 2
            path = library.save_workout(match[0], args.name)
        else:
            path = library.save_plan(plan, args.name)
        print(f"saved {path}")
        return 0
    if args.action == "use":
        profile = _profile(args)
        workout = library.load_workout(args.name, _date(args.date))
        plan = Plan(plan=workout.name, workouts=[workout])
        _write_or_print(
            plan.dumps()
            if args.output
            else render_plan(plan, compile_plan(plan, profile), profile),
            args.output,
        )
        return 0
    return 2


def cmd_template(args) -> int:
    if args.action == "apply":
        start = _date(args.start)
        race = _date(args.race) if args.race else None
        plan = library.apply_plan(args.name, start - dt.timedelta(days=start.weekday()), race)
        _write_or_print(plan.dumps(), args.output)
        return 0
    if args.action == "return":
        plan = library.return_to_run(_date(args.start), args.tier)
        _write_or_print(plan.dumps(), args.output)
        return 0
    return 2


# --- one-line, formats, share, diff -----------------------------------------


def cmd_oneline(args) -> int:
    profile = _profile(args)
    try:
        workout = oneline.parse_workout(
            args.text, name=args.name, date=_date(args.date).isoformat()
        )
    except oneline.OneLineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    plan = Plan(plan=workout.name, workouts=[workout])
    if args.output:
        _write_or_print(plan.dumps(), args.output)
    else:
        print(render_workout(compile_plan(plan, profile)[0], profile))
    return 0


def cmd_export(args) -> int:
    profile = _profile(args)
    plan = Plan.load(args.plan)
    fmt = args.format.lower()
    if fmt == "share":
        _write_or_print(formats.export_share(plan, profile, args.note), args.output)
        return 0
    if fmt == "json":
        _write_or_print(plan.dumps(), args.output)
        return 0
    out_dir = Path(args.output or ".")
    out_dir.mkdir(parents=True, exist_ok=True)
    for w in plan.workouts:
        text = formats.export_workout(w, profile, fmt)
        ext = {"icu": "txt", "zwo": "zwo", "mrc": "mrc", "erg": "erg"}[fmt]
        path = out_dir / f"{w.date.isoformat()}-{library._slug(w.name)}.{ext}"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path}")
    return 0


def cmd_import(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    start = _date(args.start) if args.start else None
    try:
        if text.lstrip().startswith("{"):
            bundle = formats.import_share(
                text, start - dt.timedelta(days=start.weekday()) if start else None
            )
            plan = bundle.plan
            print(
                f"shared by {bundle.shared_by} at {bundle.exported_at}"
                + (f": {bundle.note}" if bundle.note else "")
            )
        else:
            workout = formats.import_workout(text, _date(args.start), args.format)
            plan = Plan(plan=workout.name, workouts=[workout])
    except formats.FormatError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    _write_or_print(plan.dumps(), args.output)
    return 0


def cmd_diff(args) -> int:
    before, after = Plan.load(args.before), Plan.load(args.after)
    result = diff.diff_plans(before, after)
    print(result.text())
    return 0 if result.empty else 1


def cmd_transpile(args) -> int:
    profile = _profile(args)
    plan = Plan.load(args.plan)
    out = (
        transpile.to_power(plan, profile)
        if args.to == "power"
        else transpile.to_pace(plan, profile)
    )
    _write_or_print(out.dumps(), args.output)
    return 0


# --- adaptation -------------------------------------------------------------


def cmd_pause(args) -> int:
    plan = Plan.load(args.plan)
    result = adapt.pause_plan(plan, _date(args.start), args.days, args.reason)
    for r in result.reasons:
        print(f"  {r}")
    for d in result.dropped:
        print(f"  dropped: {d}")
    _write_or_print(result.plan.dumps(), args.output)
    return 0


def cmd_missed(args) -> int:
    profile = _profile(args)
    plan = Plan.load(args.plan)
    result = adapt.replan_missed(plan, [_date(d) for d in args.dates], profile)
    for r in result.reasons:
        print(f"  {r}")
    _write_or_print(result.plan.dumps(), args.output)
    return 0


def cmd_layoff(args) -> int:
    tier, advice = adapt.layoff_tier(args.days)
    print(f"{args.days} days off -> {tier}: {advice}")
    return 0


# --- numbers ----------------------------------------------------------------


def cmd_predict(args) -> int:
    profile = _profile(args)
    for label, metres in (("5k", 5000), ("10k", 10000), ("half", 21097.5), ("marathon", 42195)):
        fast, likely, slow = models.predict_from_threshold(profile.threshold_pace, metres)
        line = f"  {label:<9} {format_duration(likely)}  (range {format_duration(fast)} - {format_duration(slow)})"
        if profile.cs:
            try:  # noqa: SIM105
                line += (
                    f"   CS model: {format_duration(models.predict_from_cs(profile.cs, metres))}"
                )
            except models.ModelError:
                pass
        if profile.vdot:
            line += f"   VDOT {profile.vdot:.0f}: {format_duration(models.race_time_for_vdot(profile.vdot, metres))}"
        print(line)
    print("\nBands are Riegel's typical +-3%; treat them as a range, not a promise.")
    return 0


def cmd_weather(args) -> int:
    profile = _profile(args)
    if args.temp is not None and args.humidity is not None:
        cond = environment.Conditions(args.temp, args.humidity)
    elif profile.latitude is not None and profile.longitude is not None:
        when = dt.datetime.combine(_date(args.date), dt.time(args.hour))
        try:
            cond = environment.fetch_forecast(profile.latitude, profile.longitude, when)
        except environment.EnvironmentError_ as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    else:
        print(
            "error: give --temp and --humidity, or set [location] in the profile", file=sys.stderr
        )
        return 2
    pace = parse_pace(args.pace) if args.pace else profile.pace_zone("easy")[1]
    info = cond.describe(pace)
    print(f"  {info['temp_c']} C, {info['humidity_pct']}% RH, dew point {info['dew_point_c']} C")
    print(f"  WBGT {info['wbgt_c']} C ({info['wbgt_band']}), heat band: {info['band']}")
    print(
        f"  slow targets by {info['slowdown_s_per_km']} s/km: {format_pace(pace)} -> {format_pace(info['adjusted_pace_spk'])}"
    )
    if profile.latitude is not None and profile.longitude is not None:
        note = environment.daylight_note(
            profile.latitude,
            profile.longitude,
            _date(args.date),
            dt.time(args.hour),
            args.utc_offset,
        )
        if note:
            print(f"  {note}")
    return 0


def cmd_cs(args) -> int:
    trials = []
    for spec in args.trial:
        try:
            distance, time = spec.split("@")
            trials.append((parse_distance(distance), parse_duration(time)))
        except Exception as exc:
            print(
                f"error: trial {spec!r} should look like 1600m@5:30 (distance@time): {exc}",
                file=sys.stderr,
            )
            return 2
    try:
        cs = models.critical_speed(trials)
    except models.ModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(
        f"  critical speed {cs.cs_mps:.2f} m/s = {format_pace(cs.cs_pace)}, D' {cs.d_prime_m:.0f} m"
    )
    if cs.note:
        print(f"  {cs.note}")
    print(
        "\nAdd to profile.toml:\n[cs]\ntrials = ["
        + ", ".join(f'{{ distance = "{d:g}m", time = "{format_duration(t)}" }}' for d, t in trials)
        + ']\n[pace]\nmodel = "cs"'
    )
    return 0


def cmd_advice(args) -> int:
    advice = analysis.today_advice(
        args.role, args.readiness, args.hrv, args.sleep, args.resting_hr, args.baseline_hr
    )
    for r in advice.reasons:
        print(f"  - {r}")
    print(f"  {advice.suggestion}")
    return 0


def cmd_report(args) -> int:
    profile = _profile(args)
    plan = Plan.load(args.plan)
    print(checks.check(plan, profile).text())
    from .load import plan_dashboard

    dash = plan_dashboard(plan, profile)
    print(
        f"\n{dash['sessions']} sessions, {dash['total_km']} km, {dash['total_minutes']} min, {dash['hard_share']:.0%} hard"
    )
    for w in dash["weeks"]:
        mono = f", monotony {w['monotony']}" if w["monotony"] is not None else ""
        print(
            f"  week of {w['start']}: {w['sessions']} sessions, {w['km']} km, {w['hard_share']:.0%} hard, long {w['longest_run_km']} km{mono}"
        )
    return 0


# --- registration -----------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    lib = sub.add_parser("library", help="your saved workouts and plan templates")
    lib.add_argument("action", choices=["list", "save", "use"])
    lib.add_argument("file", nargs="?", help="plan file (save) or name (use)")
    lib.add_argument("--workout", help="save just this workout (by name or date) from the plan")
    lib.add_argument("--name", help="name to save under / workout to use")
    lib.add_argument("--date", help="date for a used workout (default today)")
    lib.add_argument("-o", "--output")
    lib.set_defaults(func=lambda a: cmd_library(_normalise_library(a)))

    tpl = sub.add_parser(
        "template", help="apply a plan template to dates, or build a return-to-run ramp"
    )
    tpl.add_argument("action", choices=["apply", "return"])
    tpl.add_argument("name", nargs="?", help="template name or path (apply)")
    tpl.add_argument("--start", help="first week's Monday, or the start date for a ramp")
    tpl.add_argument("--race", help="re-anchor the template's A race to this date")
    tpl.add_argument(
        "--tier",
        default="restart-phase",
        choices=["resume", "restart-phase", "back-to-base", "foundation"],
    )
    tpl.add_argument("-o", "--output")
    tpl.set_defaults(func=cmd_template)

    one = sub.add_parser(
        "oneline",
        help='a workout from a sentence: "20min warmup, 6x3m @ T w/ 2m jog, 10min cooldown"',
    )
    one.add_argument("text")
    one.add_argument("--name")
    one.add_argument("--date")
    one.add_argument("-o", "--output")
    one.set_defaults(func=cmd_oneline)

    exp = sub.add_parser(
        "export", help="write a plan as icu / zwo / mrc / erg files, or a share bundle"
    )
    exp.add_argument("plan")
    exp.add_argument(
        "--format", default="share", choices=["share", "json", "icu", "zwo", "mrc", "erg"]
    )
    exp.add_argument("--note", help="a line for the person you are sending it to")
    exp.add_argument("-o", "--output", help="file (share/json) or directory (per-workout formats)")
    exp.set_defaults(func=cmd_export)

    imp = sub.add_parser("import", help="read a share bundle, intervals.icu text or a ZWO file")
    imp.add_argument("file")
    imp.add_argument("--format", choices=["icu", "zwo"])
    imp.add_argument(
        "--start", help="date for a single workout, or the Monday to re-base a shared plan to"
    )
    imp.add_argument("-o", "--output")
    imp.set_defaults(func=cmd_import)

    dif = sub.add_parser("diff", help="what changed between two plan files")
    dif.add_argument("before")
    dif.add_argument("after")
    dif.set_defaults(func=cmd_diff)

    tr = sub.add_parser("transpile", help="convert pace targets to power, or back")
    tr.add_argument("plan")
    tr.add_argument("--to", choices=["power", "pace"], default="power")
    tr.add_argument("-o", "--output")
    tr.set_defaults(func=cmd_transpile)

    pa = sub.add_parser("pause", help="illness or holiday: shift the plan and scale the return")
    pa.add_argument("plan")
    pa.add_argument("--start", required=True)
    pa.add_argument("--days", type=int, required=True)
    pa.add_argument("--reason", default="break")
    pa.add_argument("-o", "--output")
    pa.set_defaults(func=cmd_pause)

    mi = sub.add_parser("missed", help="replan around sessions you missed")
    mi.add_argument("plan")
    mi.add_argument("dates", nargs="+")
    mi.add_argument("-o", "--output")
    mi.set_defaults(func=cmd_missed)

    lo = sub.add_parser("layoff", help="what a break of N days means for the plan")
    lo.add_argument("days", type=int)
    lo.set_defaults(func=cmd_layoff)

    pr = sub.add_parser("predict", help="race-time predictions with honest ranges")
    pr.set_defaults(func=cmd_predict)

    we = sub.add_parser("weather", help="heat-adjusted pace for a day (typed or forecast)")
    we.add_argument("--temp", type=float, help="C")
    we.add_argument("--humidity", type=float, help="relative humidity, percent")
    we.add_argument("--pace", help="target pace to adjust, e.g. 5:00/km")
    we.add_argument("--date")
    we.add_argument("--hour", type=int, default=7)
    we.add_argument("--utc-offset", type=float, default=0.0)
    we.set_defaults(func=cmd_weather)

    cs = sub.add_parser(
        "cs", help="critical speed from two or more all-out trials, e.g. 1500m@5:10 3000m@11:20"
    )
    cs.add_argument("trial", nargs="+")
    cs.set_defaults(func=cmd_cs)

    ad = sub.add_parser(
        "advice", help="should today's session be easier? (a nudge, never a command)"
    )
    ad.add_argument("--role", default="quality")
    ad.add_argument("--readiness", type=int)
    ad.add_argument("--hrv")
    ad.add_argument("--sleep", type=int)
    ad.add_argument("--resting-hr", type=int)
    ad.add_argument("--baseline-hr", type=int)
    ad.set_defaults(func=cmd_advice)

    rp = sub.add_parser("report", help="sanity report and weekly dashboard for a plan")
    rp.add_argument("plan")
    rp.set_defaults(func=cmd_report)


def _normalise_library(args):
    if args.action == "use" and args.file and not args.name:
        args.name = args.file
    return args
