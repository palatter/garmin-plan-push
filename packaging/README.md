# Packaging

Two more ways to install gpp than `uv tool install`: a Homebrew formula,
and a standalone Windows download for winget. Neither is published from
this repo; both need an account and a separate submission.

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

## The standalone Windows download (`windows/`)

`windows/build.py` freezes gpp with PyInstaller: a folder holding
`gpp.exe` and its own Python, so nothing else needs installing. It then
runs that `gpp.exe` on commands that load every library it carries (the
Garmin sign-in included, stopping before the network), and zips the
folder as `dist/gpp-<version>-windows.zip` with a `.sha256` beside it.
On Windows, for each release:

    uv run --locked --with pyinstaller==6.22.3 python packaging/windows/build.py
    gh release upload v<version> dist/gpp-<version>-windows.zip dist/gpp-<version>-windows.zip.sha256

It is not code-signed, so Windows SmartScreen may warn the first time a
downloaded `gpp.exe` runs; see ROADMAP § Tech stack assessment for what
signing would cost.

## winget (`winget/`)

Three manifests for `palatter.GarminPlanPush`: the zip above, with
`gpp.exe` as a portable command, plus the version and locale manifests.
They carry the current version (the version test checks) and a
placeholder for the zip's SHA-256. To submit a release:

1. With the zip on the release, `python packaging/winget/fill.py --release`
   writes the manifests with its SHA-256 into `dist/winget/<version>/`.
2. On Windows, `winget validate dist\winget\<version>`, then try it:
   `winget install --manifest dist\winget\<version>` (local manifests
   are off by default; `winget settings --enable LocalManifestFiles`, as
   administrator, turns them on).
3. Submit the folder to microsoft/winget-pkgs:
   `wingetcreate submit --token <a GitHub token> dist\winget\<version>`,
   or a pull request adding it as
   `manifests/p/palatter/GarminPlanPush/<version>/`.
4. Later versions: `wingetcreate update palatter.GarminPlanPush --version
   <version> --urls <zip URL> --submit --token <a GitHub token>`.

Once it is accepted, `winget install palatter.GarminPlanPush` installs
gpp and puts it on PATH, `winget upgrade` updates it, and `gpp doctor`
tells a standalone install to reinstall with winget rather than uv.
