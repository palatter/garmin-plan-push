# Homebrew tap formula for garmin-plan-push. A DRAFT: it has not been built
# or tested with brew. Before publishing to a tap, fill in sha256 for the
# release tarball and generate the resource blocks (packaging/README.md).
#
# Named garmin-plan-push, not gpp: homebrew-core already has a `gpp` formula
# (a text preprocessor) that installs bin/gpp too.
class GarminPlanPush < Formula
  include Language::Python::Virtualenv

  desc "Turn AI-written running plans into structured Garmin workouts"
  homepage "https://palatter.github.io/garmin-plan-push/"
  url "https://github.com/palatter/garmin-plan-push/archive/refs/tags/v0.2.1.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.12"

  conflicts_with "gpp", because: "both install a `gpp` executable"

  # The Python dependencies go here as resource blocks, generated with
  #   brew update-python-resources palatter/gpp/garmin-plan-push
  # Without them the install has gpp but none of the libraries it imports.

  def install
    virtualenv_install_with_resources
  end

  test do
    (testpath/"profile.toml").write <<~TOML
      name = "Test"
      [pace]
      threshold = "4:30/km"
    TOML
    (testpath/"plan.json").write <<~JSON
      {"plan": "Test", "workouts": [{"name": "Easy", "date": "2030-01-08",
        "steps": [{"kind": "run", "duration": "40m"}]}]}
    JSON
    # Checking a plan needs jsonschema; importing the rest proves they came too.
    assert_match "1 workout(s) valid",
      shell_output("#{bin}/gpp --profile #{testpath}/profile.toml check #{testpath}/plan.json")
    system libexec/"bin/python", "-c", "import garminconnect, anthropic, openai"
  end
end
