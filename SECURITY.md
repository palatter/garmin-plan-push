# Security

## What this tool handles

- **Your Garmin Connect password.** It is sent from the local page to the
  local server to Garmin, and written to no file. Garmin's own login token
  persists, one per Garmin account
  (`~/.garminconnect/gpp/<account>/garmin_tokens.json`), so later pushes skip
  the password and the two-factor code, and two athletes sharing a computer
  each reach their own calendar. Treat those files as you would a saved
  login. They are written owner-only (0600 in a 0700 folder) and symlinked
  paths are refused.
  A password you type (in the send dialog with **Keep the password** ticked,
  or at the terminal) is also kept in the OS keychain (Windows Credential
  Manager, the macOS Keychain, the Secret Service on Linux) once Garmin
  accepts it, so a sign-in Garmin expires is renewed without asking.
  `GARMIN_PASSWORD` is never kept. The keychain encrypts it for your user
  account; like the token files, anything running as you can read it. Run
  `gpp signout` (or Sign out in the send dialog) to delete the tokens and
  the kept passwords.
  `python-garminconnect` versions up to 0.3.4 created that directory with
  insecure permissions (a published advisory, fixed in 0.3.5); this project
  requires 0.3.16 or newer.
- **AI and intervals.icu API keys**, read from an environment variable
  first, then from the OS keychain, where the web app's **Keys** fields and
  `gpp keys set` put them. They are never written to a file, the debug log
  or the diagnostics bundle, and never sent back to the page.
- **Your training profile** (`profile.toml`): name, paces, heart-rate numbers.
  A local file in `~/.config/gpp` (`C:\Users\<you>\.config\gpp` on Windows),
  or `profile.toml` in the folder gpp is started from, if there is one.

## How the local server is protected

`gpp web` runs an HTTP server on your machine. Because it can be handed a
Garmin password, it:

- binds `127.0.0.1` only — it is not reachable from other machines;
- requires a per-run random token on every API call, injected into the page at
  load, so a page on any other origin cannot drive it (it cannot read the
  token to sign a request);
- checks the `Host` header, which is what stops DNS-rebinding attacks;
- compares the token in constant time, as bytes;
- refuses to be framed by another site (`X-Frame-Options: DENY` and
  `frame-ancestors 'none'`), so its buttons cannot be clicked through a
  disguised page;
- sends a Content-Security-Policy that lets only its own files run, so text
  that reaches the page (an error quoting the profile, say) cannot run script;
- never changes AI provider settings from the page. They name an environment
  variable and a URL, so they are edited in `profile.toml` only;
- saves only the keys the app reads, and never sends a key or a password
  back to the page: it is told where each key comes from, not what it is.

The token keeps other websites out, not other programs: anything running on
this machine can open the page and drive the app while `gpp web` runs. On a
computer you share with other accounts, stop it when you are done.

There is no TLS because there is no network hop — traffic never leaves the
loopback interface.

## What it does not do

- It sends your plan, profile and credentials only where you point it:
  - Garmin Connect, when you push, sync or sign in;
  - the AI provider you chose, which gets the profile and your request (with
    the `manual` provider, nothing is sent to any AI service at all);
  - intervals.icu, when you run `gpp icu` or send from the web app, which
    sends the plan with your intervals.icu API key;
  - Open-Meteo, when `gpp weather` or `gpp today --forecast` looks up a
    forecast, which sends the latitude and longitude from your profile.
- It does not phone home, collect telemetry, or check for updates.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/palatter/garmin-plan-push/security/advisories/new)
on GitHub rather than a public issue. Include steps to reproduce. You should
hear back within a week.

## Scope note

The Garmin integration uses an unofficial, reverse-engineered API through
`python-garminconnect`. Changes on Garmin's side can break it without notice;
that is a reliability concern, not a security one, but it is worth knowing
before you depend on it.
