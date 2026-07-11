#!/bin/bash
# Daily Substack sync, run from this Mac (Substack blocks GitHub's server IPs,
# so the sync can't run in GitHub Actions — see README).
# Installed as a launchd agent: com.canyoucrossthestreet.sync
set -euo pipefail

cd "$(dirname "$0")/.."

# self-contained python environment; recreate it if the repo was moved
# (venvs hardcode their creation path and break after a move)
if ! ./.venv/bin/python -c 'pass' 2>/dev/null; then
  rm -rf .venv
  python3 -m venv .venv
fi
./.venv/bin/python -m pip install --quiet -r requirements.txt

git pull --rebase --quiet

LOG=$(mktemp)
./.venv/bin/python scripts/sync_substack.py | tee "$LOG"

if [ -n "$(git status --porcelain content/posts)" ]; then
  titles=$(grep '^NEW: ' "$LOG" | sed 's/^NEW: //' | tr '\n' ',' | sed 's/,$//; s/,/, /g')
  git add content/posts
  git commit -m "sync: ${titles:-new Substack post(s)}"
  git push
  echo "pushed: ${titles}"
else
  echo "no changes to push"
fi
rm -f "$LOG"
