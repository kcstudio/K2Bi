#!/bin/bash
# deploy-to-vps.sh -- Sync K2Bi project files from MacBook to Hostinger VPS.
#
# Usage:
#   deploy-to-vps.sh              # auto-detect what changed, sync those categories
#   deploy-to-vps.sh <category>   # force a single category from deploy-config.yml
#   deploy-to-vps.sh all          # sync every category
#   deploy-to-vps.sh --dry-run    # show what would sync without doing it
#   deploy-to-vps.sh --verify-runtime  # verify VPS git checkout + hooks only
#
# The category list + the set of paths each category covers live in
# scripts/deploy-config.yml. Both this script and the /invest-ship step 12
# preflight read that file via scripts/lib/deploy_config.py. To add a new
# deployed path: append to deploy-config.yml's `targets:`. To add an
# intentionally-local path: append to `excludes:`. The preflight will block
# /ship until the drift is resolved.
#
# Phase 3.9 Stage 2 renamed this script from deploy-to-mini.sh to
# deploy-to-vps.sh and retargeted it from the Mac Mini to the Hostinger KL VPS.
# Phase G (2026-05-19): the VPS runtime root is a real git checkout. This
# script still rsyncs category payloads, but it first verifies the remote
# checkout + hooks so lifecycle code that commits from the engine runtime
# keeps its required git metadata.

set -euo pipefail

VPS="hostinger"
LOCAL_BASE="$HOME/Projects/K2Bi"
REMOTE_BASE="/home/k2bi/Projects/K2Bi"
CONFIG_HELPER="$LOCAL_BASE/scripts/lib/deploy_config.py"
SSH_TRANSPORT="$LOCAL_BASE/scripts/ssh-vps-transport.sh"
SSH_OPTS=(-o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
DRY_RUN=false
VERIFY_RUNTIME=false
MODE=""
RESTART_FAILED=false
RESTART_SERVICES=""
REMOTE_GIT_SHA=""
REMOTE_BACKUP_DIR=""
REMOTE_BACKUP_RETENTION_DAYS=14
RSYNC_EXCLUDE_ARGS=(--exclude '__pycache__/' --exclude '*.pyc' --exclude '.venv/')

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()  { echo -e "${GREEN}[sync]${NC} $1"; }
warn() { echo -e "${YELLOW}[sync]${NC} $1"; }
err()  { echo -e "${RED}[sync]${NC} $1"; }

# --- arg parse ------------------------------------------------------------

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=true ;;
        --verify-runtime) VERIFY_RUNTIME=true ;;
        *) MODE="${MODE:-$arg}" ;;
    esac
done
MODE="${MODE:-auto}"

if [ ! -f "$CONFIG_HELPER" ]; then
    err "deploy-config helper missing at $CONFIG_HELPER"
    exit 2
fi

KNOWN_CATEGORIES=$(python3 "$CONFIG_HELPER" list-categories)

# --- helpers --------------------------------------------------------------

ssh_vps() {
    "$SSH_TRANSPORT" "${SSH_OPTS[@]}" "k2bi@$VPS" "$@" </dev/null
}

hook_wrapper_sha() {
    local hook="$1"
    HOOK_NAME="$hook" python3 - <<'PY'
import hashlib
import os

hook = os.environ["HOOK_NAME"]
body = f"""#!/usr/bin/env bash
set -euo pipefail
REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ -x "$REPO_ROOT/.venv/bin/python3" ]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi
exec "$REPO_ROOT/.githooks/{hook}" "$@"
""".encode()
print(hashlib.sha256(body).hexdigest())
PY
}

verify_remote_githooks_match_local() {
    local diff_output
    if [[ ! -d "$LOCAL_BASE/.githooks" ]]; then
        err "Local .githooks directory is missing; cannot verify VPS hook source tree."
        return 1
    fi
    diff_output=$(rsync -r --checksum --dry-run --delete --out-format='%n' \
        -e "$SSH_TRANSPORT" \
        "${RSYNC_EXCLUDE_ARGS[@]}" \
        "$LOCAL_BASE/.githooks/" "k2bi@$VPS:$REMOTE_BASE/.githooks/")
    if [[ -n "$diff_output" ]]; then
        err "Remote .githooks differs from local tracked hooks at $REMOTE_BASE/.githooks."
        printf '%s\n' "$diff_output" >&2
        return 1
    fi
}

