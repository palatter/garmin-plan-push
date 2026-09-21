"""Command line interface."""

from __future__ import annotations

import argparse
import getpass
import json
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

    serve(
        port=args.port,
        open_browser=not args.no_browser,
        profile_path=Path(args.profile) if args.profile else None,
    )
    return 0


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


def cmd_zones(args: argparse.Namespace) -> int:
    print(_load_profile(args.profile).describe())
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
    chosen = pick_default(configs, default)
    for name, config in configs.items():
        resolve(config)
        marker = "*" if name == chosen else " "
        target = config.model or "(any model)"
        if config.base_url:
            target += f" @ {config.base_url}"
        print(f" {marker} {name:<12} {config.kind:<18} {target:<48} {_key_status(config)}")
    print("\n* = will be used by default. Configure more under [ai.providers] in your profile.")
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

    print("\nGarmin")
    try:
        import garminconnect  # noqa: F401

        line(True, "python-garminconnect", "installed")
    except ImportError:
        line(False, "python-garminconnect", "missing - run: uv sync")
    token_dir = Path.home() / ".garminconnect"
    line(
        True if token_dir.exists() else None,
        "saved login",
        "found - push won't ask for a password"
        if token_dir.exists()
        else "none yet - first push will ask for your Garmin password",
    )
    line(
        None,
        "watch support",
        "needs structured workouts: Fenix 6+, Epix, FR 255/265/955/965, Edge 530+",
    )

    print()
    print("Everything looks ready." if ok else "Fix the lines marked !! and run again.")
    return 0 if ok else 1


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

    result = generate_plan(
        provider, profile, request, attempts=args.attempts, log=lambda m: print(m)
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
    profile, plan, compiled = _load(args)
    print(f"OK: {plan.plan} - {len(compiled)} workout(s) valid")
    report = checks.check(plan, profile)
    print(report.text())
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

    if args.dry_run:
        print("Dry run: nothing sent to Garmin.")
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

    results = client.push(
        compiled,
        replace=args.replace,
        verify=not args.no_verify,
        log=lambda line: print(line),
    )

    failures = [r for r in results if r.action == "failed"]
    warnings = [r for r in results if r.detail and r.action != "failed"]
    print()
    for result in warnings:
        print(f"warning: {result.date} {result.name}: {result.detail}")
    for result in failures:
        print(f"failed:  {result.date} {result.name}: {result.detail}", file=sys.stderr)

    created = sum(1 for r in results if r.action in ("created", "replaced"))
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
    sub = parser.add_subparsers(dest="command", required=True)

    from .cli_extra import register

    register(sub)

    web = sub.add_parser("web", help="open the graphical app in your browser")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--no-browser", action="store_true", help="don't open a browser window")
    web.set_defaults(func=cmd_web)

    init = sub.add_parser("init", help="set up your profile in the terminal")
    init.set_defaults(func=cmd_init)

    zones = sub.add_parser("zones", help="show your resolved training zones")
    zones.set_defaults(func=cmd_zones)

    prompt = sub.add_parser("prompt", help="print the LLM prompt for writing a plan")
    prompt.set_defaults(func=cmd_prompt)

    providers = sub.add_parser("providers", help="list configured AI providers")
    providers.set_defaults(func=cmd_providers)

    doctor = sub.add_parser("doctor", help="check profile, AI keys and Garmin setup")
    doctor.add_argument(
        "--ping",
        action="store_true",
        help="actually call each configured AI provider (costs a few tokens)",
    )
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
        "--attempts",
        type=int,
        default=3,
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
    push.add_argument("--token-dir", help="where to cache the auth token")
    push.set_defaults(func=cmd_push)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
