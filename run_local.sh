#!/bin/zsh
# Local cinema-watch runner (launchd, every 10 minutes).
# Loads .env, runs check.py, and pushes state.json changes so the repo
# stays in sync with the GitHub Actions workflow.
set -euo pipefail
cd "$(dirname "$0")"

if [ -f .env ]; then
  set -a
  source .env
  set +a
fi

./.venv/bin/python check.py

if [ -n "$(git status --porcelain state.json)" ]; then
  git add state.json
  git commit -m "chore: update watch state [local runner]"
  git push
fi