verify_remote_git_checkout() {
    local expected_sha="${1:-}"
    local remote_sha remote_short expected_short
    local expected_pre_commit_sha expected_commit_msg_sha expected_post_commit_sha
    expected_pre_commit_sha="$(hook_wrapper_sha pre-commit)"
    expected_commit_msg_sha="$(hook_wrapper_sha commit-msg)"
    expected_post_commit_sha="$(hook_wrapper_sha post-commit)"
    if ! ssh_vps "
        set -e
        cd '$REMOTE_BASE'
        test \"\$(git rev-parse --is-inside-work-tree 2>/dev/null)\" = true
        test \"\$(git rev-parse --show-toplevel)\" = '$REMOTE_BASE'
        git rev-parse --verify HEAD >/dev/null
        hooks_path_rc=0
        hooks_path=\"\$(git config --get core.hooksPath)\" || hooks_path_rc=\$?
        if [ \"\$hooks_path_rc\" -eq 0 ]; then
            test \"\$hooks_path\" = '.git/hooks'
        else
            test \"\$hooks_path_rc\" -eq 1
        fi
        test -x .githooks/pre-commit
        test -x .githooks/commit-msg
        test -x .githooks/post-commit
        check_hook_wrapper() {
            hook=\"\$1\"
            expected_sha=\"\$2\"
            test -x \".git/hooks/\$hook\"
            bash -n \".git/hooks/\$hook\"
            actual_sha=\"\$(sha256sum \".git/hooks/\$hook\" | awk '{print \$1}')\"
            test \"\$actual_sha\" = \"\$expected_sha\"
        }
        check_hook_wrapper pre-commit '$expected_pre_commit_sha'
        check_hook_wrapper commit-msg '$expected_commit_msg_sha'
        check_hook_wrapper post-commit '$expected_post_commit_sha'
        test -z \"\${K2BI_SKIP_POST_COMMIT_MIRROR:-}\"
        if command -v systemctl >/dev/null 2>&1; then
            ! systemctl show -p Environment k2bi-engine.service \
                | grep -q 'K2BI_SKIP_POST_COMMIT_MIRROR=1'
        fi
        tmp_msg=\"\$(mktemp)\"
        trap 'rm -f \"\$tmp_msg\"' EXIT
        printf 'chore(vps): hook verify\n' > \"\$tmp_msg\"
        .git/hooks/pre-commit >/dev/null
        .git/hooks/commit-msg \"\$tmp_msg\" >/dev/null
        K2BI_SKIP_POST_COMMIT_MIRROR=1 .git/hooks/post-commit >/dev/null
        rm -f \"\$tmp_msg\"
        trap - EXIT
    "; then
        err "Remote VPS runtime is not a git checkout with installed hooks at $REMOTE_BASE."
        err "Convert it first: clone/fetch K2Bi on the VPS, checkout the intended commit, and install .git/hooks wrappers before running /sync."
        return 1
    fi
    if ! verify_remote_githooks_match_local; then
        return 1
    fi
    if ! remote_sha=$(ssh_vps "
        set -e
        cd '$REMOTE_BASE'
        git rev-parse HEAD
    "); then
        err "Remote VPS runtime verification could not read git HEAD."
        return 1
    fi
    remote_sha=$(printf '%s\n' "$remote_sha" | tail -n 1 | tr -d '\r')
    if [[ ! "$remote_sha" =~ ^[0-9a-f]{40}$ ]]; then
        err "Remote VPS runtime verification did not return a git HEAD SHA."
        return 1
    fi
    REMOTE_GIT_SHA="$remote_sha"
    if [[ -n "$expected_sha" && "$remote_sha" != "$expected_sha" ]]; then
        remote_short="${remote_sha:0:7}"
        expected_short="${expected_sha:0:7}"
        err "Remote VPS runtime HEAD mismatch: remote=$remote_sha local=$expected_sha."
        err "Fetch/checkout the VPS runtime to the approved deploy commit before using this verify-only gate."
        err "Short form: remote=${remote_short:-unknown} local=${expected_short:-unknown}."
        return 1
    fi
    log "Remote git checkout verified at ${remote_sha:0:7}."
    if [[ -n "$expected_sha" ]]; then
        log "Remote git checkout HEAD matches local baseline ${expected_sha:0:7}."
    fi
}

category_requires_restart() {
    case "$1" in
        execution|pm2) return 0 ;;
        *) return 1 ;;
    esac
}

