# Learnings: invest-screen

## What Works

- Line-level frontmatter mutation preserves Stage-1 fields byte-for-byte while injecting Stage-2 keys and flipping status.
- LLM retry with strict math invariant validation catches hallucinated sub-factors and component-sum errors reliably.
- Single-call LLM scoring (prompt + JSON schema) is sufficient for Phase 3.7; no need for multi-call decomposition at this stage.
- `--manual-promote` collapsing the `promoted` state is operationally simpler for operator-driven entries than forcing a two-step flow.

## What Doesn't Work

- (To be filled as the skill runs in production)

## Patterns Discovered

- Idempotency by default (`--re-enrich` opt-in) prevents accidental overwrite of human-reviewed scores.
- Deterministic band derivation from quick_score means the LLM's `rating_band` field is validated, not trusted.
- Separating `_validate_stage1_presence` from `_validate_stage1_status` allows `--re-enrich` to bypass status checks while still guarding against corruption.
