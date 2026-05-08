#!/usr/bin/env bash
# gateway-query.sh -- run a python snippet on the VPS against the local IB Gateway.
#
# IB Gateway runs on the VPS at 127.0.0.1:4002. The engine (also on the VPS)
# connects to it natively. Operator one-off queries from the MacBook must go
# through this helper -- never tunnel or open the gateway port off-host.
#
# Usage:
#   scripts/gateway-query.sh "<python-snippet>"
#   scripts/gateway-query.sh -f path/to/snippet.py
#
# Example:
#   scripts/gateway-query.sh "from ib_async import IB
#   ib = IB(); ib.connect('127.0.0.1', 4002, clientId=99)
#   print(sum(float(v.value) for v in ib.accountValues()
#             if v.tag == 'NetLiquidation' and v.currency == 'BASE'))
#   ib.disconnect()"
#
# The snippet is piped to the remote python3 via stdin (no shell heredoc
# interpolation), so backticks, $-expansions, quotes, and EOF-shaped lines
# inside the snippet pass through unchanged and cannot escape into the remote
# shell.
#
# Convention (NOT enforced): use clientId values 90-99 for ad-hoc operator
# queries to avoid colliding with the engine (clientId 1) or backtests. The
# helper does not validate this -- if your snippet picks clientId 1 it will
# kick the engine off the gateway. Operator-responsible.

set -euo pipefail

VPS="hostinger"
SSH_USER="k2bi"
REMOTE_REPO="/home/${SSH_USER}/Projects/K2Bi"
REMOTE_PYTHON="${REMOTE_REPO}/.venv/bin/python3"

SNIPPET=""
if [[ "${1:-}" == "-f" ]]; then
    [[ -n "${2:-}" ]] || { echo "usage: gateway-query.sh -f <path-to-snippet.py>" >&2; exit 2; }
    [[ -f "$2" ]] || { echo "snippet file not found: $2" >&2; exit 2; }
    SNIPPET=$(cat "$2")
elif [[ -n "${1:-}" ]]; then
    SNIPPET="$1"
else
    echo "usage: gateway-query.sh <python-snippet>" >&2
    echo "       gateway-query.sh -f <path-to-snippet.py>" >&2
    exit 2
fi

# Pipe snippet via stdin to ssh -> remote python3 (no shell interpolation).
# ConnectTimeout fails fast if VPS unreachable; ServerAliveInterval keeps long
# queries from hanging silently on a dropped connection.
printf '%s\n' "$SNIPPET" | ssh \
    -o ConnectTimeout=10 \
    -o ServerAliveInterval=15 \
    -o ServerAliveCountMax=4 \
    "${SSH_USER}@${VPS}" \
    "cd '${REMOTE_REPO}' && '${REMOTE_PYTHON}' -"