restart_service_for_category() {
    case "$1" in
        execution|pm2) printf '%s\n' "k2bi-engine.service" ;;
    esac
}

mark_restart_service() {
    local service="$1"
    [[ -z "$service" ]] && return 0
    case $'\n'"$RESTART_SERVICES" in
        *$'\n'"$service"$'\n'*) return 0 ;;
    esac
    RESTART_SERVICES="${RESTART_SERVICES}${service}"$'\n'
}

targets_for_categories() {
    local category
    while IFS= read -r category; do
        [[ -z "$category" ]] && continue
        python3 "$CONFIG_HELPER" list-targets "$category"
    done <<< "$1" | awk 'NF' | sort -u
}

create_remote_payload_backup() {
    local targets="$1"
    [[ -z "$targets" ]] && return 0
    REMOTE_BACKUP_DIR=".sync-state/deploy-backups/$(date -u +%Y%m%dT%H%M%SZ)-$$"
    log "Creating VPS payload backup at $REMOTE_BACKUP_DIR"
    ssh_vps "
        set -e
        cd '$REMOTE_BASE'
        repo_root=\"\$(pwd -P)\"
        backup='$REMOTE_BACKUP_DIR'
        backup_parent=\"\$(dirname \"\$backup\")\"
        mkdir -p \"\$backup_parent\"
        backup_parent_real=\"\$(realpath -m \"\$backup_parent\")\"
        case \"\$backup_parent_real/\" in
            \"\$repo_root/\"*) ;;
            *) echo \"Refusing to create backup outside repo: \$backup_parent\" >&2; exit 1 ;;
        esac
        rm -rf -- \"\$backup\"
        mkdir -p \"\$backup/payload\" \"\$backup/absent\"
        validate_deploy_target() {
            stripped=\"\$1\"
            case \"\$stripped\" in
                ''|/*|*'..'*) echo \"invalid deploy target: \$stripped\" >&2; exit 1 ;;
            esac
            parent=\"\$(dirname \"\$stripped\")\"
            parent_real=\"\$(realpath -m \"\$parent\")\"
            case \"\$parent_real/\" in
                \"\$repo_root/\"*) ;;
                *) echo \"Refusing target with parent outside repo: \$stripped\" >&2; exit 1 ;;
            esac
            if [ -e \"\$stripped\" ] || [ -L \"\$stripped\" ]; then
                target_real=\"\$(realpath -m \"\$stripped\")\"
                case \"\$target_real/\" in
                    \"\$repo_root/\"*) ;;
                    *) echo \"Refusing target outside repo: \$stripped\" >&2; exit 1 ;;
                esac
            fi
        }
        while IFS= read -r target; do
            [ -z \"\$target\" ] && continue
            stripped=\"\${target%/}\"
            validate_deploy_target \"\$stripped\"
            parent=\"\$(dirname \"\$stripped\")\"
            if [ -d \"\$stripped\" ] || [ -f \"\$stripped\" ]; then
                mkdir -p \"\$backup/payload/\$parent\"
                cp -a \"\$stripped\" \"\$backup/payload/\$parent/\"
            else
                mkdir -p \"\$backup/absent/\$parent\"
                touch \"\$backup/absent/\$stripped\"
            fi
        done <<'K2BI_DEPLOY_TARGETS'
$targets
K2BI_DEPLOY_TARGETS
    "
}

restore_remote_payload_backup() {
    local targets="$1"
    [[ -z "$REMOTE_BACKUP_DIR" || -z "$targets" ]] && return 0
    warn "Restoring VPS payload backup from $REMOTE_BACKUP_DIR"
    ssh_vps "
        set -e
        cd '$REMOTE_BASE'
        repo_root=\"\$(pwd -P)\"
        backup='$REMOTE_BACKUP_DIR'
        test -d \"\$backup\"
        backup_real=\"\$(realpath -m \"\$backup\")\"
        payload_root=\"\$backup/payload\"
        payload_root_real=\"\$(realpath -m \"\$payload_root\")\"
        case \"\$backup_real/\" in
            \"\$repo_root/\"*) ;;
            *) echo \"Refusing backup outside repo: \$backup\" >&2; exit 1 ;;
        esac
        validate_restore_target() {
            stripped=\"\$1\"
            case \"\$stripped\" in
                ''|/*|*'..'*) echo \"invalid deploy target: \$stripped\" >&2; exit 1 ;;
            esac
            parent=\"\$(dirname \"\$stripped\")\"
            parent_real=\"\$(realpath -m \"\$parent\")\"
            case \"\$parent_real/\" in
                \"\$repo_root/\"*) ;;
                *) echo \"Refusing to restore target outside repo: \$stripped\" >&2; exit 1 ;;
            esac
            if [ -e \"\$stripped\" ] || [ -L \"\$stripped\" ]; then
                target_real=\"\$(realpath -m \"\$stripped\")\"
                case \"\$target_real/\" in
                    \"\$repo_root/\"*) ;;
                    *) echo \"Refusing to restore target outside repo: \$stripped\" >&2; exit 1 ;;
                esac
            fi
            if [ -e \"\$payload_root/\$stripped\" ] || [ -L \"\$payload_root/\$stripped\" ]; then
                payload_real=\"\$(realpath -m \"\$payload_root/\$stripped\")\"
                case \"\$payload_real/\" in
                    \"\$payload_root_real/\"*) ;;
                    *) echo \"Refusing backup payload outside backup root: \$stripped\" >&2; exit 1 ;;
                esac
            fi
        }
        while IFS= read -r target; do
            [ -z \"\$target\" ] && continue
            stripped=\"\${target%/}\"
            validate_restore_target \"\$stripped\"
            parent=\"\$(dirname \"\$stripped\")\"
            rm -rf -- \"\$stripped\"
            if [ -e \"\$backup/payload/\$stripped\" ]; then
                mkdir -p \"\$parent\"
                cp -a \"\$backup/payload/\$stripped\" \"\$parent/\"
            fi
        done <<'K2BI_DEPLOY_TARGETS'
$targets
K2BI_DEPLOY_TARGETS
    "
}

cleanup_remote_payload_backup() {
    [[ -z "$REMOTE_BACKUP_DIR" ]] && return 0
    ssh_vps "
        set -e
        cd '$REMOTE_BASE'
        repo_root=\"\$(pwd -P)\"
        backup='$REMOTE_BACKUP_DIR'
        backup_parent='.sync-state/deploy-backups'
        mkdir -p \"\$backup_parent\"
        backup_parent_real=\"\$(realpath -m \"\$backup_parent\")\"
        case \"\$backup_parent_real/\" in
            \"\$repo_root/\"*) ;;
            *) echo \"Refusing backup cleanup outside repo: \$backup_parent\" >&2; exit 1 ;;
        esac
        rm -rf -- \"\$backup\"
        find .sync-state/deploy-backups -mindepth 1 -maxdepth 1 -type d \
            -mtime +$REMOTE_BACKUP_RETENTION_DAYS -exec rm -rf -- {} +
    " || {
        warn "Could not remove remote deploy backup $REMOTE_BACKUP_DIR"
        warn "Manual cleanup may be needed under $REMOTE_BASE/.sync-state/deploy-backups"
        return 0
    }
}

verify_rsync_target_clean() {
    local local_rel="$1"
    local stripped="${local_rel%/}"
    local diff_output=""

    if [[ -d "$LOCAL_BASE/$stripped" ]]; then
        diff_output=$(rsync -r --checksum --dry-run --delete --out-format='%n' \
            -e "$SSH_TRANSPORT" \
            "${RSYNC_EXCLUDE_ARGS[@]}" \
            "$LOCAL_BASE/$stripped/" "k2bi@$VPS:$REMOTE_BASE/$stripped/")
    elif [[ -f "$LOCAL_BASE/$stripped" ]]; then
        diff_output=$(rsync --checksum --dry-run --out-format='%n' \
            -e "$SSH_TRANSPORT" \
            "$LOCAL_BASE/$stripped" "k2bi@$VPS:$REMOTE_BASE/$stripped")
    else
        if ! ssh_vps "test ! -e '$REMOTE_BASE/$stripped'"; then
            err "Post-rsync payload integrity check failed for deleted target $stripped"
            return 1
        fi
    fi

    if [[ -n "$diff_output" ]]; then
        err "Post-rsync payload integrity check failed for $stripped"
        printf '%s\n' "$diff_output" >&2
        return 1
    fi
}

verify_synced_payloads() {
    local targets="$1"
    local target
    $DRY_RUN && return 0
    while IFS= read -r target; do
        [[ -z "$target" ]] && continue
        verify_rsync_target_clean "$target" || return 1
    done <<< "$targets"
}

detect_changed_categories() {
    # Ask the config helper for the set of categories with pending changes
    # since the last successful sync (sentinel at .sync-state/last-synced-commit).
    # Canonical implementation unions uncommitted diffs, untracked files, and
    # committed-since-sentinel diffs. Replaces the cycle-2 `git diff HEAD~1
    # HEAD` fallback, which silently dropped earlier commits once a devlog
    # follow-up commit landed on top (the cycle-5 carry-over bug).
    #
    # $1 is the pinned baseline SHA (captured at run start). Passed through
    # to detect-categories + record-sync so the sentinel never advances past
    # content the rsync plan did not see (Codex R7 final-gate F1).
    cd "$LOCAL_BASE"
    local baseline="${1:-}"
    if [[ -n "$baseline" ]]; then
        python3 "$CONFIG_HELPER" detect-categories --head "$baseline"
    else
        python3 "$CONFIG_HELPER" detect-categories
    fi
}

rsync_target() {
    # rsync one deploy-config.yml target (file or directory). Handles the
    # local-deleted-but-remote-present case so the VPS mirrors deletions.
    local local_rel="$1"    # path relative to LOCAL_BASE; from config verbatim
    local rsync_flag=""
    $DRY_RUN && rsync_flag="--dry-run"
    local stripped="${local_rel%/}"

    if [[ -d "$LOCAL_BASE/$stripped" ]]; then
        rsync -av $rsync_flag --delete \
            -e "$SSH_TRANSPORT" \
            "${RSYNC_EXCLUDE_ARGS[@]}" \
            "$LOCAL_BASE/$stripped/" "k2bi@$VPS:$REMOTE_BASE/$stripped/"
    elif [[ -f "$LOCAL_BASE/$stripped" ]]; then
        rsync -av $rsync_flag -e "$SSH_TRANSPORT" \
            "$LOCAL_BASE/$stripped" "k2bi@$VPS:$REMOTE_BASE/$stripped"
    else
        if $DRY_RUN; then
            warn "  (dry-run) would remove k2bi@$VPS:$REMOTE_BASE/$stripped if present"
            return 0
        fi
        # Mirror local deletion to remote so state stays consistent.
        local result
        result=$(ssh_vps "
            if [ -d $REMOTE_BASE/$stripped ]; then
                rm -rf $REMOTE_BASE/$stripped && echo REMOVED_DIR
            elif [ -f $REMOTE_BASE/$stripped ]; then
                rm $REMOTE_BASE/$stripped && echo REMOVED_FILE
            else
                echo ABSENT
            fi
        ")
        case "$result" in
            REMOVED_DIR)  log "  removed remote tree $stripped (deleted locally)" ;;
            REMOVED_FILE) log "  removed remote file $stripped (deleted locally)" ;;
        esac
    fi
}

sync_category() {
    local category="$1"
    log "Syncing category: $category"
    local targets
    targets=$(python3 "$CONFIG_HELPER" list-targets "$category")
    if [[ -z "$targets" ]]; then
        warn "  (no targets in category $category)"
        return 0
    fi
    while IFS= read -r target; do
        [[ -z "$target" ]] && continue
        rsync_target "$target"
    done <<< "$targets"

    # Skills category preserves the Phase 1 verify-count sanity check so a
    # drift between MacBook and VPS skill-folder counts surfaces loudly.
    if [[ "$category" == "skills" ]] && ! $DRY_RUN; then
        local remote_count local_count
        remote_count=$(ssh_vps "ls -d $REMOTE_BASE/.claude/skills/*/ 2>/dev/null | wc -l" | tr -d ' ')
        local_count=$(ls -d "$LOCAL_BASE/.claude/skills/"*/ 2>/dev/null | wc -l | tr -d ' ')
        if [[ "$remote_count" == "$local_count" ]]; then
            log "  skills verified: $remote_count skill folders on both machines"
        else
            warn "  skills count mismatch: local=$local_count remote=$remote_count"
        fi
    fi

    # Service restarts are deferred until after all payload files are synced and
    # the runtime git hooks have been re-verified. That keeps a scripts/ sync
    # from breaking hook execution after an execution/ restart already happened.
    if ! $DRY_RUN && category_requires_restart "$category"; then
        mark_restart_service "$(restart_service_for_category "$category")"
    fi
}

