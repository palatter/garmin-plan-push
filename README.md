# garmin-plan-push

Turn an AI-written running plan into real structured workouts on your Garmin
watch — validated, previewed, and scheduled on your Garmin Connect calendar.

```
describe what you want  ->  model writes a plan  ->  validator rejects bad ones
                                                           |
                            watch  <-  Garmin Connect  <-  compiler
```

Tested against a Fenix 7s. Any watch with structured-workout support (Fenix 6+,
Epix, Forerunner 255/265/955/965, Edge 530+) will work; older models like the
FR235 or Vivoactive 3 won't accept structured workouts at all.

## Why it is built this way

The Garmin half is easy. The hard part is that a language model writing Garmin's
workout JSON directly will produce plausible-looking output that is subtly
wrong — a recovery jog targeted at 5k pace, a repeat group whose children have
mismatched IDs — and you won't find out until the watch is beeping at you at
6am.

So the model never writes Garmin JSON. It writes a small DSL with human units
and named zones, which is validated hard, compiled by deterministic code, and
shown to you as text before anything is uploaded. Three properties fall out:

- **Ambiguity is designed out.** Pace bounds are `slow`/`fast`, never
  `low`/`high`, because "low pace" is genuinely ambiguous and a model gets it
  wrong about half the time. A step takes exactly one of duration/distance/lap.
  Zone names are a closed set resolved from *your* profile, so the model cannot
  invent paces.
- **Errors are fed back.** Anything the validator or compiler rejects goes
  straight back to the model as a correction turn, up to `--attempts` times.
- **Nothing is trusted blindly.** Garmin's workout API is undocumented outside
  their partner program, so every magic constant lives in one file and the
  push step reads each workout back and diffs it against what was sent.

## Install

```bash
cd garmin-plan-push && uv sync --extra dev --extra anthropic
```

Then copy the example profile and set your threshold pace:

```bash
cp examples/profile.toml profile.toml
```

`threshold` is roughly what you could hold for a hard hour — 10k pace plus
10–15 s/km is a decent estimate. Every pace zone derives from it, so it is
worth re-checking every 6–8 weeks. Check what you got:

```bash
uv run gpp zones
```

## Use

```bash
# have an AI write a plan
uv run gpp generate "4 weeks to a 10k, 5 runs a week, long run Sunday" -o week.json

# read it before it goes anywhere near your watch
uv run gpp show week.json

# upload and schedule
uv run gpp push week.json
```

`push` renders the plan, asks for confirmation, uploads, schedules each workout
on its date, and reads each one back to verify. Then sync your watch.

| Command | What it does |
|---|---|
| `gpp zones` | Show your resolved pace and HR zones |
| `gpp providers` | List configured AI providers |
| `gpp generate` | Have a model write a plan, with validation retries |
| `gpp prompt` | Print the prompt, to paste into any chat window yourself |
| `gpp check` | Validate a plan file |
| `gpp show` | Render a plan as text (no network) |
| `gpp compile` | Emit the raw Garmin workout JSON |
| `gpp push` | Upload and schedule (`--dry-run` to preview) |

## AI providers

Plan generation is provider-neutral — the DSL is the contract, so switching
models is a one-word change. Configure under `[ai.providers]` in your profile:

| `kind` | Uses | For |
|---|---|---|
| `anthropic` | `anthropic` SDK | Claude |
| `openai` | `openai` SDK | OpenAI |
| `openai-compatible` | `openai` SDK + your `base_url` | OpenRouter, Groq, Together, vLLM, LM Studio |
| `ollama` | same, `localhost:11434/v1` | local models |
| `manual` | nothing | writes the prompt to a file, you paste the reply back |

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

Then `gpp generate --provider local "..."`. The `manual` provider needs no API
key or SDK at all — useful with a chat window you already have open. Only the
SDK you actually use needs installing (`--extra anthropic`, `--extra openai`).

## The plan format

```json
{
  "plan": "Autumn 10k block",
  "workouts": [{
    "name": "Threshold 5x1k",
    "date": "2026-09-24",
    "notes": "Comfortably hard, not a race.",
    "steps": [
      {"kind": "warmup", "duration": "15m", "target": {"type": "pace", "zone": "easy"}},
      {"kind": "repeat", "reps": 5, "steps": [
        {"kind": "run", "distance": "1km", "target": {"type": "pace", "zone": "threshold"}},
        {"kind": "recover", "duration": "90s", "target": {"type": "pace", "zone": "recovery"}}
      ]},
      {"kind": "cooldown", "duration": "10m", "target": {"type": "pace", "zone": "easy"}}
    ]
  }]
}
```

Step kinds: `warmup`, `run`, `recover`, `rest`, `cooldown`, `repeat`.
Each non-repeat step takes exactly one of `duration`, `distance`, or
`"until": "lap"`. Targets are `pace` (zone or slow/fast range), `hr` (zone or
low/high bpm), `cadence`, or `none`.

## Re-pushing is safe

Every workout carries a marker in its description: `[gpp:<plan>:<hash>]`. On
push, workouts already on that date are matched against it:

- same hash → left alone, reported as `unchanged`
- different hash → replaced
- **no marker → never touched**, so anything you built by hand in Garmin
  Connect is safe

## Layout

```
gpp/constants.py   Garmin's magic IDs, with provenance notes. The only file
                   that should need editing if Garmin renumbers something.
gpp/units.py       Duration/distance/pace parsing. SI internally.
gpp/profile.py     Your threshold pace -> concrete zones.
gpp/plan.py        The DSL, its JSON Schema, and the semantic validator.
gpp/compile.py     DSL -> Garmin JSON. Pure, no network, heavily tested.
gpp/render.py      The text preview you approve before pushing.
gpp/providers.py   Pluggable AI backends.
gpp/generate.py    Ask -> validate -> compile -> feed errors back -> repeat.
gpp/client.py      Garmin Connect: login, upload, schedule, verify.
gpp/cli.py         Commands.
```

```bash
uv run pytest
```

67 tests cover units, zones, the compiler (step ordering, repeat groups, target
units), the validator, and the generation retry loop. None of them touch the
network.

## Caveats, honestly

**The Garmin API is unofficial.** `python-garminconnect` is reverse-engineered
from the Connect web app. It has been stable for years, but Garmin can change
it without notice. If that happens, `_resolve_transport()` in
[gpp/client.py](gpp/client.py) is the one place to fix.

**The constants are empirical.** Garmin publishes workout API documentation only
to Connect Developer Program partners. The IDs in
[gpp/constants.py](gpp/constants.py) come from community reverse-engineering
and are annotated with confidence levels. This is exactly why `push` verifies:
if an ID is wrong, or Garmin stores pace bounds the other way round, the
verifier says so instead of letting you find out mid-interval.

**Automating your own account is a grey area** in Garmin's terms of service.
Fine for personal use in practice; don't build a service on it. The supported
route is the [Training API](https://developer.garmin.com/gc-developer-program/training-api/),
which requires partner approval.

**The network layer has not been run against a live account** — it is written
against the documented endpoints and the library's API surface, and the
transport probe exists precisely because that surface has shifted before.
Everything upstream of the network (parsing, zones, compiling, validating,
rendering, the retry loop) is covered by tests and verified end-to-end.

## Alternative

[Intervals.icu](https://intervals.icu) is free, is an approved Garmin partner,
and pushes planned workouts to Connect through the official API. If you'd
rather not run any of this, generate plans in its text syntax and paste them
in — you lose the automation, and gain a supported integration.
