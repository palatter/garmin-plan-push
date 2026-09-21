# Roadmap

One hundred candidate features in two batches, from research into what
comparable tools do, what the Garmin platform exposes that we don't touch, and
what the training-science literature actually supports. The [second fifty](#second-fifty)
came from a follow-up pass that closed most of the first batch's research gaps.

Tiered by value-per-effort, not by excitement. **Effort** is S (hours), M (a
day or two), L (a week+), XL (a project). Sources are cited where a claim is
load-bearing.

Read [§ Deliberately not doing](#deliberately-not-doing) before picking work —
two of the most-requested features in this category are not supported by the
evidence, and we should not ship them.

## Status

As of 2026-09-21, **86 of the 100 items are built** and covered by tests,
**7 are partial** and **7 are deliberately not built**. Marks in the tables
below: ✅ built · ◐ partial · ✗ not built, each with the reason.

**Partial**

- **#31** — intervals.icu text export is done. TrainingPeaks and Final Surge
  have no open plan-import format; their structured-workout import is served
  by the ZWO / MRC / ERG files instead.
- **#44** — plans can be generated in another language (`language` in the
  profile); the app's own screens are English only.
- **#71** — Garmin's running tolerance is synced and stored; it is not yet
  used as a volume ceiling when generating.
- **#72** — `gpp today` nudges toward an easier day after a bad night; it
  does not move sessions on its own.
- **#83** — `gpp suggestion` shows Garmin's Daily Suggested Workout beside the
  plan's session; push does not yet warn about the clash automatically.
- **#86** — ZWO, MRC and ERG export are done; FIT workout files are not. The
  binary format needs a library or a careful encoder, and USB sideload is the
  fallback nobody needs once push works.
- **#99** — a Homebrew formula and a winget manifest sit in `packaging/`, not
  yet submitted to a tap or to winget-pkgs.

**Not built, and why**

- **#26 Courses API** — the endpoint's shape is unverified; building on a
  guess would fail on the first real push.
- **#27 / #81 Garmin training-plan object** — same: the library exposes the
  calls, the schema is undocumented.
- **#46 signed desktop installers** — an Apple Developer account ($99/yr) and
  a code-signing subscription; see the stack assessment below. `uv tool
  install` is the install path.
- **#47 Microsoft Store** — depends on #46.
- **#75 altitude adjustment** — no formula with evidence was found; listed so
  nobody builds on a guess.
- **#91 lap-alert text** — a watch setting, not a field on a workout; there
  is nothing to push.

**Built, but only provable against a live Garmin account** — the network
layer is written against the documented calls and probed at runtime, and none
of the tests touch the network: #2 unpush, #11 activity sync, #14 compliance,
#15 re-estimation, #28 HR zones, #29 the race predictor, #64 marathon shape,
#67 / #68 the readiness nudge, #77–#80 metrics, in-place update,
push-to-device and the exercise catalog, #83 suggestions.

---

## Tier 1 — Do next

High value, low effort, no new subsystems.

| # | Feature | Effort | Why |
|---|---|---|---|
| 1 | ✅ **Power targets** (`power.zone`, id 2) | S | Constants already verified; Fenix 7s supports running power. One `TARGET_TYPES` entry plus a DSL target kind. Unlocks Stryd users entirely. |
| 2 | ✅ **Undo the last push** | S | We already tag every workout and can list them. "Remove the plan I just sent" is one call over the tag; today the only way back is Garmin's UI. |
| 3 | ~~**`gpp doctor`**~~ **done** | S | One command that checks: profile valid, provider reachable, Garmin login works, watch model supports structured workouts. Every support question a friend will ask, answered before they ask it. |
| 4 | ✅ **Edit a workout before pushing** | M | The review screen is read-only. Letting someone drag a session to another day, or bump reps 5→4, removes the "regenerate the whole block because Thursday is wrong" loop. |
| 5 | ✅ **Plan templates saved locally** | M | Final Surge treats plans as first-class objects separate from the calendar, reapplicable to any date ([support.finalsurge.com](https://support.finalsurge.com/hc/en-us/articles/4408535325207-Creating-and-Editing-Training-Plans)). We regenerate from scratch every time. |
| 6 | ✅ **Workout library** | M | Save "my 5×1k" and reuse it. Pairs with #5 and is the unit people actually think in. |
| 7 | ✅ **Race-date anchoring** | M | Today you say "four weeks to a 10k" in prose and hope. A real race date field lets the generator count backwards and lets #12 (taper) exist at all. |
| 8 | ✅ **Lazy-import `jsonschema`** | S | Measured: 136 ms of our 210 ms CLI import. The schema *dict* is needed at import (we feed it to the LLM); the *validator* is not. Nearly halves `gpp --help`. |
| 9 | ~~**A `LICENSE`**~~ **done (MIT)** | S | Currently absent. Nobody can legally use or fork this, which matters the moment you share it. |
| 10 | ✅ **Long-run spike guardrail** | S | See #17. Strongest evidence of any safety rule here, and it's a pure function of the plan — no history needed if the user enters their recent longest run. |

---

## Tier 2 — High value, real work

| # | Feature | Effort | Why |
|---|---|---|---|
| 11 | ✅ **Read completed activities from Garmin** | L | The keystone. Everything adaptive below depends on it. Unofficial API exposes activity lists and details. |
| 12 | ✅ **Taper generation** | M | Needs #7. The evidence here is unusually clear: Bosquet et al.'s meta-analysis (27 studies) found the optimal taper cuts volume **41–60%** over **~2 weeks** while **keeping intensity and frequency**, for a ~2.2% performance gain — and cutting more than 60% performs *worse* ([Semantic Scholar](https://www.semanticscholar.org/paper/Effects-of-tapering-on-performance:-a-Bosquet-Montpetit/a41517ab5fa06b92568b861e2b1aa32b3003d214)). Those four numbers are the whole spec. |
| 13 | ✅ **Deload weeks** | M | 3:1 or 2:1 loading patterns. Cheap to generate, and the single most common thing missing from naive LLM-written plans. |
| 14 | ✅ **Compliance review** | L | Needs #11. Final Surge colour-codes planned vs actual duration with user-tunable thresholds (green 80–120%, yellow 50–79%/121–150%, red outside) ([blog.finalsurge.com](https://blog.finalsurge.com/workout-completion-color-coding/)). Concrete and copyable. |
| 15 | ✅ **Re-estimate threshold from recent runs** | L | Needs #11. Our threshold is entered once and rots. Stryd recomputes Critical Power on every upload ([blog.stryd.com](https://blog.stryd.com/2019/07/09/introducing-auto-calculated-critical-power/)); COROS rewrites zones from a field test and re-scales already-scheduled workouts ([coros.com](https://coros.com/stories/coros-metrics/c/running-fitness-test)). |
| 16 | ✅ **Targets stored as % of threshold, not absolute paces** | M | Stryd stores every plan workout as a % of CP so a CP change rewrites all future targets. We bake absolute paces at compile time, so updating your threshold silently leaves old workouts wrong. This is a data-model fix and gets harder the longer it waits. |
| 17 | ✅ **Availability-aware scheduling** | L | "I can run Tue/Thu/Sat, 45 min max on weekdays." Humango captures this at onboarding and replans within the constraints ([humango.ai/faqs](https://humango.ai/faqs)). Turns a generic block into *your* block. |
| 18 | ✅ **Missed-session replan** | L | Needs #11. The single most-requested feature across every tool researched, and the one every tool is criticised for doing badly. |
| 19 | ✅ **Layoff rules** | M | Stryd publishes explicit tiers: 0–7 days skip and resume; 8–14 restart the phase; 15–28 back to Aerobic; 29+ restart at Foundation ([help.stryd.com](https://help.stryd.com/en/articles/12580285-stryd-adaptive-training-how-to)). Rules, not ML — implementable today. |
| 20 | ✅ **Plan-level system prompt** | S | AI Endurance keeps persistent free-text constraints the generator always respects ("no running Mondays", "I hate track work"). Ours are one-shot and forgotten. |
| 21 | ✅ **A/B/C race hierarchy with taper protection** | M | AI Endurance forbids B/C races inside the 14 days before an A race ([aiendurance.com/en/faq](https://aiendurance.com/en/faq)). A hard constraint that prevents a real, common mistake. |
| 22 | ✅ **Structured strength / mobility sessions** | M | Garmin has an exercise catalog (category + name enums, `reps` end condition id 10). Runners do need the gym, and today we can't express it. |
| 23 | ✅ **SQLite for history** | M | stdlib, no new dependency. Needed once #11 lands. Do it when there's history to store, not before. |
| 24 | ✅ **Weather-aware pace adjustment** | M | Stryd adjusts race targets for local temperature and humidity ([help.stryd.com](https://help.stryd.com/en/articles/6879547-race-power-calculator)). Heat genuinely changes achievable pace; a plan that ignores it prescribes impossible sessions in August. |

---

## Tier 3 — Worthwhile later

| # | Feature | Effort |
|---|---|---|
| 25 | ✅ Multi-sport (bike/swim) — sport constants already exist | M |
| 26 | ✗ Course/route attachment via the Courses API | L |
| 27 | ✗ Push a plan as a Garmin *training plan* object, not loose workouts | L |
| 28 | ✅ Read Garmin's own HR zones instead of deriving from LTHR | M |
| 29 | ✅ Read VO2max / race predictor as a sanity check on entered threshold | M |
| 30 | ✅ Grade-adjusted pace for hilly routes | L |
| 31 | ◐ Export to TrainingPeaks / intervals.icu / Final Surge | M |
| 32 | ✅ Import an existing plan (reverse of export) | L |
| 33 | ✅ Pace→power transpilation of a pace-based plan | M |
| 34 | ✅ Plan sharing — publish a block for a friend to import | M |
| 35 | ✅ VDOT / Daniels tables as an alternative zone model — note VDOT **underestimates VO₂max in recreational runners** (d = 3.44, [PubMed](https://pubmed.ncbi.nlm.nih.gov/28426511/)); the *paces* remain useful, the VO₂ number shouldn't be shown | M |
| 36 | ✅ Critical-speed model as an alternative to threshold multipliers | L |
| 37 | ✅ Pain & injury log (Final Surge's PAIR) | S |
| 38 | ✅ Session RPE capture after each run | S |
| 39 | ✅ Calendar view instead of a flat list | M |
| 40 | ✅ Drag-to-reschedule in the UI, with re-push | M |
| 41 | ✅ Per-workout regenerate ("rewrite just Thursday") | M |
| 42 | ✅ Diff view — what changed between plan versions | M |
| 43 | ✅ Multi-athlete profiles on one install | S |
| 44 | ◐ Localisation / non-English plan generation | M |
| 45 | ✅ `gpp watch` — re-push automatically when the plan file changes | S |
| 46 | ✗ Signed desktop installers (see below) | XL |
| 47 | ✗ Microsoft Store distribution | L |
| 48 | ✅ Streaming generation so plans appear as they're written | M |
| 49 | ✅ Cost/token display per generation | S |
| 50 | ✅ Local model quality presets (which local models are good enough) | M |

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

## Second fifty

From the follow-up research: intervals.icu, TrainingPeaks, Runna and Garmin
Run Coach, Runalyze and GoldenCheetah, Pfitzinger's plan structure, the
critical-speed literature, heat-adjustment formulas, HRV-guided training
trials, `python-garminconnect`'s read-side, and — most usefully — the
documented ways AI-generated plans go wrong. Same tiers and effort scale.

### Plan sanity — the ways AI plans actually fail

A coaching-expert evaluation of ChatGPT-written plans found they *"increased
training variables too rapidly, violating individual progression principles"*,
that quality *rises with the amount of input information provided*, and that
runners describe them as swinging *"between very mild or incredibly intense,
with no middle ground"* ([PMC](https://pmc.ncbi.nlm.nih.gov/articles/PMC10915606),
[Third Coast](https://thirdcoasttraining.com/why-ai-running-plans-fail-what-runners-need-to-know/)).
Every item here is a check we can run on the plan *before* it reaches a watch.

| # | Feature | Tier | Effort |
|---|---|---|---|
| 51 | ✅ **Plan sanity report** shown on the review screen: ramp too fast, no rest days, hard sessions back-to-back, no moderate sessions. The single highest-leverage item in this batch — it turns the documented failure modes into a checklist. | 1 | M |
| 52 | ✅ **Injury history and constraints in the profile**, fed to every prompt. The study's clearest finding: more input, better plan. "Left Achilles, no hills for 6 weeks" should never have to be retyped. | 1 | S |
| 53 | ✅ **Consecutive-hard-days rule** — flag two quality sessions in a row unless the user declared a double day. | 1 | S |
| 54 | ✅ **"Middle gear" check** — warn when a plan has no steady/moderate sessions at all, the mild-or-brutal pattern runners complain about. | 1 | S |
| 55 | ✅ **Monotony index** (Runalyze / Foster): variance of daily load over 7 days. The same load every day is the pattern that precedes overtraining ([Runalyze](https://appsforstrava.com/app/runalyze)). Computable on the plan itself, no history needed. | 2 | S |
| 56 | ✅ **Per-session-type progression cap** — e.g. threshold minutes week over week — as a warning, not a block. | 2 | S |
| 57 | ✅ **Weekly structure lint** (Pfitzinger): a recovery day after each quality day; strides present in base weeks; a medium-long run midweek in marathon blocks. | 2 | M |
| 58 | ✅ **Return-to-run templates**: Pfitzinger's five-week post-race recovery, and a post-injury ramp using the Stryd layoff tiers (#19). | 2 | M |

### Periodization and methodology

| # | Feature | Tier | Effort |
|---|---|---|---|
| 59 | ✅ **Mesocycle scaffolding** the generator must fill: Pfitzinger's five — mileage establishment → lactate-threshold endurance → race preparation (VO₂) → taper → recovery ([Running With Rock](https://runningwithrock.com/pfitz-marathon-training-explained/)). A skeleton with phase goals beats "write me 16 weeks". | 2 | M |
| 60 | ✅ **Medium-long run** as a named session type (~90–120 min midweek). | 2 | S |
| 61 | ✅ **Threshold-embedded long runs** ("10 miles with 5 at 15k–HM pace") in the workout library — the DSL already expresses it; it needs to be a one-click template. | 2 | S |
| 62 | ✅ **Strides** as a first-class step: short, fast, full recovery, no target. Naive plans omit them entirely. | 1 | S |
| 63 | ✅ **Critical-speed profile option**: two all-out trials (e.g. 5 min and 20 min) → CS and D′ by linear regression. A 2-point model is as good as 3-point with trivial bias ([PubMed](https://pubmed.ncbi.nlm.nih.gov/30427230/)). A field test, not a race, for people who don't race. | 2 | M |
| 64 | ✅ **Marathon Shape** (Runalyze): weighted 6-month weekly mileage (⅔) + 10-week long runs (⅓) → "do you have the endurance for this distance". Needs #11. | 3 | M |
| 65 | ✅ **VDOT-style zone names** (E / M / T / I / R) as an alternative labelling of our bands, since intervals.icu users think in them. | 3 | S |
| 66 | ✅ **Race-time predictions across distances** with honest error bars, from CS or VDOT. | 3 | M |

### Adaptation signals — only what the evidence supports

| # | Feature | Tier | Effort |
|---|---|---|---|
| 67 | ✅ **HRV-guided downgrade** of a quality session when morning HRV is below baseline. Evidence is real but modest: an 8-week RCT in professional runners (n = 12) found small-to-medium effects on submaximal parameters ([ScienceDirect](https://www.sciencedirect.com/science/article/abs/pii/S0031938421003413)); the meta-analysis is cautious. Ship as a *suggestion with the reason shown*, never an automatic rewrite. | 3 | M |
| 68 | ✅ **Training Readiness gate** — read Garmin's own score and nudge "today's session looks too hard for how you slept". Garmin Run Coach already adjusts daily on sleep, HRV and readiness. | 3 | M |
| 69 | ✅ **Illness and holiday modes** (Runna) — explicit pause, with the layoff tiers applied on return. | 2 | M |
| 70 | ✅ **"Readapt or keep?" prompt** after a missed run, Runna's exact UX ([Runna vs Garmin Coach](https://www.runningwestwardho.co.uk/post/runna-vs-garmin-coach)). Pairs with #18. | 2 | M |
| 71 | ◐ **Running Tolerance** (Garmin metric) as a volume-ceiling hint when generating. | 3 | M |
| 72 | ◐ **Sleep-aware day swap** — move a quality session by a day when the night before was bad. | 3 | L |

### Environment

| # | Feature | Tier | Effort |
|---|---|---|---|
| 73 | ✅ **Dew-point pace adjustment**, the rule of thumb coaches actually use: add 0.025 min/mile per °F of dew point above 60 °F; or temperature + dew point ≥ 100 → adjust, ≥ 180 → no hard running ([Runners Connect](https://runnersconnect.net/training/tools/temperature-calculator/)). Needs no history — a forecast or a typed number. | 1 | S |
| 74 | ✅ **sWBGT race-day pacing** from a weather API: `0.567·T + 0.393·e + 3.94`, applied to published marathon performance curves from 7,867 athletes across 1,258 races ([Running Writings](https://apps.runningwritings.com/heat-adjusted-pace/)). | 3 | M |
| 75 | ✗ **Altitude adjustment** — *not researched*: this pass found heat evidence but no comparable altitude formula. Listed so it isn't forgotten, not so it gets built on a guess. | — | — |
| 76 | ✅ **Daylight-aware scheduling** for early or late runs. A nicety. | 3 | S |

### Garmin platform depth

`python-garminconnect` now exposes 16 advanced-metrics methods (training
readiness, training status, HRV, VO₂, training zones, running tolerance) plus
training-plan objects, an exercise catalog, in-place workout editing and
push-to-device ([GitHub](https://github.com/cyberjunky/python-garminconnect)).
All reverse-engineered; all subject to breaking without notice.

| # | Feature | Tier | Effort |
|---|---|---|---|
| 77 | ✅ **Read training status / readiness / HRV / VO₂ / tolerance** — the keystone alongside #11; everything in the previous table depends on it. | 2 | L |
| 78 | ✅ **In-place workout editing** (`update_workout`) instead of delete-and-recreate — keeps Garmin's IDs and any history attached to them. | 1 | S |
| 79 | ✅ **Push to device** (`push_workout_to_device`) so a workout lands on the watch now, not at the next sync. | 1 | S |
| 80 | ✅ **Exercise catalog search** for strength sessions — needed for #22. | 2 | M |
| 81 | ✗ **Push a block as a Garmin training-plan object** rather than loose workouts; schema unverified, so this is L not M. | 3 | L |
| 82 | ✅ **Per-step cues on the watch** — the Fenix displays optional step notes during a workout ([Garmin manual](https://www8.garmin.com/manuals/webhelp/GUID-C001C335-A8EC-4A41-AB0E-BAC434259F92/EN-US/GUID-54A017B7-95D1-4C96-A39F-AEA91B7ACE29.html)). We send `description`; verify it renders, then let the UI author cues like "tall posture". | 1 | S |
| 83 | ◐ **Daily Suggested Workout conflict warning** — a pushed plan and Garmin's own suggestions compete for the same day. | 2 | S |
| 84 | ~~**Python-floor watch**~~ **done** — turned out not to be hypothetical; see below. The project now requires Python 3.12, matching `python-garminconnect` 0.3.16. | — | — |

### Interoperability

| # | Feature | Tier | Effort |
|---|---|---|---|
| 85 | ✅ **Import and export intervals.icu's text workout syntax** — the closest free tool, 160k athletes, and its plain-text format is a natural sibling of our DSL ([intervals.icu](https://www.intervals.icu/features/workout-builder/)). | 2 | M |
| 86 | ◐ **Export FIT workout files** (the USB sideload path from the very first conversation), plus ZWO/MRC/ERG for cross-training on a bike. | 2 | M |
| 87 | ✅ **One-line workout generator** — TrainingPeaks lets coaches type `20min warmup, 6x3m @ threshold w/ 2min recovery, 10 min cooldown` and get a structured workout ([TrainingPeaks](https://www.trainingpeaks.com/learn/articles/introducing-trainingpeaks-workout-builder/)). A fast path for people who know exactly what they want. | 1 | S |
| 88 | ✅ **An MCP server** exposing generate / preview / push, so *any* assistant — Claude, ChatGPT, Gemini, a local model — can drive the tool conversationally. IcuSync already does this for intervals.icu; `garmin_mcp` exists for the read-side. Directly serves "runs generated by any AI". | 2 | M |
| 89 | ✅ **RPE targets** (TrainingPeaks supports perceived exertion). Garmin has no RPE target type, so compile to "no target" plus a step note — honest, and still useful for runners without a HR strap. | 3 | S |
| 90 | ✅ **Per-session load number** (rTSS / TRIMP) on the review card, auto-calculated the way TrainingPeaks does — a load *number*, never an ACWR. | 2 | M |

### App experience

| # | Feature | Tier | Effort |
|---|---|---|---|
| 91 | ✗ **Lap-alert text** — the watch's lap alert message is customisable; author it per workout. | 3 | S |
| 92 | ✅ **"Why this session" line** pushed as the first step note, so the purpose is on the wrist. | 2 | S |
| 93 | ✅ **Watch-screen preview** — a mock of what the step screen will show, so surprises happen on a laptop. | 3 | M |
| 94 | ✅ **Plan-health dashboard**: weekly volume, hard-time share, long-run trend. The gap every adaptive tool was criticised for lacking. | 2 | M |
| 95 | ✅ **Onboarding asks the right questions**: injury history, availability, goal race — feeding #52, #17 and #7 in one screen. | 1 | M |
| 96 | ✅ **Command palette and shortcuts** — `⌘K` to jump between compose / review / push. | 3 | S |

### Distribution and operations

| # | Feature | Tier | Effort |
|---|---|---|---|
| 97 | ✅ **Pin GitHub Actions to commit SHAs** — supply-chain hygiene now that the repo is public and Dependabot will keep the pins fresh. | 1 | S |
| 98 | ✅ **Release workflow**: tag → build wheel → GitHub Release with notes, so `uv tool install` can target a version instead of `main`. | 1 | S |
| 99 | ◐ **Homebrew tap and winget manifest**. Package managers don't dodge SmartScreen (see the stack assessment) but they do make install one familiar command. | 3 | M |
| 100 | ✅ **`gpp doctor --bundle`**: write a local diagnostics file for bug reports — versions, profile shape, last error — and never upload it. | 2 | S |

### What this batch changed in the first fifty

- **#11 (read activities) and #77 (read metrics) are the same keystone** seen from two sides; do them together.
- **#24 (weather) is now two items**: #73 is a Tier 1 rule of thumb needing no infrastructure; #74 is the proper model.
- **#22 (strength) depends on #80.**
- **The Dependabot advisory was not a false positive.** `python-garminconnect` <= 0.3.4 set insecure permissions on the OAuth token store. Our declared floor was `>=0.2.19` and our Python floor was 3.11 - but 0.3.16 needs Python 3.12, so the lock file had silently forked: `0.3.16` for 3.12+, **`0.3.2` for 3.11**. Every Python 3.11 install, including three green CI jobs, was running the vulnerable version. Fixed by requiring Python >= 3.12 and `garminconnect >= 0.3.16`; the lock now has one entry. A passing matrix can hide a forked resolution.

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

The first batch lost six of eight research streams to a session rate limit.
The follow-up pass (done directly, not via subagents) covered intervals.icu,
TrainingPeaks, Runna and Garmin Run Coach, Runalyze and GoldenCheetah,
Pfitzinger, critical speed, heat adjustment, HRV-guided training and the
`python-garminconnect` read-side — those are reflected in the second fifty.

Still not researched, stated so nobody builds on a guess:

- **Altitude pace adjustment** (#75) — no formula found in this pass.
- **Garmin's training-plan object schema** (#27, #81) — the library exposes
  it; the shape is unverified.
- **Courses API** (#26) — sketched from the official docs' description only.
- **Reddit** was unreachable from the research environment throughout, so the
  "what runners complain about" signal comes from App Store reviews,
  Trustpilot, coaching blogs and the one peer-reviewed evaluation of
  ChatGPT-written plans. Several review samples are small (Stryd n = 24,
  Humango n = 21, AI Endurance n = 24) and indicate failure *modes*, not
  prevalence.

Also: Reddit was unreachable from the research environment, so the "what do
runners actually complain about" signal comes from App Store reviews,
Trustpilot and vendor forums. Several of those samples are small (Stryd n=24,
Humango n=21, AI Endurance n=24) and indicate failure *modes*, not prevalence.
