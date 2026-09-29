# Security

## What this tool handles

- **Your Garmin Connect password**, once per login, in memory only. It is
  sent from the local page to the local server to Garmin, and never written to
  disk. Garmin's own login token is the only thing that persists, one per
  Garmin account (`~/.garminconnect/gpp/<account>/garmin_tokens.json`), so
  later pushes skip the password and the two-factor code, and two athletes
  sharing a computer each reach their own calendar. Treat those files as you
  would a saved login. They are written owner-only (0600 in a 0700 folder)
  and symlinked paths are refused; run `gpp signout` (or Sign out in the send
  dialog) to delete them.
  `python-garminconnect` versions up to 0.3.4 created that directory with
  insecure permissions (a published advisory, fixed in 0.3.5); this project
  requires 0.3.16 or newer.
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
- compares the token in constant time, as bytes;
- refuses to be framed by another site (`X-Frame-Options: DENY` and
  `frame-ancestors 'none'`), so its buttons cannot be clicked through a
  disguised page;
- only accepts AI provider settings whose key variable is named `*_API_KEY`
  and whose URL is https (plain http only to this machine), so no request
  can point another secret at someone else's server.

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
