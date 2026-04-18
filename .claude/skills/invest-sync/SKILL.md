---
name: invest-sync
description: Sync K2Bi project files to the Mac Mini server -- detects what changed, syncs skills/code/scripts, rebuilds if needed. Use when Keith says /sync, "sync to mini", "deploy to mini", "push to mini", or after K2Bi modifies project files (skills, CLAUDE.md, invest-remote code (Phase 4), scripts).
---

# K2B Sync to Mac Mini

Push K2B project file changes from MacBook to the always-on Mac Mini server.

## When to Trigger

**Explicitly:** Keith says `/sync`, "sync to mini", "deploy", "push to mini".

**Proactively prompt Keith** at the end of any session where K2B modified files in:
- `.claude/skills/` (any skill SKILL.md)
- `CLAUDE.md` or `K2B_ARCHITECTURE.md`
- `k2b-remote/` (bot code)
- `k2b-dashboard/` (dashboard app -- full-stack React/Vite + Express, requires build + pm2 restart on Mini)
- `scripts/` (utility scripts)

Say: "Changes were made to [list]. These are on your MacBook only. Run /sync to push to Mac Mini?"

Do NOT auto-sync without Keith's confirmation. Always ask first.

## Commands

- `/sync` -- auto-detect what changed and sync it
- `/sync skills` -- sync only skills + CLAUDE.md + K2B_ARCHITECTURE.md
- `/sync code` -- sync only invest-remote code (Phase 4) (+ build + restart pm2)
- `/sync dashboard` -- sync only k2b-dashboard (+ npm run build + pm2 restart k2b-dashboard)
- `/sync scripts` -- sync only scripts/
- `/sync all` -- force full sync of everything
- `/sync status` -- check what's out of sync without changing anything

## Paths

- Deploy script: `~/Projects/K2Bi/scripts/deploy-to-mini.sh`
- Mac Mini SSH alias: `macmini`
- Remote K2B project: `~/Projects/K2Bi/` on Mac Mini

## Workflow

### 0. Consume the pending-sync mailbox

Before detecting changes in the current session, check the **current repo's** `.pending-sync/` mailbox directory (resolved from `git rev-parse --show-toplevel`, not hardcoded) for entries deferred by `/ship --defer` in this or a previous session. For K2B sessions this resolves to `~/Projects/K2Bi/.pending-sync/`. Each entry is a single JSON file written atomically; multiple entries accumulate if Keith defers several ships before running `/sync`. `/sync` is the sole consumer: it reads entries, folds them into the sync scope, runs the deploy, and deletes each entry it successfully processed -- **by filename**, never by a rewrite-compare pattern. This makes the protocol race-free without locks.

> **Fork-friendly, not auto-portable.** The mailbox path is derived per-repo so this skill body doesn't have to be edited at fork time, but the rsync targets and deploy-script invocations later in this skill (lines starting around "Detect Changes" and "Deployment") are still hardcoded to `~/Projects/K2Bi/`. A sibling repo that forks this skill MUST also swap those paths to its own targets, otherwise `/sync` would consume a sibling-repo mailbox entry and then rsync the K2B tree -- silently syncing the wrong files. K2Bi's Phase 1 Session 3 plan covers this fork-time swap.

**Parse failures must be loud, not silent.** A malformed entry is *worse* than no entry -- it means the durable recovery signal is broken, and silently skipping it would re-create the lost-recovery problem the mailbox was added to prevent. If an entry file cannot be parsed, stop and report to Keith so they can inspect, fix, or delete it manually.

**Readers always exit 0** and encode state in stdout. This avoids a footgun where a caller wraps the script in `set -e` and the bash abort hides the very error condition we're trying to surface.

**Scan step.** List the mailbox and classify each entry:

```bash
# Derive mailbox dir from current repo root, not hardcoded -- so a forked sync
# skill in a sibling repo (e.g. K2Bi) reads from its own .pending-sync/, not K2B's.
MAILBOX="$(git rev-parse --show-toplevel)/.pending-sync"
mailbox_state=$(python3 <<PYEOF
import json, os, sys, time

MAILBOX = "$MAILBOX"
STALE_TMP_THRESHOLD = 60  # seconds; real writes take milliseconds
if not os.path.isdir(MAILBOX):
    print("EMPTY")
    sys.exit(0)

entries = []
unreadable = []
now = time.time()
for name in sorted(os.listdir(MAILBOX)):
    if name.startswith(".tmp_"):
        # In-progress atomic write -- skip while fresh. But if a producer
        # crashed between fsync and os.replace(), the only durable artifact
        # is the .tmp_ file; silently skipping it forever would lose the
        # defer signal. Any .tmp_ older than STALE_TMP_THRESHOLD is surfaced
        # as UNREADABLE so Keith can recover it (rename if the JSON is
        # complete, delete if not).
        try:
            age = now - os.stat(os.path.join(MAILBOX, name)).st_mtime
        except OSError:
            continue
        if age > STALE_TMP_THRESHOLD:
            unreadable.append((name, f"stale-temp:{int(age)}s old, likely crashed producer"))
        continue
    if not name.endswith(".json"):
        continue
    path = os.path.join(MAILBOX, name)
    try:
        with open(path) as f:
            d = json.load(f)
    except json.JSONDecodeError as e:
        unreadable.append((name, f"json:{e.msg}"))
        continue
    except OSError as e:
        unreadable.append((name, f"io:{e}"))
        continue

    if not isinstance(d, dict):
        unreadable.append((name, "schema:top-level is not an object"))
        continue
    if not d.get("pending", False):
        # pending:false entries are treated as already-processed stragglers.
        # Not strictly an error; skip silently. /sync will not act on them.
        continue
    required = ("set_at", "set_by_commit", "categories", "files", "entry_id")
    missing = [k for k in required if k not in d]
    if missing:
        unreadable.append((name, f"schema:missing {','.join(missing)}"))
        continue

    # Category allowlist. Legacy entries written before the /ship producer
    # fix could contain "hooks" or other labels that /sync has no deploy
    # target for. Blindly trusting the mailbox would silently consume such
    # entries without deploying the files -- exactly the lost-signal hole
    # we are trying to close. Treat unknown categories as UNREADABLE and
    # require manual recovery (rewrite the entry with a valid category or
    # delete it).
    VALID_CATEGORIES = {"skills", "code", "dashboard", "scripts"}
    cats = d.get("categories", [])
    if not isinstance(cats, list) or not cats:
        unreadable.append((name, "schema:categories must be non-empty list"))
        continue
    bad_cats = [c for c in cats if c not in VALID_CATEGORIES]
    if bad_cats:
        unreadable.append((name, f"category:unknown {','.join(bad_cats)} (expected subset of {sorted(VALID_CATEGORIES)})"))
        continue

    entries.append((name, d))

if not entries and not unreadable:
    print("EMPTY")
elif unreadable:
    print("UNREADABLE|" + json.dumps(unreadable))
    # Also print valid entries so caller can still decide to proceed
    if entries:
        print("VALID|" + json.dumps([(n, e) for n, e in entries]))
else:
    print("VALID|" + json.dumps([(n, e) for n, e in entries]))
PYEOF
)
```

**Decision tree:**

1. **`mailbox_state == "EMPTY"`**: no deferred entries. Proceed with normal conversation-context detection in step 1.
2. **`mailbox_state` starts with `VALID|`**: parse the JSON list that follows the pipe. Each element is `[filename, payload]`. Fold all payloads into a single sync scope: union of `files`, union of `categories`. Report to Keith: "Consuming N mailbox entries: list the `entry_id`s." Save the list of filenames -- those are the exact files you will delete after the sync succeeds.
3. **`mailbox_state` starts with `UNREADABLE|`**: STOP. Do not proceed to detection or sync. Report loudly to Keith with the list of bad entries: "Mailbox entries at `<repo-root>/.pending-sync/` are unreadable: {list}. The durable deferred-sync signal is broken. Inspect, fix, or delete the bad files and re-run /sync." (For K2B sessions, `<repo-root>` is `~/Projects/K2B`.) Never auto-delete corrupted entries -- they may be useful evidence. If there's a mixed `VALID|` line as well, show it too but still require Keith to acknowledge the broken state before proceeding.

