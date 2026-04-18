# Eval: auto-promote-rejected

## Setup

A learning `L-2026-04-20-003` exists in `self_improve_learnings.md` with:

- `distilled-rule: "Never auto-merge Dependabot PRs without reading the diff"`
- `- **Reinforced:** 5`
- `- **auto-promote-rejected:** true` (added by a prior `/ship` step 0 when Keith answered `n`)

## Task

Run `/ship` step 0.

## Expected output

- `scripts/promote-learnings.py` stdout does NOT include any object with `learn_id: "L-2026-04-20-003"`.
- /ship does NOT surface this learning to Keith.
- /ship does NOT prompt `Promote now?` for this L-ID.
- /ship does NOT append anything to `active_rules.md` for this L-ID.
- /ship continues silently to step 1 (or to the next candidate if others exist).

## Pass criteria

- No prompt text referencing `L-2026-04-20-003` appears anywhere in the step 0 output.
- `active_rules.md` is unchanged with respect to this learning.
- Once reinforced rejection is in place, this learning is never surfaced again by `/ship` step 0 unless Keith manually removes the `- **auto-promote-rejected:**` bullet.
