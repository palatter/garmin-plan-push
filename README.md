# garmin-plan-push

Describe the training you want in plain English. Get real structured workouts
on your Garmin watch.

```
"four weeks to a 10k, five runs a week"
        ↓
   an AI writes it  →  validator rejects bad plans  →  you review the chart
        ↓
   Garmin Connect  →  your watch
```

Runs as a local app in your browser. Nothing is hosted, no account to make,
and your Garmin password never leaves your machine.

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

To update later: `uv tool upgrade garmin-plan-push`

## Using it

**Setup** happens once. You don't need to know your threshold pace — give it a
race you ran hard ("10k, 47:30") and it derives everything, showing your zones
live as you type.

**Write a plan** by describing it the way you'd tell a coach: *"Four weeks to a
10k. Five runs a week, one long run Sunday, one threshold session and one set
of hills. Keep Mondays easy."*

**Review** before anything is sent. Each session is drawn as a profile — you
can see the shape of the intervals at a glance, colour-coded by effort, with
every step listed underneath.

**Send to Garmin**, enter your Connect login, and sync your watch.

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
work before you rely on them.

## Sharing it with someone

Send them the link. The repo is public, so the same two commands above are
all they need. Everything else is self-contained: no server to run, no
account, no API key required if they use the paste option.

Their profile is their own — paces, zones and plans live on their machine.

## Which watches work

Anything that supports structured workouts: **Fenix 6/7/8**, **Epix**,
**Forerunner 255/265/955/965**, **Edge 530+**. Developed against a Fenix 7s.

Older models like the Forerunner 235 or Vivoactive 3 don't accept structured
workouts at all, so this won't help there.

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
  threshold, so a model cannot invent paces.
- **Errors are fed back.** Anything the validator or compiler rejects goes
  straight back to the model as a correction turn, and only a plan that
  survives is shown to you.
- **Nothing is trusted blindly.** Garmin's workout API is undocumented outside
  their partner program, so every magic constant lives in one annotated file,
  and each push reads the workout back from Garmin and diffs it against what
  was sent.

## Re-running is safe

Every workout carries a marker in its description: `[gpp:<plan>:<hash>]`.

- same hash → left alone, reported as `unchanged`
- different hash → replaced
- **no marker → never touched**, so anything you built by hand in Garmin
  Connect is safe

## Command line

The browser app is the front door; everything is also a command.

| Command | |
|---|---|
| `gpp web` | the graphical app |
| `gpp init` | set up your profile in the terminal instead |
| `gpp doctor` | check profile, AI keys and Garmin setup (`--ping` calls each AI) |
| `gpp zones` | show your resolved pace and HR zones |
| `gpp generate "..."` | write a plan |
| `gpp show plan.json` | render a plan as text |
| `gpp push plan.json` | upload and schedule (`--dry-run` to preview) |
| `gpp compile plan.json` | emit the raw Garmin workout JSON |
| `gpp prompt` | print the prompt, to use in any chat window |

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
`--extra openai`).

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
gpp/constants.py   Garmin's magic IDs, with provenance notes. The only file
                   that should need editing if Garmin renumbers something.
gpp/units.py       Duration/distance/pace parsing. SI internally.
gpp/estimate.py    Race time -> threshold pace (inverted Riegel).
gpp/profile.py     Your threshold -> concrete zones; config read/write.
gpp/plan.py        The DSL, its JSON Schema, and the semantic validator.
gpp/compile.py     DSL -> Garmin JSON. Pure, no network, heavily tested.
gpp/timeline.py    Flattens a workout into drawable blocks.
gpp/render.py      The text preview.
gpp/providers.py   Pluggable AI backends.
gpp/generate.py    Ask -> validate -> compile -> feed errors back -> repeat.
gpp/client.py      Garmin Connect: login, upload, schedule, verify.
gpp/web/           Local server, background jobs, and the browser UI.
```

112 tests cover units, zones, race estimation, the compiler (step ordering,
repeat groups, target units), the validator, the timeline, config round-trips,
and the job registry's input handshake. None touch the network.

## Security notes

The local server handles your Garmin password, so it:

- binds `127.0.0.1` only, never `0.0.0.0`;
- requires a per-run token on every API call, injected into the page at load,
  so another site in your browser cannot drive it;
- checks the `Host` header, which is what actually stops DNS rebinding;
- uses the password for one login and never writes it to disk. Garmin's own
  OAuth token cache is the only thing that persists.

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
parsing, zones, estimation, compiling, validating, rendering, the retry loop,
the job handshake — is covered by tests and verified end to end.

## Alternative

[Intervals.icu](https://intervals.icu) is free, is an approved Garmin partner,
and pushes planned workouts through the official API. If you'd rather not run
any of this, generate plans in its text syntax and paste them in — you lose the
automation and gain a supported integration.
