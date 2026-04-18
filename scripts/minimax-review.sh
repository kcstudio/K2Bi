#!/usr/bin/env bash
# Standalone MiniMax M2.7 adversarial code reviewer.
# Phase A MVP: working-tree scope, single-shot, JSON output.
# Touches nothing in /ship or the codex plugin -- runs as its own tool.
#
# Usage:
#   scripts/minimax-review.sh                       # review working tree
#   scripts/minimax-review.sh --focus "auth path"   # extra focus area
#   scripts/minimax-review.sh --json                # raw JSON to stdout
#   scripts/minimax-review.sh --model MiniMax-M2.5  # try a different model
#
# Endpoint pinned to https://api.minimaxi.com (global). Override with
# MINIMAX_API_HOST env var if you really need to.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Non-interactive shells (cron, pm2, Bash tool) don't load ~/.zshrc, so the
# MiniMax key won't be in env. Source it if available; minimax_common.py also
# parses ~/.zshrc as a last resort.
if [ -z "${MINIMAX_API_KEY:-}" ] && [ -f "$HOME/.zshrc" ]; then
  # shellcheck disable=SC1091
  source "$HOME/.zshrc" 2>/dev/null || true
fi

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || true)"
if [ -z "$REPO_ROOT" ]; then
  echo "minimax-review: must be run inside a git repository" >&2
  exit 1
fi

cd "$REPO_ROOT"
exec python3 "$SCRIPT_DIR/lib/minimax_review.py" "$@"
