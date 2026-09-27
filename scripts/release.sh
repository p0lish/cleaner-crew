#!/usr/bin/env bash
# Tag main as a release; the `release` workflow does the rest (CI, PyPI, GitHub release).
#
#   1. bump __version__ in src/cleaner_crew/__init__.py in a pull request, merge it
#   2. git checkout main && git pull
#   3. scripts/release.sh
set -euo pipefail

die() { echo "release: $*" >&2; exit 1; }

cd "$(git rev-parse --show-toplevel)"
git fetch -q origin main --tags

[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "not on main"
[ -z "$(git status --porcelain --untracked-files=no)" ] || die "working tree has changes"
[ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] || die "main is not in sync with origin/main"

version=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' src/cleaner_crew/__init__.py)
[ -n "$version" ] || die "could not read __version__"
tag="v$version"
if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  die "$tag already exists; bump __version__ first"
fi

git tag -a "$tag" -m "Release $tag"
git push origin "$tag"
echo "pushed $tag. Follow the release: gh run watch \$(gh run list -w release -L1 --json databaseId -q '.[0].databaseId')"
