# Packaging

Package-manager manifests for the two ecosystems the roadmap named (#99).
Neither is published from this repo -- both need an account and a separate
submission -- so what lives here is the ready-to-submit source, and the
honest notes on what publishing would cost.

## Homebrew (`Formula/gpp.rb`)

A tap formula that installs the CLI into its own virtualenv. To publish:

1. Create a tap repository named `homebrew-gpp` under your account.
2. Copy `Formula/gpp.rb` into it, replacing `url`/`sha256` with the release
   tarball from the GitHub Release (`gh release download` gives you both).
3. Users then run `brew tap palatter/gpp && brew install gpp`.

Homebrew's acceptable-casks policy requires binaries to pass Gatekeeper; a
Python formula that builds from source sidesteps that, which is why this is
a formula rather than a cask.

## winget (`winget/palatter.gpp.yaml`)

A manifest that runs the `uv tool install` line, since there is no signed
installer to point at. To publish, open a pull request against
`microsoft/winget-pkgs` with the three manifest files winget expects; the
template here is the installer manifest. Expect the reviewers to ask for a
signed installer eventually -- see ROADMAP § Tech stack assessment for what
that costs.

## What this does not solve

Neither route removes macOS Gatekeeper or Windows SmartScreen prompts for a
frozen binary; both just make the *install command* familiar. The tool is
still a `uv tool install` underneath.