**After a successful sync:**

Delete only the specific filenames that were in the `VALID|` list at the start of this run. Do NOT scan the directory again -- any new entries that appeared during the sync were written by a concurrent `/ship --defer` under a DIFFERENT filename (producers use a unique `entry_id`), and must be preserved for the next `/sync` run to pick up.

```bash
python3 <<PYEOF
import os, subprocess
# Same per-repo derivation as the scan step -- a forked sync skill in a sibling
# repo deletes from its own mailbox, not K2B's.
repo_root = subprocess.check_output(
  ["git", "rev-parse", "--show-toplevel"], text=True
).strip()
MAILBOX = os.path.join(repo_root, ".pending-sync")
PROCESSED = $PROCESSED_FILENAMES_JSON  # list of filenames captured from VALID| line at start
for name in PROCESSED:
    path = os.path.join(MAILBOX, name)
    try:
        os.remove(path)
    except FileNotFoundError:
        # Already gone. Benign.
        pass
    except OSError as e:
        print(f"WARNING: could not remove {name}: {e}")
# If the mailbox directory is now empty, leave it -- an empty dir is harmless
# and avoids a TOCTOU where rmdir races with a fresh producer write.
PYEOF
```

If the sync **fails**, do NOT delete any entries. They remain in place so the next `/sync` attempt can retry from the same mailbox state.

**Why the mailbox design is race-free:** producers (`/ship --defer`) write each entry as a unique filename via `os.replace()` (atomic rename). They never read or delete. The consumer (`/sync`) deletes only filenames it read at the start of its run -- so any concurrent producer's new filename is simply not in the delete list and survives untouched. No compare-and-swap, no locks, no TOCTOU. Correct on POSIX under concurrent `/ship` and `/sync` invocations.

This mechanism is the durable recovery path: a fresh Claude Code session can discover that the Mini is stale and act on it without needing access to a previous session's conversation.

### 1. Detect Changes

**Primary method: Use conversation context.** K2B knows which files it modified in the current session. List exactly those files -- don't scan the whole repo.

**If context is unclear** (e.g., Keith runs `/sync` in a fresh session), use rsync dry-run to compare MacBook vs Mac Mini. Only probe paths that actually exist in K2Bi today; Phase 4 will add `invest-remote/` and a dashboard if Keith ever ports one — at that point extend this block.

```bash
# Skills + CLAUDE.md + DEVLOG.md (top-level docs K2Bi actually ships today)
rsync -avn --delete \
  ~/Projects/K2Bi/.claude/skills/ macmini:~/Projects/K2Bi/.claude/skills/ 2>&1 | grep -E "^(sending|deleting|\.claude)"

for doc in CLAUDE.md DEVLOG.md README.md; do
  [ -f ~/Projects/K2Bi/"$doc" ] && \
    rsync -avn ~/Projects/K2Bi/"$doc" macmini:~/Projects/K2Bi/ 2>&1 | grep -v "^$"
done

# Scripts
rsync -avn ~/Projects/K2Bi/scripts/ macmini:~/Projects/K2Bi/scripts/ 2>&1 | grep -E "^(sending|deleting|scripts)"

# Phase 4+ only (invest-remote / dashboard -- uncomment when they land):
# rsync -avn --exclude node_modules --exclude dist \
#   ~/Projects/K2Bi/invest-remote/ macmini:~/Projects/K2Bi/invest-remote/ 2>&1 | grep -E "^(sending|deleting|invest-remote)"
```