restart_synced_services() {
    local service
    while IFS= read -r service; do
        [[ -z "$service" ]] && continue
        log "  restarting $service on VPS"
        ssh_vps "sudo systemctl restart $service" || {
            err "  RESTART FAILED for service '$service'. Sync sentinel will NOT advance; re-run /sync after resolving the restart issue."
            RESTART_FAILED=true
        }
    done <<< "$RESTART_SERVICES"
}

# --- mode resolution ------------------------------------------------------

is_known_category() {
    local candidate="$1"
    local cat
    while IFS= read -r cat; do
        [[ "$cat" == "$candidate" ]] && return 0
    done <<< "$KNOWN_CATEGORIES"
    return 1
}

# Codex R7 final-gate F1: capture the baseline SHA ONCE at run start and
# thread it through detection + record-sync. Without this, a commit that
# lands locally while rsync is copying files would advance the sentinel
# even though its content was never part of the sync plan.
#
# MiniMax R7 R2 F1: a silent capture failure (empty BASELINE_SHA) causes
# detect + record-sync to each re-resolve HEAD independently and
# potentially disagree. For a real sync run, that is a correctness gap
# we must surface loudly. --dry-run bypasses record-sync entirely, so
# baseline inconsistency there is observational; still require git so
# the dry-run reflects what the real run would do.
BASELINE_SHA="$(cd "$LOCAL_BASE" && git rev-parse HEAD 2>/dev/null || true)"
if [[ -z "$BASELINE_SHA" ]]; then
    err "Cannot capture baseline SHA: \`git rev-parse HEAD\` failed in $LOCAL_BASE."
    err "Deploy requires an initialised git repo with at least one commit."
    err "If this is a fresh clone, finish \`git clone\` before running /sync."
    exit 1
