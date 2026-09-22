"""Commands over the history store and the Garmin read-side.

sync, history, compliance, reestimate, shape, today, log, unpush, devices,
exercises, garmin-predict. Each is a thin wrapper; the logic lives in
history.py, sync.py, analysis.py and client.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import os
import sys

from . import analysis, sync
from .compile import compile_plan
from .history import History
from .plan import Plan
from .profile import Profile, ProfileError, find_profile
from .render import render_plan
from .units import format_duration, format_pace


def _profile(args) -> Profile:
    if getattr(args, "profile", None):
        return Profile.load(args.profile)
    found = find_profile(getattr(args, "athlete", None))
    if found is None:
        raise ProfileError("no profile yet. Run  gpp web  or  gpp init  first.")
    return Profile.load(found)


def _store(args) -> History:
    return History(args.db) if getattr(args, "db", None) else History()


def _connect(args):
    """Shared login flow for the commands that talk to Garmin."""
    from .client import GarminClient, PushError

    email = args.email or os.environ.get("GARMIN_EMAIL") or input("Garmin Connect email: ").strip()
    password = os.environ.get("GARMIN_PASSWORD") or getpass.getpass(
        "Garmin Connect password (not stored): "
    )
    client = GarminClient(email, password or None, token_dir=getattr(args, "token_dir", None))
    try:
        client.connect(prompt_mfa=lambda: input("Garmin MFA code: ").strip())
    except PushError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    print(f"connected via {client.transport}")
    return client


# --- sync and history -------------------------------------------------------


def cmd_sync(args) -> int:
    client = _connect(args)
    if client is None:
        return 2
    store = _store(args)
    result = sync.sync_activities(client.api, store, days=args.days, log=print)
    print(f"synced {result.activities} run(s) from the last {args.days} days")
    today = dt.date.today()
    for back in range(args.metric_days):
        sync.sync_metrics(client.api, store, today - dt.timedelta(days=back), log=print)
    if result.skipped:
        print("not available from this library version: " + ", ".join(sorted(set(result.skipped))))
    return 0


def cmd_history(args) -> int:
    store = _store(args)
    since = dt.date.today() - dt.timedelta(weeks=args.weeks)
    weeks = store.weekly(since)
    if not weeks:
        print("no activities yet -- run  gpp sync  first")
        return 1
    for w in weeks:
        print(
            f"  week of {w['start']}: {w['runs']} runs, {w['km']} km, {format_duration(w['seconds'])}, longest {w['longest_km']} km"
        )
    return 0


def cmd_compliance(args) -> int:
    profile = _profile(args)
    store = _store(args)
    plan = Plan.load(args.plan)
    compiled = compile_plan(plan, profile)
    from .timeline import workout_summary

    store.record_planned(
        plan.plan,
        [
            {
                "date": c.date,
                "name": c.name,
                "tag": c.tag,
                "seconds": c.estimated_seconds,
                "metres": workout_summary(w, profile)["metres"],
                "role": w.role,
            }
            for c, w in zip(compiled, plan.workouts, strict=True)
        ],
    )
    dates = [w.date for w in plan.workouts]
    rows = store.compliance(min(dates), max(dates))
    for r in rows:
        actual = format_duration(r["actual_seconds"]) if r["actual_seconds"] else "-"
        print(
            f"  {r['status']:<6} {r['date']}  {r['name']:<28} planned {format_duration(r['planned_seconds'])}  actual {actual}"
        )
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("  " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    return 0


def cmd_reestimate(args) -> int:
    profile = _profile(args)
    store = _store(args)
    est = store.reestimate_threshold()
    if est is None:
        print("no sustained efforts of 20-90 minutes in the last 60 days -- nothing to go on")
        return 1
    current = profile.threshold_pace
    print(f"  best effort:   {est.basis} on {est.activity_date} ({est.confidence} confidence)")
    print(f"  implies:       {format_pace(est.threshold_pace, profile.imperial)} threshold")
    print(f"  profile says:  {format_pace(current, profile.imperial)}")
    if est.threshold_pace < current - 1:
        print(
            "  faster than the profile -- worth updating."
            + ("" if args.apply else "  Re-run with --apply to write it.")
        )
        if args.apply:
            profile.threshold_pace = est.threshold_pace
            path = args.profile or find_profile(getattr(args, "athlete", None))
            profile.save(path)
            print(f"  wrote {path}")
    else:
        print(
            "  not faster than the profile; a slower estimate may just mean no hard efforts recently."
        )
    return 0


def cmd_shape(args) -> int:
    profile = _profile(args)
    store = _store(args)
    from .units import parse_distance

    goal_km = (
        parse_distance(args.distance) / 1000
        if args.distance
        else (
            parse_distance(profile.goal_race.distance) / 1000
            if profile.goal_race and profile.goal_race.distance
            else 42.195
        )
    )
    shape = store.marathon_shape(goal_km)
    print(
        f"  endurance shape for {goal_km:g} km: {shape.score}/100  (mileage {shape.mileage_score}, long runs {shape.long_run_score})"
    )
    print(f"  basis: {shape.basis}")
    return 0


def _status_line(store: History) -> str | None:
    recent = store.recent_status()
    if not recent:
        return None
    latest = recent[-1]
    return (
        f"logged {latest['kind']} on {latest['date']}"
        + (f" ({latest['note']})" if latest.get("note") else "")
        + " -- an easy day or a pause is the honest choice; see gpp pause"
    )


def _weather_lines(args, profile) -> list[str]:
    """Today's conditions folded into the advice (#199), typed or from the forecast."""
    from . import environment as env

    temp = getattr(args, "temp", None)
    humidity = getattr(args, "humidity", None)
    if getattr(args, "forecast", False) and temp is None:
        if profile.latitude is None or profile.longitude is None:
            return ["  (no latitude/longitude in the profile, so no forecast)"]
        when = dt.datetime.now().replace(hour=8, minute=0)
        try:
            info = env.fetch_forecast(profile.latitude, profile.longitude, when).describe()
        except env.EnvironmentError_ as exc:
            return [f"  ({exc})"]
        temp, humidity = info["temp_c"], info["humidity_pct"]
    if temp is None or humidity is None:
        return []
    dew = env.dew_point_c(temp, humidity)
    band = env.combined_band(temp, dew)
    slow = env.heat_slowdown_spk(dew)
    lines = [f"  weather: {temp:g} C, {humidity:g}% RH, dew point {dew:.0f} C -- {band}"]
    if slow >= 5:
        lines.append(f"  add about {slow:.0f} s/km to every target, or run by effort")
    if band == "no-hard-running":
        lines.append("  move any quality session to the coolest hour, or make today easy")
    elif band == "adjust":
        lines.append("  an early or late start beats the midday heat for a quality session")
    return lines


