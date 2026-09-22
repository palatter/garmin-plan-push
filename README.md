# garmin-plan-push

Describe the training you want in plain English. Get real structured workouts
on your Garmin watch — and a plan that is checked, editable and adaptable
before and after it gets there.

```
"four weeks to a 10k, five runs a week"
        ↓
   an AI writes it  →  validator + sanity checks  →  you review, edit, drag
        ↓
   Garmin Connect  →  your watch
```

Runs as a local app in your browser. Nothing is hosted, no account to make,
and your Garmin password never leaves your machine.

**New here?** The [guide](https://palatter.github.io/garmin-plan-push/) walks
through the install, the first plan and sending it to the watch, in plain
language. The rest of this README is the reference.

## Install

You need [uv](https://docs.astral.sh/uv/getting-started/installation/) (one
command on any OS — the link has it; it fetches Python 3.12+ for you if
needed). Then:

```bash
uv tool install git+https://github.com/palatter/garmin-plan-push
```

```bash
gpp web
```

That opens the app. It asks for a recent race time, works out your training
paces, and you're ready. There is nothing else to configure.

To update later: `uv tool upgrade garmin-plan-push`. Tagged releases with the
wheel attached are on the
[releases page](https://github.com/palatter/garmin-plan-push/releases).

To try it without installing anything permanently:

```bash
uvx --from git+https://github.com/palatter/garmin-plan-push gpp web
```

`pipx install git+https://github.com/palatter/garmin-plan-push` works too.
The release workflow publishes to PyPI with trusted publishing once the
repository turns that on; from then, `uv tool install garmin-plan-push` is
the short form. `gpp --version` tells you what you have.

## Using it

**Setup** happens once. You don't need to know your threshold pace — give it a
race you ran hard ("10k, 47:30") and it derives everything, showing your zones
live as you type. Under *About your training* it asks what a coach would ask:
goal race, which days you can run, injury history, standing rules ("no hills
for six weeks"), your longest recent run. All optional; all of it goes into
every prompt and into the sanity checks.

**Write a plan** by describing it the way you'd tell a coach: *"Four weeks to a
10k. Five runs a week, one long run Sunday, one threshold session and one set
of hills. Keep Mondays easy."* Or open a template from the library and set its
start date.

**Review** before anything is sent:

- Each session is drawn as a profile, colour-coded by effort, with every step
  listed underneath and its load number (minutes weighted by intensity).
- The **sanity report** lists what the checks found — a long run that jumps
  past anything in the last month, a weekly ramp over 25%, hard days back to
  back, a taper that cuts too little or too much — with the dates to look at.
- **Plan health** shows every week as a bar: height is volume, colour is the
  share of hard running.
- **Edit** any session: date, name, the "why" line that goes on the wrist, a
  cue per step ("tall posture"), or retype the whole session in one line —
  `15m warm up, 5 x 1km @ T w/ 2m jog, 10m cool down`.
- **Calendar** view: drag a session to another day (arrow keys work too); the
  checks re-run and the weekly totals update.
- **On the watch** previews the step screens a Fenix will show, step by step.
- **This week** lists the days ahead with the key session, the coming phase
  change and the days to the race; **load focus** splits the recent weeks'
  minutes the way the watch does (low aerobic, high aerobic, anaerobic).
- Once runs are synced, each past session carries a one-line **recap** against
  what was planned, and **plan vs actual** graphs weekly volume.
- **Hot day** shows every pace slowed for today's dew point without editing
  the plan. **Move to…** moves a session without dragging. <kbd>?</kbd> lists
  the keys; <kbd>T</kbd> jumps to today.
- **Export** the plan as a calendar file (.ics), Markdown, CSV or a share
  bundle, or one session as intervals.icu text, ZWO, MRC/ERG or a Garmin FIT
  file. Print gives you the sessions and nothing else.
- Recent plans reopen from the first screen, and the app installs to a dock or
  home screen from the browser's install menu.
- **Changes** is the diff against the plan as generated or last pushed, with
  undo and reset.
- **Missed** a session? *Readapt* moves or drops what is around it by rule and
  says why; *Keep* leaves the plan alone. **Pause** for an illness or holiday
  shifts everything and scales the first week back.
- <kbd>Ctrl</kbd> <kbd>K</kbd> opens a command palette for all of the above.

**Send to Garmin**, enter your Connect login, and sync your watch. Re-sending
an edited plan updates the workouts in place, so Garmin keeps their IDs.

### Which AI writes the plan

Any of them. Out of the box the app offers **Claude**, **ChatGPT**, **Gemini**
and **paste**, and preselects whichever one you have a key for.

**No API key?** Pick **paste**. The app hands you the prompt, you drop it into
whatever assistant you already use — ChatGPT, Claude, Gemini, Copilot, a local
model, anything — and paste the reply back. Same validation, same charts, same
push to Garmin. Costs nothing, and the app tells you it's the way out whenever
a key is missing.

**With a key**, set it once and generation is automatic. Only the one you use
is needed:

```bash
setx ANTHROPIC_API_KEY sk-ant-...     # Claude      (Windows; use export on macOS/Linux)
setx OPENAI_API_KEY sk-...            # ChatGPT
setx GEMINI_API_KEY AIza...           # Gemini
```

Then restart the app. `gpp doctor --ping` proves each key and model actually
work before you rely on them. Generation streams, and each call reports its
tokens and estimated cost. `gpp providers` lists which local models (via
Ollama) are good enough to hold the schema and which are not.

**From another assistant**: `gpp mcp` runs the tool as an MCP server, so
Claude, ChatGPT, Gemini or a local model can check, preview, adapt and push
plans conversationally. Push is a dry run unless you have set
`GARMIN_EMAIL` and `GARMIN_PASSWORD` (or signed in before); the assistant
never sees either.

## Sharing it with someone

Send them the link. The repo is public, so the same two commands above are
all they need. Everything else is self-contained: no server to run, no
account, no API key required if they use the paste option.

Their profile is their own — paces, zones and plans live on their machine.
To hand a friend a plan, **Export → Share bundle**: it carries zone names, not
your paces, so `gpp import` gives them the same plan at *their* paces.

## Which watches work

Anything that supports structured workouts: **Fenix 6/7/8**, **Epix**,
**Forerunner 255/265/955/965**, **Edge 530+**. Developed against a Fenix 7s.

Older models like the Forerunner 235 or Vivoactive 3 don't accept structured
workouts at all, so this won't help there.

## What the checks enforce, and what they refuse to

Every plan — generated, pasted, edited or dragged — goes through the same
rules before it reaches a watch. Blocking findings go straight back to the
model as a correction; warnings are shown to you. The rules are the ones with
evidence behind them:

- a single run more than ~10% longer than the longest in the previous 30
  days (the best-supported injury signal there is);
- a weekly ramp over 25%, or four rising weeks without a deload;
- two quality sessions back to back, no rest day, no recovery day after
  quality work, no moderate sessions at all ("mild or brutal");
- Foster's monotony index over 2;
- a taper outside the 41–60% volume cut, or one that drops frequency or
  intensity (Bosquet's meta-analysis);
- a B or C race inside the 14 days before the A race;
- anything that violates your availability or standing rules.

Two things every comparable tool ships are **deliberately absent**: the
acute:chronic workload ratio (no evidence it predicts injury) and the 10%
rule (never validated in a trial). [ROADMAP.md](ROADMAP.md) has the sources.

## After the plan

Once you have synced runs from Garmin (`gpp sync`), the plan stops being a
one-way document:

| | |
|---|---|
| `gpp compliance plan.json` | planned vs actual per session, colour-coded the Final Surge way |
| `gpp reestimate` | re-derive your threshold from recent runs (`--apply` writes it) |
| `gpp today --plan plan.json` | should today's session be easier? from readiness, HRV and sleep — a nudge with its reason, never a rewrite |
| `gpp shape --distance marathon` | do you have the endurance for the distance (Runalyze's marathon shape) |
| `gpp missed plan.json 2026-03-14` | replan around a missed session, with a reason for every change |
| `gpp pause plan.json --start 2026-03-14 --days 7` | illness or holiday |
| `gpp layoff 21` | what a break of N days means, by Stryd's tiers |
| `gpp log rpe 7` / `gpp log pain 3 --name knee` | session RPE and a pain log with a trend |
| `gpp log illness` / `gpp log cycle --note "..."` | body status that feeds the pause suggestion (logging only, by design) |
| `gpp recap plan.json` | one line per past session from the synced run: on plan, short, skipped |
| `gpp next` / `gpp week` | the next session, watch-style / this week and next with the key session |
| `gpp today --forecast` | the daily nudge with today's dew point folded in |
| `gpp recover --race 2026-10-12 --distance marathon` | a post-race recovery block (Pfitzinger's weeks after) |
| `gpp why threshold` | what a kind of session does, and the evidence behind it |

None of it is automatic. Every adaptation is a stated rule, shown with its
reason, and you can undo it.

## Why it's built this way

The Garmin half is easy. The hard part is that a language model writing
Garmin's workout JSON directly produces plausible-looking output that is subtly
wrong — a recovery jog targeted at 5k pace, a repeat group with mismatched
child IDs — and you find out at 6am when the watch starts beeping.

So the model never writes Garmin JSON. It writes a small DSL with human units
and named zones, which is validated hard and then compiled by deterministic,
tested code. Three consequences:

- **Ambiguity is designed out.** Pace bounds are `slow`/`fast`, never
  `low`/`high`, because "low pace" is genuinely ambiguous and models get it
  wrong about half the time. Each step takes exactly one of
  duration/distance/lap. Zone names are a closed set resolved from *your*
  threshold, so a model cannot invent paces — and when your threshold changes,
  every future session changes with it.
- **Errors are fed back.** Anything the validator, compiler or sanity checks
  reject goes straight back to the model as a correction turn, and only a plan
  that survives is shown to you.
- **Nothing is trusted blindly.** Garmin's workout API is undocumented outside
  their partner program, so every magic constant lives in one annotated file,
  and each push reads the workout back from Garmin and diffs it against what
  was sent.

## Re-running is safe

Every workout carries a marker in its description: `[gpp:<plan>:<hash>]`.

- same hash → left alone, reported as `unchanged`
- different hash → updated in place (Garmin keeps the ID)
- **no marker → never touched**, so anything you built by hand in Garmin
  Connect is safe
- `gpp unpush plan.json` removes exactly the workouts a plan created

## Command line

The browser app is the front door; everything is also a command. `--profile`
picks a profile file; several athletes can share one install.

**Plans**

| Command | |
|---|---|
| `gpp web` | the graphical app |
| `gpp init` / `gpp doctor` | set up in the terminal / check profile, AI keys and Garmin (`--ping`, `--bundle`) |
| `gpp zones` / `gpp providers` | your resolved zones / configured AI providers and local-model verdicts |
| `gpp generate "..."` | write a plan (`--attempts`, `--provider`) |
| `gpp prompt` | print the prompt, to use in any chat window |
| `gpp check` / `gpp report` / `gpp show` | validate / sanity report and weekly dashboard / render as text |
| `gpp oneline "20m wu, 6x3m @ T w/ 2m jog, 10m cd"` | a workout from a sentence |
| `gpp library list\|save\|use` / `gpp template` | saved sessions and plan templates, re-based to any start date or race date |
| `gpp diff a.json b.json` | what changed between two plan files |
| `gpp enrich plan.json` | fuelling cues, a heat block, strength placement, durability notes, cadence cues — by rule, each with its reason |
| `gpp race-plan --distance half --time 1:45:00` | split targets (even, negative, 10-10-10) and, with `--workout DATE`, a race-day session |
| `gpp export` / `gpp import` | JSON, share bundles, CSV, Markdown; one session as intervals.icu text, ZWO, MRC/ERG |
| `gpp fit plan.json` | one Garmin FIT workout file per session, for USB sideload |
| `gpp icu plan.json --athlete i12345` | push to an intervals.icu calendar, the partner route to Garmin (`--remove` takes it back) |
| `gpp eval` / `gpp providers --bench` | plan quality across athlete cases (costs tokens) / measure a local model |
| `gpp transpile` | pace targets to running power, or back |
| `gpp predict` / `gpp cs` | race-time predictions with ranges / critical speed from two trials |
| `gpp weather` | heat-adjusted pace for a day, typed or from a forecast |
| `gpp watch plan.json` | re-check (or `--push`) whenever the file changes |
| `gpp mcp` | run as an MCP server |
| `gpp plans` / `gpp schema` / `gpp completions bash` | recent plans / the DSL's JSON Schema (`--markdown` for the reference) / shell completions |
| `gpp profile set key value` / `gpp backup` / `gpp restore` | one field without opening the TOML / everything in one zip, and back |
| `gpp --json …` / `gpp --verbose …` | machine-readable output on the read-only commands / a debug log for `doctor --bundle` |

**Garmin**

| Command | |
|---|---|
| `gpp push plan.json` | upload and schedule (`--dry-run`, `--dry-run --live` to compare with the calendar first, `--device` to send to the watch now) |
| `gpp unpush plan.json` / `gpp pushes` | remove a plan's workouts, or `--receipt` to remove exactly what a push created / list the receipts |
| `gpp pull --from 2026-09-01 --to 2026-09-30` | read scheduled Garmin workouts back into a plan file |
| `gpp compile plan.json` | emit the raw Garmin workout JSON |
| `gpp devices` / `gpp exercises` | your devices / search the strength exercise catalog |
| `gpp sync` | pull runs and daily metrics into local history |
| `gpp history` / `gpp compliance` / `gpp reestimate` / `gpp shape` / `gpp today` / `gpp advice` | see *After the plan* |
| `gpp garmin-predict` / `gpp suggestion` | Garmin's race predictor and HR zones vs your profile / its Daily Suggested Workout |
| `gpp pause` / `gpp missed` / `gpp layoff` / `gpp log` / `gpp pain` | adaptation and the logs |

## AI providers

Provider-neutral by design — the DSL is the contract, so switching models is a
one-word change. Configured under `[ai.providers]` in `profile.toml`:

| `kind` | for | key |
|---|---|---|
| `anthropic` (or `claude`) | Claude | `ANTHROPIC_API_KEY` |
| `openai` (or `chatgpt`) | ChatGPT | `OPENAI_API_KEY` |
| `gemini` | Gemini, via Google's OpenAI-compatible endpoint | `GEMINI_API_KEY` |
| `openai-compatible` | OpenRouter, Groq, Together, vLLM, LM Studio — any `base_url` | `OPENAI_API_KEY` or `api_key_env` |
| `ollama` | local models | none |
| `manual` (or `paste`) | no key at all: copy the prompt out, paste the reply back | none |

Model names go stale as vendors retire them. If a provider ever says it
doesn't recognise its model, the error tells you the exact line to change —
`model = "..."` under that provider — and links to the vendor's current list.

```toml
[ai]
default = "claude"

[ai.providers.claude]
kind = "anthropic"
model = "claude-opus-5"
api_key_env = "ANTHROPIC_API_KEY"

[ai.providers.local]
kind = "ollama"
model = "llama3.3"
```

Only the SDK you actually use needs installing (`--extra anthropic`,
`--extra openai`, `--extra mcp` for the MCP server).

## Development

```bash
git clone https://github.com/palatter/garmin-plan-push
cd garmin-plan-push
uv sync --extra dev
uv run pytest
uv run ruff check gpp tests && uv run ruff format --check gpp tests
uv run gpp web
```

```
gpp/constants.py    Garmin's magic IDs, with provenance notes. The only file
                    that should need editing if Garmin renumbers something.
gpp/units.py        Duration/distance/pace parsing. SI internally.
gpp/estimate.py     Race time -> threshold pace (inverted Riegel).
gpp/models.py       VDOT, critical speed, grade-adjusted pace, pace<->power,
                    race predictions.
gpp/profile.py      Your threshold -> concrete zones; goal race, availability,
                    injuries; config read/write.
gpp/plan.py         The DSL, its JSON Schema, and the semantic validator.
gpp/compile.py      DSL -> Garmin JSON. Pure, no network, heavily tested.
gpp/checks.py       The sanity rules (the evidence-backed ones only).
gpp/load.py         Session load, weekly stats, monotony, the dashboard.
gpp/adapt.py        Pause, missed-session replan, layoff tiers.
gpp/analysis.py     Compliance, threshold re-estimation, marathon shape,
                    the daily nudge.
gpp/environment.py  Dew point, sWBGT, forecasts, daylight.
gpp/library.py      Saved sessions, plan templates, return-to-run ramps.
gpp/oneline.py      A sentence -> a workout.
gpp/formats.py      intervals.icu, ZWO, MRC/ERG, share bundles.
gpp/diff.py         What changed between two plans.
gpp/transpile.py    Pace <-> power.
gpp/timeline.py     Flattens a workout into drawable blocks.
gpp/render.py       The text preview.
gpp/providers.py    Pluggable AI backends, streaming, usage and cost.
gpp/generate.py     Ask -> validate -> compile -> check -> feed errors back.
gpp/client.py       Garmin Connect: login, upload, update, schedule, device
                    push, verify, unpush.
gpp/sync.py         Activities and daily metrics from Garmin.
gpp/history.py      The local SQLite history.
gpp/mcp_server.py   The MCP server.
gpp/watch.py        File watching.
gpp/enrich.py       Fuelling, heat, strength, durability and cadence cues by rule.
gpp/chunked.py      Long plans one mesocycle per call.
gpp/evaluate.py     The plan-quality harness and the local-model bench.
gpp/checks.py       (also) the constraint parser: day rules, keywords, "for N weeks".
gpp/fit.py          FIT workout files, encoded and decoded, no dependency.
gpp/decompile.py    Garmin JSON back into the DSL.
gpp/loadfocus.py    Garmin's three load buckets.
gpp/receipts.py     What each push created.
gpp/race.py         Splits and race-day sessions.
gpp/agenda.py       The next session and the week ahead.
gpp/education.py    What each session type does, with sources.
gpp/recent.py       Recent plans.  gpp/backup.py  Backup and restore.
gpp/icu.py          intervals.icu calendar push.
gpp/completions.py  Shell completions from the parser.
gpp/web/            Local server, background jobs, and the browser UI
                    (no build step: index.html, app.js, review.js, two CSS files).
```

446 tests cover units, zones, the models, the compiler (step ordering, repeat
groups, target units), the validator, the sanity rules, adaptation, the
formats, FIT encoding and decoding, the library, the history store, the web
endpoints and the HTTP layer, the MCP tools, the job registry's input
handshake, and the intervals.icu client up to the socket. Property-based tests
(hypothesis) cover the unit parsers and the compile round trip; an
accessibility test audits the real HTML (labels, headings, duplicate ids) and
a contrast test measures the design tokens against WCAG AA in both themes.
None touch the network. CI runs the suite on Linux, macOS and Windows on
Python 3.12 and 3.14 with a coverage floor, lints, type-checks (advisory),
runs CodeQL, dependency review and an OpenSSF Scorecard, and checks that the
web assets shipped in the wheel. Actions are pinned to commit SHAs; Dependabot
keeps them fresh. Tagging a release runs the tests, builds the wheel, takes
the notes from [CHANGELOG.md](CHANGELOG.md) and can publish to PyPI with
trusted publishing.

## Security notes

The local server handles your Garmin password, so it:

- binds `127.0.0.1` only, never `0.0.0.0`;
- requires a per-run token on every API call, injected into the page at load,
  so another site in your browser cannot drive it;
- checks the `Host` header, which is what actually stops DNS rebinding;
- uses the password for one login and never writes it to disk. Garmin's own
  OAuth token cache is the only thing that persists.

`gpp doctor --bundle` writes a diagnostics file for bug reports with versions
and the shape of your profile — no credentials, no plan contents — and never
uploads it.

## Caveats, honestly

**The Garmin API is unofficial.** `python-garminconnect` is reverse-engineered
from the Connect web app. It has been stable for years, but Garmin can change
it without notice. If that happens, `_resolve_transport()` in
[gpp/client.py](gpp/client.py) is the one place to fix.

**The constants are empirical.** Garmin publishes workout API documentation
only to Connect Developer Program partners. The IDs in
[gpp/constants.py](gpp/constants.py) come from community reverse-engineering
and carry confidence annotations. This is why every push verifies: if an ID is
wrong, or Garmin stores pace bounds the other way round, you find out then
rather than mid-interval.

**Two-level nested repeats are the least-tested path** — Connect payloads in
the wild only show one level. The verifier will tell you if the watch got
something different from what was sent.

**Automating your own account is a grey area** in Garmin's terms of service.
Fine for personal use in practice; don't build a service on it. The supported
route is the [Training API](https://developer.garmin.com/gc-developer-program/training-api/),
which requires partner approval.

**The Garmin network layer has not been run against a live account.** It is
written against the documented endpoints, and the transport probe exists
because that surface has shifted before. Everything upstream of the network —
parsing, zones, estimation, compiling, validating, the checks, adaptation,
rendering, the retry loop, the web endpoints, the job handshake — is covered
by tests and verified end to end. Sync, compliance, re-estimation and the
readiness nudge need a real account to prove.

**What is not built**, and why, is listed at the top of
[ROADMAP.md](ROADMAP.md): signed installers (paid certificates), the Courses
and training-plan APIs (schemas unverified), altitude adjustment (no formula
with evidence), and lap-alert text (a watch setting, not a workout field).
Deliberately left out on the evidence: ACWR and the 10% rule as hard gates,
cycle-phase periodization, Strava import and sleep prescriptions — the
roadmap says why, with sources.

## Alternative

[Intervals.icu](https://intervals.icu) is free, is an approved Garmin partner,
and pushes planned workouts through the official API. If you'd rather not run
any of this, `gpp export plan.json --format icu` writes plans in its text syntax — you
lose the automation and gain a supported integration.
