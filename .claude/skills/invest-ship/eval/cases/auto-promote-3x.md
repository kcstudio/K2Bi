# Eval: auto-promote-3x

## Setup

A learning `L-2026-04-20-001` exists in `self_improve_learnings.md` with:

- `distilled-rule: "Never run /sync on uncommitted changes"` (frontmatter-style line in the entry body)
- `- **Reinforced:** 3`
- no `- **auto-promote-rejected:**` bullet

`active_rules.md` currently has 9 numbered rules; the `Cap: 12 rules` paragraph is in place. `L-2026-04-20-001` is not cited anywhere in `active_rules.md`.

## Task

Run `/ship` step 0 (the active-rules auto-promotion scan).

## Expected output

- `scripts/promote-learnings.py` prints a JSON array that includes an object with `learn_id: "L-2026-04-20-001"`, `count: 3`, `distilled_rule: "Never run /sync on uncommitted changes"`, `would_exceed_cap: false`, `current_active_count: 9`, `cap: 12`.
- /ship prints to Keith:
  ```
  L-2026-04-20-001 has been reinforced 3x and is not in active_rules.
  Distilled: "Never run /sync on uncommitted changes"
  Promote now? [y/n/skip]
  ```
- /ship does NOT silently append to `active_rules.md`.
- /ship does NOT call `scripts/demote-rule.sh` (current count is below cap).
- /ship waits for Keith's answer before continuing to step 1.

## Pass criteria

- Prompt text above appears verbatim (allowing minor whitespace).
- `active_rules.md` is unchanged until Keith answers `y`.
- `self_improve_learnings.md` is unchanged until Keith answers `n`.
