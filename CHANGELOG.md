# Changelog

All notable changes, newest first. The release workflow takes the section for
the tag being released and puts it in the GitHub Release notes.

## [0.2.2] — 2026-09-29

After upgrading, each Garmin account signs in with its password once more:
logins are now kept per account. None of this has been run against a live
Garmin, intervals.icu or OpenAI account; the Garmin calls are checked
offline against the real garminconnect library.

### Fixed
- Garmin: on a computer shared by several athletes, every push went to
  whichever account signed in first, whatever email was typed, and with
  Replace or prune rewrote that athlete's sessions. Each account now has
  its own saved login (under `~/.garminconnect/gpp/`), and the log names
  the account Garmin signed in to.
- Garmin: with no internet, a saved login was reported as expired and the
  password asked for. A network error, a 5xx or a 429 now says what is
  wrong and asks for nothing.
- Push: a plan was known on the calendar by the first 16 letters of its
  name, so two plans whose names began alike rewrote each other's sessions
  on a shared day, and `--prune` removed the other plan's. Each plan now
  has its own tag; sessions pushed by 0.2.1 are still recognised on their
  own day and title. Prune only looks from today on, lists what it would
  remove and asks first (`--yes` skips the question).
- Push: a failed calendar read after the writes no longer loses the
  receipt that `gpp unpush` works from.
- Strength sessions: exercises are looked up in Garmin's own catalog
  (names used to be guessed into pairs Garmin does not have), `gpp check`
  flags a name the catalog lacks with the closest matches, and `gpp
  exercises` searches it without signing in. Weights go out in grams, as
  Garmin stores them: a 16 kg goblet squat used to arrive as 16 g.
- Claude: rewriting one session in the web app and `gpp generate
  --chunk-weeks` failed with "Streaming is required"; every call streams.
- OpenAI: the default model (gpt-5.2) rejected every request because gpp
  sent `max_tokens`; OpenAI now gets `max_completion_tokens`. The `gpp
  doctor --ping` check gives thinking models room to answer.
- MCP: a tool error reaches the assistant with its reason instead of
  "Error executing tool".
- intervals.icu: `--athlete` is the athlete id again (it was looked up as
  a gpp profile name). Distances under 1 km go out as `mtr`, since
  intervals.icu reads `400m` as 400 minutes, and imports read `m` as
  minutes. Sessions moved since a push are found and listed, and removed
  with `--prune` after asking; `--remove` lists what it found and asks too.
- Profile: a save (`gpp profile set`, `reestimate --apply`, the heart-rate
  zone sync, the web settings) dropped `[defaults]` and every key gpp does
  not manage, and could write TOML the next command could not read. It now
  keeps every setting and replaces the file in one step. New profiles go
  to `~/.config/gpp/profile.toml`, so `gpp web` finds them from any folder,
  and `gpp backup` includes the profile in use wherever it lives.
- Web app: a profile with a typo is reported with its line, instead of
  opening setup and saving a new profile over it.
- Web app: closing the send dialog mid-send no longer loses the send, and
  a second send cannot run alongside the first.
- Web app: Check first honours the Replace box; a return to running after
  a week or less off shows the advice instead of replacing the plan with
  an empty one; a decimal heart rate is rounded and saved; Enter works in
  the edit dialog.
- One-line workouts read "warm up", "cool down" and "warm down" as two
  words, so the README's own example works. Durations accept `min`, `hr`
  and `sec`.
- Windows: plans and profiles saved as UTF-16 or with a byte-order mark
  are read; text the console cannot show no longer crashes the output; a
  CSV export opens in Excel with every letter intact.
- A mistyped path, a bad duration or a Garmin failure prints one line
  instead of a traceback.
- `gpp pause`, `missed` and `import` keep stdout to the plan, so
  `> file.json` saves a plan gpp can read.
- `gpp doctor --bundle` works with no file name, as the guide gives it.
- `gpp template return` ramps start on their start day, and `--tier
  resume` prints the advice instead of writing an empty plan.
- Calendar (.ics) exports and FIT names no longer lose letters at a line
  fold or a field limit.
- `gpp pull` never fetches a workout by a calendar entry's own id.
- `gpp weather` checks the start hour against sunrise and sunset in local
  time; away from UTC, a start in the dark could pass without a warning.
- `gpp --json eval` prints JSON; `gpp watch --push` sends to
  `[defaults] device` and saves a receipt; a second `gpp backup` the same
  day no longer overwrites the first.
- `gpp generate --chunk-weeks` reports its tokens and cost, and `gpp
  eval`'s default races are dated from today.
- Cost estimates price Opus 5.5 and Sonnet 5.5, and each model's own
  cache-read rate.

### Security
- Web app: a start-up error is shown as text (a crafted profile could run
  script in the page), provider settings can no longer be written from the
  page, every response carries a Content-Security-Policy, and library
  templates open by name only, never by a path on disk.
- Options can no longer be abbreviated (`--ke` for `--key`), so a key
  cannot slip past the debug log's redaction.

### Changed
- The default Claude model is Opus 5.5. A profile that names
  `claude-opus-5` keeps it.
- Plans written with `-o` keep the file they replace as `.1` (up to `.5`),
  and plans the command line writes or pushes join the recent plans that
  `gpp next` and `gpp week` read.
