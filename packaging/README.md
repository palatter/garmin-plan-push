# Packaging

Package-manager manifests for the two ecosystems the roadmap named (#99).
Neither is published from this repo -- both need an account and a separate
submission -- and neither has been built or tested with its tool, so both
are drafts. What lives here is that draft source, and the honest notes on
what publishing would cost.

## Homebrew (`Formula/garmin-plan-push.rb`)

A tap formula that installs the CLI into its own virtualenv. It is named
`garmin-plan-push` because homebrew-core already has a `gpp` formula (a
text preprocessor), and a bare `gpp` in a brew command means that one. Both
install a `gpp` executable, so the formula declares the conflict. To publish:

1. Create a tap repository named `homebrew-gpp` under your account.
2. Copy `Formula/garmin-plan-push.rb` into its `Formula/` folder and fill in
   `sha256` for the tagged tarball (`curl -sL <url> | shasum -a 256`).
3. Generate the resource blocks for the Python dependencies with
   `brew update-python-resources palatter/gpp/garmin-plan-push`, then run
   `brew install --build-from-source palatter/gpp/garmin-plan-push` and
   `brew test palatter/gpp/garmin-plan-push`. The test checks a plan, which
   fails if the dependencies did not install.
4. Users then run `brew install palatter/gpp/garmin-plan-push`.

Homebrew's acceptable-casks policy requires binaries to pass Gatekeeper; a
Python formula that builds from source sidesteps that, which is why this is
a formula rather than a cask.

## winget (`winget/palatter.gpp.installer.yaml`)

Not usable as it stands: winget cannot run an install command, and its
`portable` installer type copies a single executable, which a wheel is not.
The draft is the installer manifest for the route that would work: a zip
release asset holding a small launcher (`gpp.exe`) that runs gpp through uv,
installed as a zip with a nested portable. That zip does not exist yet.
Publishing needs it first, then a pull request against
`microsoft/winget-pkgs` with the version and locale manifests beside this
one. Expect the reviewers to ask for a signed installer eventually -- see
ROADMAP § Tech stack assessment for what that costs.

## What this does not solve

Neither route removes macOS Gatekeeper or Windows SmartScreen prompts for a
frozen binary; both just make the *install command* familiar. The tool is
still a `uv tool install` underneath.
