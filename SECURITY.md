# Security

## What this tool handles

- **Your Garmin Connect password**, once per login, in memory only. It is
  sent from the local page to the local server to Garmin, and never written to
  disk. Garmin's own OAuth token cache (`~/.garminconnect/`) is the only thing
  that persists — treat that directory as you would a saved login.
- **AI API keys**, read from environment variables, never stored by this tool.
- **Your training profile** (`profile.toml`): name, paces, heart-rate numbers.
  Local file, git-ignored.

## How the local server is protected

`gpp web` runs an HTTP server on your machine. Because it can be handed a
Garmin password, it:

- binds `127.0.0.1` only — it is not reachable from other machines;
- requires a per-run random token on every API call, injected into the page at
  load, so a page on any other origin cannot drive it (it cannot read the
  token to sign a request);
- checks the `Host` header, which is what stops DNS-rebinding attacks;
- compares the token in constant time, as bytes.

There is no TLS because there is no network hop — traffic never leaves the
loopback interface.

## What it does not do

- It never sends your plan, profile or credentials anywhere except Garmin
  Connect and the AI provider you chose. With the `manual` provider, nothing
  is sent to any AI service at all.
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
