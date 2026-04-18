#!/bin/bash
# deploy-to-mini.sh -- Sync K2Bi project files from MacBook to Mac Mini
#
# Usage:
#   deploy-to-mini.sh              # auto-detect what changed, sync it
#   deploy-to-mini.sh skills       # sync skills + CLAUDE.md
#   deploy-to-mini.sh scripts      # sync scripts/
#   deploy-to-mini.sh all          # sync everything syncable
#   deploy-to-mini.sh --dry-run    # show what would sync without doing it
#
# K2Bi Phase 1: no k2b-remote, no k2b-dashboard. pm2 daemons (invest-feed,
# invest-execute, invest-alert) land per-phase as each skill ships in Phase 2+.
# For now this script handles code deploy only; the vault goes over Syncthing.

set -euo pipefail

MINI="macmini"
LOCAL_BASE="$HOME/Projects/K2Bi"
REMOTE_BASE="~/Projects/K2Bi"
DRY_RUN=false
MODE="${1:-auto}"

if [[ "$MODE" == "--dry-run" ]]; then
    DRY_RUN=true
    MODE="${2:-auto}"
fi

if [[ "${2:-}" == "--dry-run" ]]; then
    DRY_RUN=true
fi

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[sync]${NC} $1"; }
warn() { echo -e "${YELLOW}[sync]${NC} $1"; }
err()  { echo -e "${RED}[sync]${NC} $1"; }

detect_changes() {
    local changes
    cd "$LOCAL_BASE"

    changes=$(git diff --name-only HEAD 2>/dev/null || true)

    if [[ -z "$changes" ]]; then
        changes=$(git diff --name-only HEAD~1 HEAD 2>/dev/null || true)
    fi

    # Include untracked top-level docs too; a newly-created CLAUDE.md / DEVLOG.md / README.md
    # still needs to trigger the skills sync (Codex P2 finding, Phase 1 Session 3).
    local untracked
    untracked=$(git ls-files --others --exclude-standard .claude/ scripts/ CLAUDE.md DEVLOG.md README.md 2>/dev/null || true)
    changes="$changes"$'\n'"$untracked"

    echo "$changes"
}

needs_skills=false
needs_scripts=false

categorize() {
    local changes="$1"
    if echo "$changes" | grep -qE '\.claude/|CLAUDE\.md|DEVLOG\.md'; then
        needs_skills=true
    fi
    if echo "$changes" | grep -qE '^scripts/'; then
        needs_scripts=true
    fi
}

sync_skills() {
    log "Syncing skills + top-level docs..."
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"

    for doc in CLAUDE.md README.md DEVLOG.md; do
        if [[ -f "$LOCAL_BASE/$doc" ]]; then
            rsync -av $rsync_flag "$LOCAL_BASE/$doc" "$MINI:$REMOTE_BASE/$doc"
        fi
    done

    rsync -av $rsync_flag --delete "$LOCAL_BASE/.claude/skills/" "$MINI:$REMOTE_BASE/.claude/skills/"

    if ! $DRY_RUN; then
        log "Verifying skills on Mini..."
        local remote_count
        remote_count=$(ssh "$MINI" "ls -d $REMOTE_BASE/.claude/skills/*/ 2>/dev/null | wc -l" | tr -d ' ')
        local local_count
        local_count=$(ls -d "$LOCAL_BASE/.claude/skills/"*/ 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$remote_count" == "$local_count" ]]; then
            log "Skills verified: $remote_count skill folders on both machines"
        else
            warn "Skill count mismatch: local=$local_count remote=$remote_count"
        fi
    fi
}

sync_scripts() {
    log "Syncing scripts/..."
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"

    rsync -av $rsync_flag "$LOCAL_BASE/scripts/" "$MINI:$REMOTE_BASE/scripts/"
}

case "$MODE" in
    skills)
        needs_skills=true
        ;;
    scripts)
        needs_scripts=true
        ;;
    all)
        needs_skills=true
        needs_scripts=true
        ;;
    auto)
        changes=$(detect_changes)
        if [[ -z "$changes" || "$changes" == $'\n' ]]; then
            warn "No changes detected. Use 'all' to force full sync."
            exit 0
        fi
        categorize "$changes"
        if ! $needs_skills && ! $needs_scripts; then
            warn "Changes detected but none in syncable categories."
            echo "$changes"
            exit 0
        fi
        ;;
    *)
        err "Unknown mode: $MODE"
        echo "Usage: deploy-to-mini.sh [auto|skills|scripts|all] [--dry-run]"
        exit 1
        ;;
esac

if ! ssh -o ConnectTimeout=5 "$MINI" "echo ok" &>/dev/null; then
    err "Cannot reach Mac Mini (ssh macmini). Is it on?"
    exit 1
fi

# Ensure remote base exists (K2Bi's first deploy will need this)
ssh "$MINI" "mkdir -p $REMOTE_BASE/.claude/skills $REMOTE_BASE/scripts" || {
    err "Failed to create remote base directories"
    exit 1
}

$DRY_RUN && warn "DRY RUN -- no files will be changed"

echo ""
log "Sync plan:"
$needs_skills && log "  - Skills + CLAUDE.md + DEVLOG.md"
$needs_scripts && log "  - scripts/"
echo ""

$needs_skills && sync_skills
$needs_scripts && sync_scripts

echo ""
if $DRY_RUN; then
    log "Dry run complete. Run without --dry-run to sync."
else
    log "Sync complete."
fi
