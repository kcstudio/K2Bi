#!/usr/bin/env bash
# Convenience wrapper for K2Bi VPS SSH calls.
# Defaults to the non-root k2bi user; set K2BI_SSH_TARGET for root-only tasks.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${K2BI_SSH_TARGET:-k2bi@hostinger}"

exec "${SCRIPT_DIR}/ssh-vps-transport.sh" "$TARGET" "$@"