- The guide: install Git first if the computer lacks it; a key step that
  works on Windows and on a Mac; and it now says where the password goes,
  how to add a local model, what blocking findings do, and how to remove
  a sent plan (`gpp pushes`, then `gpp unpush --receipt`).
- The MCP install line names Python 3.12.
- Package metadata: description, licence, links and classifiers. The
  Homebrew draft is renamed `garmin-plan-push`, since homebrew-core
  already has a `gpp`; both packaging files are marked as drafts.
- Development: `scripts/check.py` installs every extra and fails on a
  stale lock; ty 0.0.84 and pip-audit 2.10.1 are pinned; the lock is
  refreshed.

### Removed
- `gpp suggestion`: garminconnect has no call for Garmin's daily suggested
  workout, so it could only say none was available.

## [0.2.1] — 2026-09-29

Pushing to Garmin should work again, and sign in once instead of every
time: it was checked offline against the real garminconnect library, not
against a live Garmin account. If you use intervals.icu and pushed with
0.2.0, run `gpp icu plan.json --remove --athlete <your intervals.icu id>`
(or set `ICU_ATHLETE_ID`) once before pushing again.

### Fixed
- Pushing to Garmin failed on every call with garminconnect 0.3.16 (the
  version gpp requires): push, live dry run, sync, history import and unpush
  all stopped at "Connection error". Requests now go through the library's
  0.3.x client, and a contract test drives the real library offline so a
  release that moves it fails a test instead of a push. garminconnect is now
  capped below 0.4.
- The Garmin login was never saved, so every push was a full sign-in with a
  two-factor code, and `--token-dir` was silently ignored. The login is now
  saved owner-only in `~/.garminconnect` (or `--token-dir`) and reused; the
  password is asked for only when there is no saved login or Garmin has
  expired it. The web app's push dialog says when the password can stay blank.

- Installing the way the guide says left out the Claude and OpenAI libraries,
  so generating a plan failed with an instruction (`uv sync --extra ...`) that
  does not apply to an installed tool. Both now come with every install, and
  a missing library names the reinstall command. `gpp doctor` flags it.

- `gpp mcp` said the `mcp` package was missing when mcp 2.x was installed:
  2.x renamed the server class. Ported to mcp 2.x (`mcp>=2.2,<3`); the error
  now tells "not installed" from "wrong version" and gives the install line.

- Deleting or updating a workout could fall back to a calendar entry's own
  id, which is the schedule entry, not the workout. Only the workout id is
  used now; an entry without one is reported instead of touched.
- A session moved to another day left its old copy on the calendar with no
  word about it. Push (and the live dry run) now list this plan's sessions on
  dates it no longer uses, and `gpp push --prune` removes them.
- The package now passes `ty check` (it had about 45 errors, a few of them
  real: a missing pace or HR bound failed with a bare TypeError instead of
  saying which target is incomplete, and a profile update could try to save
  to no path at all).
- intervals.icu: editing a session and pushing again added a second event,
  because the event id included a hash of the session's content. The id is
  now the plan, the date and the session's place on that day. Events pushed
  by 0.2.0 carry the old ids: run `gpp icu plan.json --remove` once (with
  `--athlete` or `ICU_ATHLETE_ID`), then push again.

### Security
- `--verbose` wrote the full command line to the debug log, so
  `gpp icu --key ...` put the intervals.icu API key there, and
  `gpp doctor --bundle` copied that log into a file described as holding no
  credentials. Credential flags are now redacted in the log, the bundle
  scrubs them (and any `*_API_KEY`/`*_TOKEN`/`*_PASSWORD` value) from older
  lines too, and only gpp's own logger writes at debug level. `gpp icu` asks
  for the key when it is not set, and the key instructions paste the key at a
  prompt so it stays out of shell history.
- `gpp restore` could write outside its folders when a zip entry had an
  absolute or drive-qualified name. Every entry is now resolved and skipped
  unless it lands inside its folder; restored Garmin tokens are written
  owner-only; and backups no longer include the debug log.
- The local web server can no longer be framed by another site, rejects a
  negative Content-Length, and refuses AI provider settings that would send
  a key to a non-https URL or read a variable not named `*_API_KEY`.
  Unexpected server errors are logged with their traceback (`--verbose`),
  and the Garmin password field is cleared after a failed push too.

### Added
- `gpp signout` deletes the saved Garmin login; the web send dialog has the
  same as a link.
- Web send dialog: **Check first** signs in and shows what Send would
  create, update or leave alone, writing nothing (the CLI's
  `push --dry-run --live`), and a box to remove this plan's sessions left on
  dates it no longer uses.

### Changed
- Version 0.2.1 everywhere, including the Homebrew and winget templates
  (they still pointed at 0.1.0); a test now keeps them in step. The winget
  template is marked as not submittable until there is a real installer.
- `scripts/check.py` runs the checks CI ran on every push (ruff, ty, the
  tests with a 75% coverage floor, pip-audit), on one machine. CI's three
  operating systems, its check of the wheel's web assets and its CLI and
  server smoke tests have no replacement. `ty` is pinned in the dev extra.
- anthropic 1.9.0 and openai 3.20.0 in the lock; both capped below their
  next major.

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
- A user guide, served from GitHub Pages at
  https://palatter.github.io/garmin-plan-push/ (`docs/`).
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