fi

RUN_CATEGORIES=""
case "$MODE" in
    auto)
        RUN_CATEGORIES=$(detect_changed_categories "$BASELINE_SHA")
        if [[ -z "$RUN_CATEGORIES" ]]; then
            warn "No pending changes since last sync. Use 'all' to force full sync."
            exit 0
        fi
        ;;
    all)
        RUN_CATEGORIES="$KNOWN_CATEGORIES"
        ;;
    *)
        if ! is_known_category "$MODE"; then
            err "Unknown mode: $MODE"
            echo "Valid modes: auto | all | --dry-run | $(echo "$KNOWN_CATEGORIES" | tr '\n' ' ')"
            exit 1
        fi
        RUN_CATEGORIES="$MODE"
        ;;
esac
RUN_TARGETS=$(targets_for_categories "$RUN_CATEGORIES")

# --- execute --------------------------------------------------------------

if ! ssh_vps "echo ok" &>/dev/null; then
    err "Cannot reach Hostinger VPS through scripts/ssh-vps-transport.sh. Is it on?"
    exit 1
fi
verify_expected_sha=""
if $VERIFY_RUNTIME; then
    verify_expected_sha="$BASELINE_SHA"
fi
if ! verify_remote_git_checkout "$verify_expected_sha"; then
    exit 1
