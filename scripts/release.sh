#!/usr/bin/env bash
# Cut a Helm release: write VERSION, commit, tag, push, create the GitHub release.
#
#   scripts/release.sh 0.9.0            # tag v0.9.0 and publish with generated notes
#   scripts/release.sh 0.9.0 --draft    # create the release as a draft to edit first
#
# Run from a clean, up-to-date main. The tag lands on the commit that carries the
# new VERSION file, so the build and the tag always agree.
set -euo pipefail

ver="${1:-}"; shift || true
[[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "usage: $0 MAJOR.MINOR.PATCH [--draft]" >&2; exit 2; }
tag="v$ver"

cd "$(git rev-parse --show-toplevel)"
[[ "$(git rev-parse --abbrev-ref HEAD)" == main ]] || { echo "not on main" >&2; exit 1; }
[[ -z "$(git status --porcelain)" ]] || { echo "working tree not clean" >&2; exit 1; }
git fetch -q origin
[[ "$(git rev-parse HEAD)" == "$(git rev-parse origin/main)" ]] || { echo "main is not in sync with origin/main" >&2; exit 1; }
git rev-parse -q --verify "refs/tags/$tag" >/dev/null && { echo "$tag already exists" >&2; exit 1; }

if [[ "$(cat VERSION 2>/dev/null)" != "$ver" ]]; then
  echo "$ver" > VERSION
  git add VERSION
  git commit -q -m "Release $tag"
fi
git tag -a "$tag" -m "Helm $tag"
git push origin main "$tag"
gh release create "$tag" --title "Helm $tag" --generate-notes "$@"
echo "released $tag"
echo "deploy: ssh vps 'cd ~/helm && git pull && docker compose up -d --build helm'"
