#!/usr/bin/env bash
# PreToolUse guard: force all code-review invocations through scripts/review.sh.
#
# Blocks direct Bash-tool calls to:
#   * codex-companion.mjs review ...           (Codex plugin's underlying binary)
#   * scripts/minimax-review.sh                (MiniMax shell wrapper)
#   * codex CLI review subcommand              (the `codex review` vendor CLI)
#
# Allowed:
#   * scripts/review.sh <anything>
#   * scripts/review-poll.sh <job_id>
#   * scripts/lib/review_runner.py <...>
#
# Why: both reviewers are slow enough (typical 60-220s, worst-case 10+ min
# when Codex's WebSocket reconnect-storms) that a foreground call looks
# hung. scripts/review.sh enforces deadline + heartbeat + automatic
# Codex->MiniMax fallback, so the ship flow can never stall on a silent
# reviewer. The wrapper itself calls these tools via subprocess.Popen,
# which does not route through this hook.
#
# Exit codes: 0 allow; 2 block (message on stderr is shown to the model).

set -euo pipefail

# Fail-closed on any dependency or input error. Silent pass-through on
# broken inputs would bypass the entire review-wrapper enforcement.
command -v python3 >/dev/null 2>&1 || {
  echo "[review-guard] FAIL-CLOSED: python3 not found in PATH" >&2
  exit 2
}

input="$(cat)"
if [ -z "$input" ]; then
  echo "[review-guard] FAIL-CLOSED: empty hook stdin (payload missing)" >&2
  exit 2
fi

cmd="$(python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("command", ""))
except Exception:
    sys.exit(3)' <<<"$input" 2>/dev/null)" || {
  echo "[review-guard] FAIL-CLOSED: unparseable hook input JSON" >&2
  exit 2
}

# Whitelist the unified wrapper paths first so we never block ourselves.
case "$cmd" in
  *scripts/review.sh*|*scripts/review-poll.sh*|*scripts/lib/review_runner.py*)
    exit 0 ;;
esac

# Diagnostic invocations (help/status/version/etc.) pass through untouched.
# Only real review launches should be blocked.
if echo "$cmd" | grep -qE '(^|[[:space:]])(--help|-h|--version)([[:space:]]|$)'; then
  exit 0
fi

block() {
  echo "[review-guard] BLOCKED: direct $1 invocation is not allowed." >&2
  echo "[review-guard] Use scripts/review.sh instead -- it enforces the deadline," >&2
  echo "               heartbeat, and Codex->MiniMax fallback that prevent" >&2
  echo "               the ship flow from stalling on a silent reviewer." >&2
  echo "[review-guard] Example: scripts/review.sh diff --files \"\$FILES\"" >&2
  exit 2
}

# Codex: only block the actual review-launch subcommands, not diagnostics
# like `codex-companion.mjs --help`, `status`, `result`, `task`, etc.
# Pattern: codex-companion.mjs ... (review|adversarial-review) <flag-or-arg>
if echo "$cmd" | \
    grep -qE 'codex-companion\.mjs([[:space:]]+[^[:space:]]+)*[[:space:]]+(review|adversarial-review)([[:space:]]+--|[[:space:]]|$)'; then
  block "codex-companion.mjs review"
fi

# MiniMax wrapper: block any actual invocation. Hook already excluded --help
# above, so this only catches real review launches.
case "$cmd" in
  *scripts/minimax-review.sh*) block "minimax-review.sh" ;;
esac

exit 0
