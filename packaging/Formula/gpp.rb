# Homebrew tap formula for garmin-plan-push.
# Replace url/sha256 with the release tarball before publishing to a tap.
class Gpp < Formula
  include Language::Python::Virtualenv

  desc "Turn AI-written running plans into structured Garmin workouts"
  homepage "https://github.com/palatter/garmin-plan-push"
  url "https://github.com/palatter/garmin-plan-push/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "REPLACE_WITH_RELEASE_TARBALL_SHA256"
  license "MIT"

  depends_on "python@3.12"

  # Runtime dependencies are resolved from the lock at release time:
  #   uv export --no-dev --no-hashes | poet-style resource blocks
  # (homebrew-pypi-poet or `brew update-python-resources gpp` generates these)

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "usage: gpp", shell_output("#{bin}/gpp --help")
  end
end
