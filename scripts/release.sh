#!/bin/sh
# Cut a release: bump version in pyproject.toml, commit, tag, push.
#
# Usage: scripts/release.sh <version>
#   e.g. scripts/release.sh 0.2.0
#
# The push triggers the publish workflow which handles PyPI, GitHub Release,
# and Homebrew tap update.
set -eu

if [ $# -ne 1 ]; then
    echo "Usage: $0 <version>  (e.g. 0.2.0)" >&2
    exit 1
fi

VERSION="$1"
TAG="v${VERSION}"

# Validate semver-ish format
if ! echo "$VERSION" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+'; then
    echo "error: version must be semver (e.g. 0.2.0)" >&2
    exit 1
fi

# Check clean working tree
if [ -n "$(git status --porcelain)" ]; then
    echo "error: working tree is not clean. Commit or stash changes first." >&2
    exit 1
fi

# Check tag doesn't already exist
if git rev-parse "$TAG" >/dev/null 2>&1; then
    echo "error: tag $TAG already exists" >&2
    exit 1
fi

# Bump version in pyproject.toml
sed -i.bak "s/^version = \".*\"/version = \"${VERSION}\"/" pyproject.toml
rm -f pyproject.toml.bak

echo "Bumped pyproject.toml to ${VERSION}"

git add pyproject.toml
git commit -m "release: ${VERSION}"
git tag -a "$TAG" -m "Release ${VERSION}"

echo ""
echo "Created commit and tag ${TAG}."
echo "Run 'git push origin main ${TAG}' to trigger the publish workflow."
