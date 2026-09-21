# Roadmap

Fifty candidate features, from research into what comparable tools do, what
the Garmin platform exposes that we don't touch, and what the training-science
literature actually supports.

Tiered by value-per-effort, not by excitement. **Effort** is S (hours), M (a
day or two), L (a week+), XL (a project). Sources are cited where a claim is
load-bearing.

Read [§ Deliberately not doing](#deliberately-not-doing) before picking work —
two of the most-requested features in this category are not supported by the
evidence, and we should not ship them.

---

## Tier 1 — Do next

High value, low effort, no new subsystems.

| # | Feature | Effort | Why |
|---|---|---|---|
| 1 | **Power targets** (`power.zone`, id 2) | S | Constants already verified; Fenix 7s supports running power. One `TARGET_TYPES` entry plus a DSL target kind. Unlocks Stryd users entirely. |
| 2 | **Undo the last push** | S | We already tag every workout and can list them. "Remove the plan I just sent" is one call over the tag; today the only way back is Garmin's UI. |
| 3 | ~~**`gpp doctor`**~~ **done** | S | One command that checks: profile valid, provider reachable, Garmin login works, watch model supports structured workouts. Every support question a friend will ask, answered before they ask it. |
| 4 | **Edit a workout before pushing** | M | The review screen is read-only. Letting someone drag a session to another day, or bump reps 5→4, removes the "regenerate the whole block because Thursday is wrong" loop. |
| 5 | **Plan templates saved locally** | M | Final Surge treats plans as first-class objects separate from the calendar, reapplicable to any date ([support.finalsurge.com](https://support.finalsurge.com/hc/en-us/articles/4408535325207-Creating-and-Editing-Training-Plans)). We regenerate from scratch every time. |
| 6 | **Workout library** | M | Save "my 5×1k" and reuse it. Pairs with #5 and is the unit people actually think in. |
| 7 | **Race-date anchoring** | M | Today you say "four weeks to a 10k" in prose and hope. A real race date field lets the generator count backwards and lets #12 (taper) exist at all. |
| 8 | **Lazy-import `jsonschema`** | S | Measured: 136 ms of our 210 ms CLI import. The schema *dict* is needed at import (we feed it to the LLM); the *validator* is not. Nearly halves `gpp --help`. |
| 9 | ~~**A `LICENSE`**~~ **done (MIT)** | S | Currently absent. Nobody can legally use or fork this, which matters the moment you share it. |
| 10 | **Long-run spike guardrail** | S | See #17. Strongest evidence of any safety rule here, and it's a pure function of the plan — no history needed if the user enters their recent longest run. |

---

## Tier 2 — High value, real work

| # | Feature | Effort | Why |
|---|---|---|---|
| 11 | **Read completed activities from Garmin** | L | The keystone. Everything adaptive below depends on it. Unofficial API exposes activity lists and details. |
| 12 | **Taper generation** | M | Needs #7. The evidence here is unusually clear: Bosquet et al.'s meta-analysis (27 studies) found the optimal taper cuts volume **41–60%** over **~2 weeks** while **keeping intensity and frequency**, for a ~2.2% performance gain — and cutting more than 60% performs *worse* ([Semantic Scholar](https://www.semanticscholar.org/paper/Effects-of-tapering-on-performance:-a-Bosquet-Montpetit/a41517ab5fa06b92568b861e2b1aa32b3003d214)). Those four numbers are the whole spec. |
| 13 | **Deload weeks** | M | 3:1 or 2:1 loading patterns. Cheap to generate, and the single most common thing missing from naive LLM-written plans. |
| 14 | **Compliance review** | L | Needs #11. Final Surge colour-codes planned vs actual duration with user-tunable thresholds (green 80–120%, yellow 50–79%/121–150%, red outside) ([blog.finalsurge.com](https://blog.finalsurge.com/workout-completion-color-coding/)). Concrete and copyable. |
| 15 | **Re-estimate threshold from recent runs** | L | Needs #11. Our threshold is entered once and rots. Stryd recomputes Critical Power on every upload ([blog.stryd.com](https://blog.stryd.com/2019/07/09/introducing-auto-calculated-critical-power/)); COROS rewrites zones from a field test and re-scales already-scheduled workouts ([coros.com](https://coros.com/stories/coros-metrics/c/running-fitness-test)). |
| 16 | **Targets stored as % of threshold, not absolute paces** | M | Stryd stores every plan workout as a % of CP so a CP change rewrites all future targets. We bake absolute paces at compile time, so updating your threshold silently leaves old workouts wrong. This is a data-model fix and gets harder the longer it waits. |
| 17 | **Availability-aware scheduling** | L | "I can run Tue/Thu/Sat, 45 min max on weekdays." Humango captures this at onboarding and replans within the constraints ([humango.ai/faqs](https://humango.ai/faqs)). Turns a generic block into *your* block. |
| 18 | **Missed-session replan** | L | Needs #11. The single most-requested feature across every tool researched, and the one every tool is criticised for doing badly. |
| 19 | **Layoff rules** | M | Stryd publishes explicit tiers: 0–7 days skip and resume; 8–14 restart the phase; 15–28 back to Aerobic; 29+ restart at Foundation ([help.stryd.com](https://help.stryd.com/en/articles/12580285-stryd-adaptive-training-how-to)). Rules, not ML — implementable today. |
| 20 | **Plan-level system prompt** | S | AI Endurance keeps persistent free-text constraints the generator always respects ("no running Mondays", "I hate track work"). Ours are one-shot and forgotten. |
| 21 | **A/B/C race hierarchy with taper protection** | M | AI Endurance forbids B/C races inside the 14 days before an A race ([aiendurance.com/en/faq](https://aiendurance.com/en/faq)). A hard constraint that prevents a real, common mistake. |
| 22 | **Structured strength / mobility sessions** | M | Garmin has an exercise catalog (category + name enums, `reps` end condition id 10). Runners do need the gym, and today we can't express it. |
| 23 | **SQLite for history** | M | stdlib, no new dependency. Needed once #11 lands. Do it when there's history to store, not before. |
| 24 | **Weather-aware pace adjustment** | M | Stryd adjusts race targets for local temperature and humidity ([help.stryd.com](https://help.stryd.com/en/articles/6879547-race-power-calculator)). Heat genuinely changes achievable pace; a plan that ignores it prescribes impossible sessions in August. |

---

## Tier 3 — Worthwhile later

| # | Feature | Effort |
|---|---|---|
| 25 | Multi-sport (bike/swim) — sport constants already exist | M |
| 26 | Course/route attachment via the Courses API | L |
| 27 | Push a plan as a Garmin *training plan* object, not loose workouts | L |
| 28 | Read Garmin's own HR zones instead of deriving from LTHR | M |
| 29 | Read VO2max / race predictor as a sanity check on entered threshold | M |
| 30 | Grade-adjusted pace for hilly routes | L |
| 31 | Export to TrainingPeaks / intervals.icu / Final Surge | M |
| 32 | Import an existing plan (reverse of export) | L |
| 33 | Pace→power transpilation of a pace-based plan | M |
| 34 | Plan sharing — publish a block for a friend to import | M |
| 35 | VDOT / Daniels tables as an alternative zone model — note VDOT **underestimates VO₂max in recreational runners** (d = 3.44, [PubMed](https://pubmed.ncbi.nlm.nih.gov/28426511/)); the *paces* remain useful, the VO₂ number shouldn't be shown | M |
| 36 | Critical-speed model as an alternative to threshold multipliers | L |
| 37 | Pain & injury log (Final Surge's PAIR) | S |
| 38 | Session RPE capture after each run | S |
| 39 | Calendar view instead of a flat list | M |
| 40 | Drag-to-reschedule in the UI, with re-push | M |
| 41 | Per-workout regenerate ("rewrite just Thursday") | M |
| 42 | Diff view — what changed between plan versions | M |
| 43 | Multi-athlete profiles on one install | S |
| 44 | Localisation / non-English plan generation | M |
| 45 | `gpp watch` — re-push automatically when the plan file changes | S |
| 46 | Signed desktop installers (see below) | XL |
| 47 | Microsoft Store distribution | L |
| 48 | Streaming generation so plans appear as they're written | M |
| 49 | Cost/token display per generation | S |
| 50 | Local model quality presets (which local models are good enough) | M |

---

## Deliberately not doing

Research turned up two features that are near-universal in this category and
that we should **not** build. Including them would be following fashion
against the evidence.

### ACWR (acute:chronic workload ratio)

Do not implement. Impellizzeri et al. (2020) conclude there is *"no evidence
supporting the use of ACWR in training-load-management systems or for training
recommendations aimed at reducing injury risk"*, citing mathematical coupling,
arbitrary time windows with no rationale, and the ratio's statistical
properties producing artifacts
([IJSPP 15(6)](https://journals.humankinetics.com/view/journals/ijspp/15/6/article-p907.xml),
[editorial](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC8138569/)).
Shipping a red "injury risk" badge computed from a debunked ratio would be
worse than shipping nothing — it looks authoritative and isn't.

### The 10% rule

Do not enforce. It was never validated in a trial; it comes from coaching
consensus. Nielsen et al. (2014) found no significant difference in injury
risk between runners increasing weekly distance by <10%, 10–30%, or >30%
([JOSPT](https://www.jospt.org/doi/10.2519/jospt.2014.5164)), and a 2022
review of 36 studies found inconsistent relationships between injury and
weekly distance or recent changes in it.

**Use this instead (#10, #17):** an 18-month cohort of >5,200 runners found
significantly higher overuse-injury risk when a *single run* exceeded ~110% of
the longest run in the previous 30 days. That is a better-supported rule, it
targets the thing that actually varies, and it is cheap to check at plan-
generation time. Warn, don't block.

### Guardrails the evidence *does* support

Three rules with real backing, all cheap to check at generation time:

1. **Long-run spike** (#10) — warn when a single run exceeds ~110% of the
   longest in the prior 30 days. Best-supported injury signal found.
2. **Taper shape** (#12) — 41–60% volume cut over ~2 weeks, intensity and
   frequency held. Warn if a generated taper cuts frequency or exceeds 60%.
3. **Intensity distribution** — roughly 80% easy / 20% hard by time.
   Polarized training shows a small but significant VO₂ advantage across 17
   studies and 437 athletes (Oliveira et al., *Sports Medicine* 2024), though
   the broader literature does **not** crown one universal winner
   ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC11679080/)). So: warn when
   a plan's hard time exceeds ~30%, but don't enforce a ratio — the honest
   reading is "mostly easy is well supported; the exact split is not."

### What we should copy instead

Every adaptive tool researched is criticised for the same thing — the
adaptation doesn't work. AI Endurance's top App Store complaint: *"If I have
to manually correct the intensity myself, the AI isn't doing the job I'm
paying it for."* COROS rescales intensity but makes you drag missed sessions
by hand. Humango's AI coach was called *"laughably bad"* in a user forum.

The lesson: **a small number of transparent, explainable rules (#13, #19, #21)
will beat an opaque adaptive model**, and they're an order of magnitude less
work. State the rule, show the user why the plan changed, let them override.

---

## Tech stack assessment

Measured, not assumed.

### Keep: stdlib HTTP server

22 installed packages total; `gpp --help` runs in 278 ms. FastAPI or Litestar
would add dependencies and startup cost to serve ~8 endpoints to one user on
loopback. There is no concurrency story we need — `ThreadingHTTPServer` plus
the job registry already handles the only two slow operations. **No change.**

### Keep: no-build frontend

The assets must ship inside a Python wheel and work offline with no CDN; CI
already asserts they're in the wheel. A Vite/React toolchain would add a Node
build step to a Python project for a three-file UI. If the frontend grows past
roughly double its current size, revisit with Preact via ESM import maps
(no bundler) before reaching for a full framework. **No change.**

### Change: lazy-import `jsonschema` (#8)

Measured with `-X importtime`: `jsonschema` is 136 ms of the 210 ms it takes
to import `gpp.cli`. We need the schema dict at import time to feed the LLM;
we need the validator only inside `validate()`. **Move the import.**

### Add: `ruff`, and a `[tool.ruff]` section

No linter or formatter is configured. Ruff is the 2026 default, one
dependency, fast. **Add.**

### Added already: CI

Cross-platform matrix (3 OSes × Python 3.11/3.13), CLI smoke tests, a token-gate
assertion, and a wheel-contents check — the web assets live outside `.py`
files, so a packaging regression would pass every unit test and break every
fresh install.

### Do not adopt: a desktop framework

Tauri, Electron and pywebview all solve "render HTML in a native window" — a
problem we don't have, because we already open the user's browser. Tauri would
add a mandatory Rust toolchain, a per-platform build matrix (no
cross-compilation), and a process supervisor. Electron would add ~150 MB of
Chromium to display 49 KB of HTML.

**If** a double-clickable installer becomes the goal, the answer is a frozen
binary plus an installer, not a framework — Briefcase 0.4.5+ is purpose-built
for it and automates macOS notarization. But price it honestly first:

- Apple Developer Program **$99/yr** — effectively mandatory on macOS, because
  right-click→Open was removed in macOS 15 and an unsigned app now requires a
  System Settings excursion with no "Open" button in the first dialog.
- Azure Artifact Signing **$9.99/mo** — but the individual tier is **US/Canada
  only**, and Microsoft now states outright that *EV certificates no longer
  bypass SmartScreen*, so signing buys you reputation continuity across
  releases, not a warning-free first install.
- **~15–30 hours** of first-time CI wiring.

That's ~$220/yr and a working week to remove one `uv tool install` line. Worth
it only if you're shipping to strangers.

---

## Coverage gaps in this research

Six of eight research streams died on a session rate limit and their findings
are **not** reflected above. What's missing:

- **Garmin platform deep-dive** — the read-side (training status, HRV, Body
  Battery, race predictor) and Courses API are sketched from general knowledge,
  not verified endpoint-by-endpoint. Items #26–#30 are lower-confidence.
- **intervals.icu / TrainingPeaks / Runna / Garmin Coach** — the competitor
  set here is Final Surge, Stryd, COROS, Humango and AI Endurance only.
  intervals.icu especially is a gap, since it's the closest free analogue.
- **Open-source prior art** — GoldenCheetah, Runalyze, existing Garmin workout
  builders. Likely contains directly reusable constant tables.
- **Pfitzinger / critical-speed methodology** — #36 is named but not
  specified. (Daniels/VDOT, taper, and intensity distribution were
  subsequently researched directly and are reflected above.)

Also: Reddit was unreachable from the research environment, so the "what do
runners actually complain about" signal comes from App Store reviews,
Trustpilot and vendor forums. Several of those samples are small (Stryd n=24,
Humango n=21, AI Endurance n=24) and indicate failure *modes*, not prevalence.
