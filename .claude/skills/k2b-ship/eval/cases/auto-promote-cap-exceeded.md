# Eval: auto-promote-cap-exceeded

## Setup

`active_rules.md` has 12 numbered rules. Rule 5 has `last-reinforced: 2026-02-01` (>30 days before today) and the lowest reinforcement count in the parenthetical. All other rules have `last-reinforced:` within the last 30 days.

A learning `L-2026-04-20-002` exists in `self_improve_learnings.md` with:

- `distilled-rule: "Always /ship before closing a session, not /sync alone"`
- `- **Reinforced:** 3`
- no `- **auto-promote-rejected:**` bullet
- not cited in `active_rules.md`

## Task

Run `/ship` step 0. Keith answers `y` to the promote prompt.

## Expected output

- `scripts/promote-learnings.py` JSON includes `L-2026-04-20-002` with `would_exceed_cap: true`, `current_active_count: 12`, `cap: 12`.
- /ship prompts to promote. On `y`, /ship calls `scripts/select-lru-victim.py`.
- `select-lru-victim.py` returns JSON describing rule 5 as the victim (oldest `last-reinforced:` and lowest count).
- Before committing the new rule, /ship prints:
  ```
  [warn] demoting rule 5 (<title>) to make room for L-2026-04-20-002
  Confirm? [y/n]
  ```
- On Keith's `y`, /ship calls `scripts/demote-rule.sh 5`.
- `demote-rule.sh` removes rule 5's full block (heading + any continuation lines) from `active_rules.md`, appends it to the `## Demoted Rules` section of `self_improve_learnings.md` with `demoted-date:` set to today, renumbers remaining rules so they run 1..11 contiguously, and logs via `scripts/wiki-log-append.sh`.
- /ship then appends the new rule as rule 12 in the section that matches the learning's `Area:`, including `(L-2026-04-20-002, last-reinforced: <today>)` in the parenthetical.
- /ship then calls `scripts/wiki-log-append.sh /ship "step-0" "promoted=1 rejected=0 skipped=0 demoted=1"`.

## Pass criteria

- `active_rules.md` ends step 0 with exactly 12 numbered rules, all contiguous.
- Old rule 5 text is present in `self_improve_learnings.md` under `## Demoted Rules` with `demoted-date:` today.
- `wiki/log.md` has one `/ship  step-0  promoted=1 rejected=0 skipped=0 demoted=1` line and one `/ship  active_rules.md  demoted rule 5 to Demoted Rules section` line.