fi

if $VERIFY_RUNTIME; then
    log "Runtime verification complete."
    exit 0
fi

# Ensure the remote repo root + each category's directory prefix exist.
# The rsync commands build directory structure as they go, but a first-time
# deploy into a virgin REMOTE_BASE needs the parent there.
REMOTE_MKDIRS=$(python3 "$CONFIG_HELPER" list-targets | awk -F/ '{if ($1 != $0) print $1}' | sort -u)
ssh_vps "mkdir -p $REMOTE_BASE $(echo "$REMOTE_MKDIRS" | awk -v base="$REMOTE_BASE" '{print base"/"$0}' | tr '\n' ' ')" || {
    err "Failed to create remote base directories"
    exit 1
}

if ! $DRY_RUN; then
    create_remote_payload_backup "$RUN_TARGETS"
fi

$DRY_RUN && warn "DRY RUN -- no files will be changed"

echo ""
log "Sync plan:"
while IFS= read -r cat; do
    [[ -z "$cat" ]] && continue
    log "  category: $cat"
done <<< "$RUN_CATEGORIES"
echo ""

while IFS= read -r cat; do
    [[ -z "$cat" ]] && continue
    sync_category "$cat"
done <<< "$RUN_CATEGORIES"

if ! verify_synced_payloads "$RUN_TARGETS"; then
    err "Post-rsync payload integrity check failed. Sync sentinel will NOT advance."
    restore_remote_payload_backup "$RUN_TARGETS"
    exit 1