This compares actual file contents between machines -- not git state. Only files that genuinely differ will show up.

**Do NOT use `git diff --name-only HEAD`** -- that shows all uncommitted changes since last commit, including files already synced in previous sessions. It produces false positives.

### 2. Categorize and Summarize

Group changed files into categories:

| Category | Matched Paths | Needs Build? |
|----------|--------------|-------------|
| skills | `.claude/skills/`, `CLAUDE.md`, `K2B_ARCHITECTURE.md` | No |
| code | `k2b-remote/` | Yes -- npm run build + pm2 restart k2b-remote |
| dashboard | `k2b-dashboard/` | Yes -- npm run build + pm2 restart k2b-dashboard |
| scripts | `scripts/` | No |

Show Keith a summary of only the files that actually differ:
```
Out of sync with Mac Mini:
  - .claude/skills/k2b-media-generator/SKILL.md
  - .claude/skills/k2b-youtube-capture/SKILL.md
  Category: skills

Sync to Mac Mini?
```

### 3. Execute Sync

For `/sync status` or when Keith wants to preview:
```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh --dry-run
```

For actual sync, run the deploy script with the appropriate mode:
```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh auto
```

Or with explicit category:
```bash
~/Projects/K2Bi/scripts/deploy-to-mini.sh skills
~/Projects/K2Bi/scripts/deploy-to-mini.sh code
~/Projects/K2Bi/scripts/deploy-to-mini.sh dashboard
~/Projects/K2Bi/scripts/deploy-to-mini.sh scripts
~/Projects/K2Bi/scripts/deploy-to-mini.sh all
```

### 4. Verify

After sync completes, verify:

**For skills:**
```bash
ssh macmini "head -3 ~/Projects/K2Bi/CLAUDE.md"
ssh macmini "ls ~/Projects/K2Bi/.claude/skills/ | wc -l"
```

**For code (k2b-remote):**
```bash
ssh macmini "pm2 status k2b-remote"
```

**For dashboard:**
```bash
ssh macmini "pm2 status k2b-dashboard"
```

**For any sync target:** Report what was synced and any warnings.

### 5. Report

Tell Keith:
- What categories were synced
- How many files transferred
- Verification results (skill count match, pm2 status)
- Any errors or warnings

## Error Handling

- **Mac Mini unreachable**: "Can't reach Mac Mini via SSH. Is it on the network?"
- **Build failure**: Show the npm error output. Don't restart pm2 if build failed.
- **pm2 restart k2b-remote failure**: Show pm2 logs. Suggest `ssh macmini "pm2 logs k2b-remote --lines 30 --nostream"`
- **pm2 restart k2b-dashboard failure**: Show pm2 logs. Suggest `ssh macmini "pm2 logs k2b-dashboard --lines 30 --nostream"`
- **No changes detected**: "No syncable changes found. Use `/sync all` to force a full sync."

## What Does NOT Sync

- **Vault** (`K2Bi-Vault/`) -- handled by Syncthing, not this skill
- **node_modules/** and **dist/** -- excluded from rsync, rebuilt on Mini
- **store/** -- production SQLite database lives on Mac Mini, NEVER overwrite from MacBook
- **.env** -- environment config stays local to each machine
- **.git/** -- each machine has its own git state

## Usage Logging

After completing the main task, log this skill invocation:
```bash
echo -e "$(date +%Y-%m-%d)\tinvest-sync\t$(echo $RANDOM | md5sum | head -c 8)\tsynced CATEGORY to mac mini" >> ~/Projects/K2Bi-Vault/wiki/context/skill-usage-log.tsv
```

## Notes

- Always confirm with Keith before syncing. Never auto-sync.
- The deploy script handles SSH connectivity checks.
- Code changes (k2b-remote) require build + restart. Skills don't.
- If Keith is iterating fast on k2b-remote, suggest batching changes before syncing.