def cmd_today(args) -> int:
    store = _store(args)
    role = args.role
    if args.plan:
        plan = Plan.load(args.plan)
        today = [w for w in plan.workouts if w.date == dt.date.today()]
        if today:
            role = today[0].role or role
            print(f"  planned today: {today[0].name} ({role})")
    advice = store.today_advice(role)
    for r in advice.reasons:
        print(f"  - {r}")
    print(f"  {advice.suggestion}")
    status = _status_line(_store(args))
    if status:
        print(f"  {status}")
    for line in _weather_lines(args, _profile(args)):
        print(line)
    return 0


def cmd_log(args) -> int:
    store = _store(args)
    day = args.date or dt.date.today().isoformat()
    if args.what == "rpe":
        store.log_rpe(day, args.name or "session", int(args.value), args.note)
        print(f"logged RPE {args.value} for {day}")
    elif args.what == "pain":
        store.log_pain(day, args.name or "unspecified", int(args.value), args.pattern, args.note)
        print(f"logged pain {args.value}/10 at {args.name} for {day}")
    else:
        note = " ".join(x for x in (args.value, args.note) if x) or None
        store.log_status(day, args.what, note)
        print(f"logged {args.what} for {day}" + (f": {note}" if note else ""))
        if args.what in ("illness", "injury"):
            print(
                "  when you know how long it will last: gpp pause plan.json --start <date> --days <n>"
            )
    return 0


def cmd_pain(args) -> int:
    store = _store(args)
    trend = store.pain_trend(dt.date.today() - dt.timedelta(weeks=args.weeks))
    if not trend:
        print("no pain logged -- good")
        return 0
    for location, points in trend.items():
        series = " ".join(f"{d[5:]}:{lvl}" for d, lvl in points)
        print(f"  {location:<16} {series}")
    return 0


# --- Garmin extras ----------------------------------------------------------