fi

if ! verify_remote_git_checkout; then
    err "Post-sync runtime verification failed. Sync sentinel will NOT advance."
    err "Service restarts have not run yet. Restoring the pre-sync payload backup before exit."
    restore_remote_payload_backup "$RUN_TARGETS"
    exit 1
fi

restart_synced_services

echo ""
if $RESTART_FAILED; then
    err "Deploy NOT recorded -- one or more systemctl restarts failed during sync. Working tree is rsync'd but engine state is uncertain. Re-run after resolving the restart issue (likely missing sudoers rule on VPS for k2bi user; see K2Bi-Vault/wiki/planning/feature_vps-migration.md gotcha #9)."
    exit 3
fi

if $DRY_RUN; then
    log "Dry run complete. Run without --dry-run to sync."
else
    # Record the post-sync HEAD so the next auto-detect can diff from here.
    # Pass the pinned baseline SHA so the sentinel matches the snapshot we
    # actually synced even if HEAD advanced mid-run (Codex R7 final-gate F1).
    # A sentinel write failure is not fatal -- the sync itself succeeded; we
    # just warn so Keith knows the next auto run may over-sync.
    record_sync_args=()
    if [[ -n "$BASELINE_SHA" ]]; then
        record_sync_args+=(--sha "$BASELINE_SHA")
    fi
    if ! python3 "$CONFIG_HELPER" record-sync "${record_sync_args[@]}"; then
        warn "Sync succeeded but sentinel write failed -- next auto run may over-sync."
    fi
    cleanup_remote_payload_backup
    log "Sync complete."
fi
