#!/bin/sh
# Updates the Homebrew formula in the tap repo with a new version and SHA.
# Called by the publish workflow after PyPI upload succeeds.
#
# Usage: update-homebrew-tap.sh <version> <sha256>
# Requires: TAP_TOKEN env var (GitHub PAT with repo scope for the tap)
#           uv and Python available (for resource block generation)
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION="$1"
SHA256="$2"
TAP_REPO="lanefoundry/homebrew-tap"

if [ -z "${TAP_TOKEN:-}" ]; then
    echo "error: TAP_TOKEN not set" >&2
    exit 1
fi

# Generate Python dependency resource blocks
echo "Generating resource blocks for ${VERSION}..."
RESOURCES=$(uv run python "$SCRIPT_DIR/generate-homebrew-resources.py" 2>/dev/null)

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

git clone "https://x-access-token:${TAP_TOKEN}@github.com/${TAP_REPO}.git" "$WORKDIR/tap"
cd "$WORKDIR/tap"

mkdir -p Formula

cat > Formula/looplane.rb << FORMULA
class Looplane < Formula
  include Language::Python::Virtualenv

  desc "A Python-first coding agent that produces verified patches in disposable workspaces"
  homepage "https://github.com/lanefoundry/looplane"
  url "https://pypi.io/packages/source/l/looplane/looplane-${VERSION}.tar.gz"
  sha256 "${SHA256}"
  license "Apache-2.0"

  depends_on "python@3.12"

${RESOURCES}

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "looplane", shell_output("#{bin}/looplane version")
  end
end
FORMULA

git add Formula/looplane.rb
git commit -m "looplane ${VERSION}"
git push origin main
echo "Homebrew tap updated to ${VERSION}"
