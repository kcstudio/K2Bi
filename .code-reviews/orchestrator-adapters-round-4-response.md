# Orchestrator Adapters Review Round 4 Response

Review artifact: `.code-reviews/2026-06-04T15-13-20Z_758a13.log`

Reviewer metadata: `primary_used=minimax`, `fallback_used=false`

Verdict: `NEEDS-ATTENTION`

## Finding Disposition

### 1. HIGH advisory flock does not protect concurrent ships across working copies or containers

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: the local per-strategy flock only coordinates callers that share the same checkout and lockfile path.
- action: `FullShipApproval` now requires `ship_lease_id` from the external orchestrator's mutual-exclusion layer, and the final approval token binds slug, pre-approval file hash, `approved_at`, and `ship_lease_id`. The `run_full_ship` docstring now states the local flock is same-checkout only and distributed callers must provide their own lease.
- verification: `test_missing_external_ship_lease_fails_before_review`

### 2. HIGH review focus string is passed to shell script without validation or escaping

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: `request.focus` reached `scripts/review.sh` without a length or control-character gate.
- action: added `_safe_review_focus`, which rejects non-strings, strings over 2000 characters, CR/LF/NUL/control characters, and path-traversal text before spawning the review wrapper.
- verification: `test_review_script_rejects_unsafe_focus_before_spawn`

### 3. MEDIUM rollback working-tree restore uses same atomic write primitive without verifying file system identity

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: a symlink or hardlinked strategy path could make rollback target identity ambiguous.
- action: `run_full_ship` now refuses symlink, special-file, or hardlinked strategy paths before reading or mutating them, and rollback rechecks the target before restoring original bytes.
- verification: `test_full_ship_refuses_symlink_strategy_before_review`, `test_full_ship_refuses_hardlinked_strategy_before_review`

## Post-Fix Verification

- `python3 -m pytest tests/test_invest_orchestrator_adapters.py -q` -> 26 passed
- `python3 -m pytest tests/test_invest_orchestrator_adapters.py tests/test_invest_thesis.py tests/test_invest_coach.py tests/test_invest_ship_strategy.py -q` -> 268 passed
- `python3 -m pytest tests/ -q` -> 1794 passed, 1 skipped, 53 subtests passed, 2 dependency warnings
- `python3 scripts/lib/deploy_config.py preflight` -> passed
