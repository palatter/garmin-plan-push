# Changelog

All notable changes, newest first. The release workflow takes the section for
the tag being released and puts it in the GitHub Release notes.

## [0.2.0] — 2026-09-22

Three research-and-review rounds; two hundred roadmap items, of which the
[roadmap's status section](ROADMAP.md#status) says which are built, partial
or deliberately not built.

### Fixed
- A push could delete or overwrite *another plan's* session on a shared day;
  sessions are now matched by the plan's own tag slug only.
- Two sessions on one day could overwrite each other on re-push.
- Plans with non-ASCII names (e.g. "Höst 10k") never matched their own tag and
  duplicated on every push.
- "No running Mondays" blocked every session that mentioned running; day rules
  and time-bound rules ("no hills for 6 weeks") are now understood.
- Race-day volume no longer counts as taper volume.
- The generation prompt states today's date and the default start, so plans
  are no longer dated in the past.
- A cut-off answer from the model is detected, explained and retried with more
  room instead of being reported as malformed JSON.
- Every `gpp` command crashed on Python 3.14 because of a stray `%` in a help
  string (fixed in 0.1.x on `main`).

### Added
- Review screen: edit any session (date, steps, per-step cues, one-line
  rewrite), calendar with drag-to-reschedule, sanity report, plan health,
  changes with undo, watch-screen preview, missed → readapt or keep, pause,
  return-to-run ramp, rewrite with AI, export, library, command palette.
- Web app, round three: this week and next, load focus, the plan's rationale,
  recap lines and plan-vs-actual once runs are synced, hot-day paces, move
  without dragging, a shortcut sheet, education snippets in the edit and watch
  dialogs, recent plans on the first screen, a first-run checklist, print
  styles, calendar/FIT/Markdown/CSV export, installable (manifest and service
  worker), accessibility and contrast tests.
- Onboarding: goal race, availability, injuries, standing rules, recent volume.
- Sanity checks: long-run spike, weekly ramp, deload, hard share, middle gear,
  back-to-back quality, monotony, taper shape, race windows, availability and
  constraints, plus long-run share, two long runs, hard sessions around races,
  strength placement, goal-race gaps, availability envelope, rest streaks, date
  and target sanity.
- Generation: structured outputs on every provider with fallbacks, streaming,
  token and cost reporting with prompt caching, multi-turn corrections, a level
  envelope and intensity-distribution choice in the prompt, plan rationale,
  continuation from a previous block, chunked generation for long plans,
  `gpp eval` and `gpp providers --bench`.
- Enrichment by rule: fuelling cues, heat block, strength placement,
  durability finishes, cadence cues; post-race recovery weeks; race-specific
  seed sessions with sources.
- Garmin: in-place updates, push to device, unpush, push receipts, live dry
  run, conflict listing, verify of end conditions and both bounds, reverse
  compile and `gpp pull`, FIT export and import, race-day sessions and split
  tables, Load Focus preview, body-status log, HR-zone import, MFA re-prompt.
- History and sync: activities and daily metrics, compliance, threshold
  re-estimation, marathon shape, daily nudge, RPE and pain logs.
- Interop: intervals.icu text and calendar push (`gpp icu`), ZWO, MRC/ERG,
  FIT, CSV, Markdown, share bundles, ICS, plan diff, pace ↔ power transpile,
  MCP server.
- CLI: `next`, `week`, `plans`, `--json`, `--version`, `--verbose`,
  `[defaults]`, `profile set|get`, `schema`, completions, `backup`/`restore`,
  versioned saves, `watch`, `doctor --bundle` and environment checks.
- Operations: pinned actions, a release workflow with trusted publishing,
  coverage and type-check jobs, CodeQL, dependency review, Scorecard,
  property-based tests.

## [0.1.0] — 2026-09

- First release: the plan DSL, validator and compiler; Claude, ChatGPT,
  Gemini, OpenAI-compatible, Ollama and paste providers with a validation
  retry loop; Garmin Connect push with read-back verification; the local web
  app with setup, compose and review screens.