def cmd_unpush(args) -> int:
    if getattr(args, "receipt", None):
        from .receipts import load_receipt

        receipt = load_receipt(args.receipt)
        ids = receipt.ids
        if not ids:
            print("that receipt lists no removable workouts")
            return 1
        if not args.yes:
            answer = (
                input(
                    f"Remove {len(ids)} workout(s) from '{receipt.plan}' ({receipt.when[:10]})? [y/N] "
                )
                .strip()
                .lower()
            )
            if answer not in ("y", "yes"):
                print("Aborted.")
                return 1
        client = _connect(args)
        if client is None:
            return 2
        results = client.unpush_ids(ids, log=print)
        removed = sum(1 for r in results if r.action == "removed")
        print(f"{removed} removed, {sum(1 for r in results if r.action == 'failed')} failed")
        return 0
    if not args.plan:
        print("error: give a plan file, or --receipt <file> (see: gpp pushes)")
        return 2
    profile = _profile(args)
    plan = Plan.load(args.plan)
    compiled = compile_plan(plan, profile)
    if not args.yes:
        answer = (
            input(f"Remove {len(compiled)} workout(s) of '{plan.plan}' from Garmin Connect? [y/N] ")
            .strip()
            .lower()
        )
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 1
    client = _connect(args)
    if client is None:
        return 2
    results = client.unpush(compiled, log=print)
    removed = sum(1 for r in results if r.action == "removed")
    print(f"{removed} removed, {sum(1 for r in results if r.action == 'failed')} failed")
    return 0


