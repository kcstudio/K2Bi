---
name: invest-retro
description: Generate a Stage 15 trade retro from a closed trade in raw/journal/*.jsonl and log the lesson to /learn. Use when Keith says /retro, "run the trade retro", "learn from the closed trade", or asks to close the orchestrator Stage-15 loop.
tier: portfolio-manager
routines-ready: true
phase: 4
status: mvp
---

# invest-retro -- Stage 15 MVP

Generate a structured retro from K2Bi's append-only journal after a strategy is
closed, then append one low-confidence lesson to `/learn`.

## Safety Boundary

- Read only `raw/journal/*.jsonl` for trade evidence.
- Write only the retro note under `wiki/insights/` and the learning entry under
  `System/memory/self_improve_learnings.md`.
- Refuse any retro path that would write outside `wiki/insights/`.
- Do not call `ib_async`.
- Do not call `scripts/gateway-query.sh`.
- Do not mutate live broker state.
- Do not edit strategies, validators, kill-switch files, or engine state.
- Do not wait for CDNS to fill or exit. Use a closed-trade fixture or an
  already-closed journal event.

## Invocation

```bash
python3 -m scripts.lib.invest_trade_retro run \
  --strategy <strategy-id> \
  --vault-root "$HOME/Projects/K2Bi-Vault" \
  --repo-root "$HOME/Projects/K2Bi" \
  --as-of "$(date +%Y-%m-%d)"
```

## Output

The helper prints structured JSON, writes:

```text
K2Bi-Vault/wiki/insights/YYYY-MM-DD_trade-retro_<strategy-id>.md
```

and appends one `/learn` entry to:

```text
K2Bi-Vault/System/memory/self_improve_learnings.md
```

## MVP Contract

The retro must include:

- `strategy_id`
- `ticker`
- closure source event id
- closure source file, line, and scan index
- entry fill if present
- entry source file, line, and scan index when present
- stopped-out exit fields
- outcome fields when entry fill exists
- at least one concrete change
- `/learn` id

The helper is idempotent by closure journal entry id. Re-running the same closed
trade returns the existing retro and does not append a duplicate learning. If a
previous run left a retro without the `/learn` entry, the helper repairs the
missing learning while preserving the retro's original date and path.

Retro reuse, partial-state repair, the learning append, and the retro write all
run under `System/memory/.stage15_trade_retro.lock`, which also covers the
journal scan and retro computation. This is a local POSIX `flock`, not a
distributed/NFS lock. Lock acquisition uses bounded exponential backoff with
jitter. The helper writes the retro first, then `/learn`; if the learning write
fails, the next run repairs the missing learning from the existing retro using
the retro-embedded learning id. The lock file is owner-only and symlink lock
paths are refused with atomic `O_NOFOLLOW` creation where available.
Learning-write gaps leave a `.stage15_pending_<closure-id>.json` marker until
repair succeeds. The helper validates the MVP retro schema before writing
Markdown and refuses symlinked vault roots or `wiki/insights` paths.
Journal scans fail hard above 100 MB or 1,000,000 lines per JSONL file.

If no closed trade is found, if the closure event has no ticker, or if entry
quantity fields conflict or are non-numeric, refuse and do not write a
learning. If the closure timestamp is missing or unparseable, refuse. Malformed
unrelated JSONL lines are skipped with a warning. When multiple close events
exist, choose the newest parsed close timestamp and use scan order only as a
tie-breaker. Entry fills are matched by parsed fill timestamp, not journal file
name order, and must be strictly earlier than the close timestamp.

## Verification

```bash
pytest tests/test_invest_trade_retro.py
```

Before ship, also run the journal and strategy lifecycle regression files named
in the implementation spec.
