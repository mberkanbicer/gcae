#!/usr/bin/env bash
# Publish wiki/*.md to the GitHub wiki of this repository.
#
# GitHub creates <repository>.wiki.git only after the first wiki page has been saved in the
# web UI (there is no API for it), so this script explains that instead of failing obscurely.
#
# Usage:  tools/publish_wiki.sh
# Env:    GCAE_WIKI_REMOTE   wiki git remote (default: the repo's .wiki.git)
#         GCAE_REPO_URL      repository URL used to derive the wiki remote
set -euo pipefail

REPO_URL="${GCAE_REPO_URL:-https://github.com/mberkanbicer/gcae.git}"
WIKI_REMOTE="${GCAE_WIKI_REMOTE:-${REPO_URL%.git}.wiki.git}"
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../wiki" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "$WORK_DIR"' EXIT

if [ ! -d "$SOURCE_DIR" ]; then
  echo "gcae: no wiki/ directory next to this script" >&2
  exit 1
fi

if ! git clone --quiet "$WIKI_REMOTE" "$WORK_DIR/wiki" 2>/dev/null; then
  cat >&2 <<MSG
gcae: the wiki repository does not exist yet ($WIKI_REMOTE).

GitHub creates it only after the first wiki page is saved in the web UI, and there is no
API for that single step:

  1. open ${REPO_URL%.git}/wiki/_new
  2. save any first page (for example the title "Home")
  3. run this script again — it replaces the pages with wiki/ and pushes
MSG
  exit 1
fi

cp "$SOURCE_DIR"/*.md "$WORK_DIR/wiki/"

if [ -z "$(git -C "$WORK_DIR/wiki" status --porcelain)" ]; then
  echo "gcae: wiki is already up to date ($(ls "$SOURCE_DIR"/*.md | wc -l) pages)"
  exit 0
fi

git -C "$WORK_DIR/wiki" add -A
git -C "$WORK_DIR/wiki" \
  -c user.name="GCAE" -c user.email="gcae@localhost" \
  commit --quiet -m "docs(wiki): sync pages from wiki/"
git -C "$WORK_DIR/wiki" push --quiet origin HEAD
echo "gcae: published $(ls "$SOURCE_DIR"/*.md | wc -l) pages to $WIKI_REMOTE"