def cmd_pull(args) -> int:
    from .decompile import plan_from_calendar

    profile = _profile(args)
    client = _connect(args)
    if client is None:
        return 2
    start, end = dt.date.fromisoformat(args.start), dt.date.fromisoformat(args.end)
    items = client.scheduled_between(start, end)
    try:
        plan, skipped = plan_from_calendar(items, client.get_workout, profile, args.name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in skipped:
        print(f"  skipped: {line}")
    if args.output:
        print(f"{len(plan.workouts)} workout(s) written to {plan.save(args.output)}")
    else:
        print(render_plan(plan, compile_plan(plan, profile), profile), end="")
    return 0


def cmd_devices(args) -> int:
    client = _connect(args)
    if client is None:
        return 2
    for d in client.devices():
        print(f"  {d.id:<16} {d.name}")
    return 0


def cmd_exercises(args) -> int:
    client = _connect(args)
    if client is None:
        return 2
    hits = client.search_exercises(args.query)
    if not hits:
        print("no matches in Garmin's catalog -- try a shorter word")
        return 1
    for h in hits[:30]:
        print(f"  {h['name']:<36} {h.get('category') or ''}")
    return 0


def cmd_garmin_predict(args) -> int:
    profile = _profile(args)
    client = _connect(args)
    if client is None:
        return 2
    preds = sync.race_predictions(client.api)
    if not preds:
        print("Garmin has no race predictions for this account yet")
        return 1
    from . import models

    for label, seconds in preds.items():
        metres = {"5k": 5000, "10k": 10000, "half": 21097.5, "marathon": 42195}[label]
        _, ours, _ = models.predict_from_threshold(profile.threshold_pace, metres)
        gap = (seconds - ours) / ours
        print(
            f"  {label:<9} Garmin {format_duration(seconds)}   from your threshold {format_duration(ours)}   ({gap:+.0%})"
        )
    print("\nA large gap either way suggests re-checking the threshold pace in your profile.")
    zones = sync.hr_zones(client.api)
    if zones:
        print(
            "  Garmin HR zones: "
            + ", ".join(f"Z{i + 1} {lo}-{hi}" for i, (lo, hi) in enumerate(zones))
        )
        if getattr(args, "apply_zones", None):
            from .profile import find_profile

            updated = profile.with_garmin_hr_zones(zones)
            path = args.profile or find_profile()
            updated.save(path)
            print(f"  profile updated with Garmin's zones (LTHR {updated.lthr}): {path}")
    return 0


def cmd_suggestion(args) -> int:
    client = _connect(args)
    if client is None:
        return 2
    raw = client.daily_suggestion(
        dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    )
    if not raw:
        print("no Daily Suggested Workout available (or the library does not expose it)")
        return 1
    print(
        "Garmin's suggestion for the day exists. If you also push a plan, the two compete for the same day;"
    )
    print("turn suggestions off on the watch or let the pushed workout win.")
    return 0


# --- registration -----------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    def garmin_args(p):
        p.add_argument("--email")
        p.add_argument("--token-dir")

    sy = sub.add_parser(
        "sync", help="pull your runs and daily metrics from Garmin into local history"
    )
    sy.add_argument("--days", type=int, default=90)
    sy.add_argument("--metric-days", type=int, default=7)
    sy.add_argument("--db")
    garmin_args(sy)
    sy.set_defaults(func=cmd_sync)

    hi = sub.add_parser("history", help="weekly volume from synced runs")
    hi.add_argument("--weeks", type=int, default=12)
    hi.add_argument("--db")
    hi.set_defaults(func=cmd_history)

    co = sub.add_parser("compliance", help="planned vs actual, colour-coded the Final Surge way")
    co.add_argument("plan")
    co.add_argument("--db")
    co.set_defaults(func=cmd_compliance)

    re_ = sub.add_parser("reestimate", help="re-derive threshold pace from your recent runs")
    re_.add_argument(
        "--apply", action="store_true", help="write a faster estimate into the profile"
    )
    re_.add_argument("--db")
    re_.set_defaults(func=cmd_reestimate)

    sh = sub.add_parser("shape", help="endurance readiness for a distance, from history")
    sh.add_argument("--distance", help='e.g. "marathon", "21.1km"')
    sh.add_argument("--db")
    sh.set_defaults(func=cmd_shape)

    to = sub.add_parser("today", help="should today's session be easier? from synced metrics")
    to.add_argument("--temp", type=float, help="today's temperature (C), with --humidity")
    to.add_argument("--humidity", type=float, help="relative humidity (%%)")
    to.add_argument(
        "--forecast", action="store_true", help="fetch the forecast for the profile's location"
    )
    to.add_argument("--plan")
    to.add_argument("--role", default="quality")
    to.add_argument("--db")
    to.set_defaults(func=cmd_today)

    lg = sub.add_parser("log", help="log a session RPE or a pain score")
    lg.add_argument("what", choices=["rpe", "pain", "illness", "injury", "cycle", "note"])
    lg.add_argument(
        "value", nargs="?", help="RPE 1-10, pain 0-10, or a short note for status kinds"
    )
    lg.add_argument("--name", help="session name (rpe) or body location (pain)")
    lg.add_argument("--date")
    lg.add_argument("--pattern", help="pain: e.g. 'warms up', 'worse after', 'constant'")
    lg.add_argument("--note")
    lg.add_argument("--db")
    lg.set_defaults(func=cmd_log)

    pa = sub.add_parser("pain", help="pain trend by location")
    pa.add_argument("--weeks", type=int, default=8)
    pa.add_argument("--db")
    pa.set_defaults(func=cmd_pain)

    un = sub.add_parser("unpush", help="remove a plan's workouts from Garmin Connect")
    un.add_argument(
        "--receipt", help="a push receipt file (see: gpp pushes); removes exactly those ids"
    )
    un.add_argument("plan", nargs="?")
    un.add_argument("--yes", action="store_true")
    garmin_args(un)
    un.set_defaults(func=cmd_unpush)

    pl = sub.add_parser("pull", help="read scheduled Garmin workouts back into a plan file")
    pl.add_argument("--from", dest="start", required=True, metavar="DATE")
    pl.add_argument("--to", dest="end", required=True, metavar="DATE")
    pl.add_argument("--name", default="Garmin calendar")
    pl.add_argument("-o", "--output", help="plan file to write (default: print)")
    pl.add_argument("--email")
    pl.add_argument("--token-dir")
    pl.set_defaults(func=cmd_pull)

    de = sub.add_parser("devices", help="list your Garmin devices (for --device on push)")
    garmin_args(de)
    de.set_defaults(func=cmd_devices)

    ex = sub.add_parser("exercises", help="search Garmin's strength exercise catalog")
    ex.add_argument("query")
    garmin_args(ex)
    ex.set_defaults(func=cmd_exercises)

    gp = sub.add_parser(
        "garmin-predict", help="Garmin's race predictor and HR zones vs your profile"
    )

    gp.add_argument(
        "--apply-zones", action="store_true", help="adopt Garmin's HR zones into the profile"
    )
    garmin_args(gp)
    gp.set_defaults(func=cmd_garmin_predict)

    su = sub.add_parser(
        "suggestion",
        help="does Garmin have a Daily Suggested Workout that will compete with the plan?",
    )
    su.add_argument("--date")
    garmin_args(su)
    su.set_defaults(func=cmd_suggestion)


_ = analysis  # analysis is used through History; keep the import explicit for readers
