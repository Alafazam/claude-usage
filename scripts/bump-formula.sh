#!/usr/bin/env bash
set -euo pipefail

# Repoint the in-tree Homebrew formula at a released tag's source tarball.
#
# WHY THIS EXISTS
#   Formula/claude-usage.rb ships *inside* the repo it installs, so it can never
#   hash its own release tarball — the sha256 would have to be the hash of a file
#   that contains that very hash (self-referential, uncomputable). So the formula
#   must point at an ALREADY-FROZEN tag: the previous release.
#
#   Brew reads the formula from the tap's default branch (main), never from DEV
#   or a tag. Run this on DEV while prepping a release; the change reaches brew
#   users through the normal DEV -> main release merge. It never touches main
#   directly, so it dodges main's branch protection. Net effect: brew tracks one
#   release behind, and it advances automatically every release instead of being
#   hand-edited (which is how the pin silently rotted at v1.1.0 for months).
#
# USAGE
#   scripts/bump-formula.sh [TAG]
#     TAG   tag to pin at (e.g. v1.5.2). Defaults to the latest v* tag on origin,
#           which — run during release prep, before the new tag exists — is the
#           previous release. Review the diff, then commit on DEV.
#
# See AGENTS.md "Homebrew formula and self-referential SHA".

REPO_SLUG="phuryn/claude-usage"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FORMULA="$REPO_DIR/Formula/claude-usage.rb"

TAG="${1:-}"
if [ -z "$TAG" ]; then
  echo "🔎  No tag given — finding the latest v* tag on origin..."
  git -C "$REPO_DIR" fetch --tags --quiet origin || true
  TAG="$(git -C "$REPO_DIR" ls-remote --tags --refs origin 'v*' \
    | awk -F/ '{print $NF}' | sort -V | tail -n1)"
fi
[ -n "$TAG" ] || { echo "❌  Could not determine a tag to pin at." >&2; exit 1; }

VERSION="${TAG#v}"
URL="https://github.com/${REPO_SLUG}/archive/refs/tags/${TAG}.tar.gz"

echo "📌  Pinning formula at ${TAG}"
echo "    ${URL}"

TARBALL="$(mktemp)"
trap 'rm -f "$TARBALL"' EXIT
curl -fsSL "$URL" -o "$TARBALL"

if command -v sha256sum >/dev/null 2>&1; then
  SHA="$(sha256sum "$TARBALL" | awk '{print $1}')"
else
  SHA="$(shasum -a 256 "$TARBALL" | awk '{print $1}')"
fi
[ -n "$SHA" ] || { echo "❌  Could not compute sha256." >&2; exit 1; }
echo "🔑  sha256 ${SHA}"

# Rewrite exactly the three pinned lines. Anchored to `^<ws>url `/`version `/
# `sha256 ` so the `head`, `homepage`, and comment lines are left untouched
# (there is exactly one of each pinned line in the stanza).
tmp="$(mktemp)"
sed -E \
  -e "s|^([[:space:]]*)url .*|\1url \"${URL}\"|" \
  -e "s|^([[:space:]]*)version .*|\1version \"${VERSION}\"|" \
  -e "s|^([[:space:]]*)sha256 .*|\1sha256 \"${SHA}\"|" \
  "$FORMULA" > "$tmp"
mv "$tmp" "$FORMULA"

echo
echo "✅  Updated $(git -C "$REPO_DIR" rev-parse --show-prefix 2>/dev/null)Formula/claude-usage.rb:"
grep -E '^[[:space:]]*(url|version|sha256) ' "$FORMULA" | sed 's/^/    /'
echo
echo "➡   Review, then commit on DEV. It ships to brew users at the next DEV -> main release merge."
