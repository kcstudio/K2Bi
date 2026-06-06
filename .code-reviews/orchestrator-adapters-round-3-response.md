# Orchestrator Adapters Review Round 3 Response

Review artifact: `.code-reviews/2026-06-04T14-38-40Z_2d7905.log`

Reviewer metadata: `primary_used=minimax`, `fallback_used=false`

Verdict: `NEEDS-ATTENTION`

## Finding Disposition

### 1. HIGH rollback verification retains TOCTOU race on shared write primitive

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: `run_full_ship` could interleave with another process while reading original bytes, mutating status, and rolling back.
- action: added an exclusive nonblocking per-strategy `flock` around the full read, review, mutation, staging, commit, and rollback sequence.
- verification: `test_existing_strategy_lock_refuses_concurrent_ship`

### 2. HIGH concurrent `run_full_ship` calls can corrupt rollback state

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: two callers on the same strategy could both capture stale original bytes and one rollback could undo the other's change.
- action: same per-strategy lock now fails closed before review or mutation when another ship is active for that strategy.
- verification: `test_existing_strategy_lock_refuses_concurrent_ship`

### 3. HIGH nested exception in rollback can suppress original failure cause

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: rollback failure handling could surface only the restore failure path and lose context about the original ship failure.
- action: rollback now records staged-restore and working-tree-restore outcomes separately, then raises one `OrchestratorGateError` containing the original ship error and every rollback error observed.
- verification: `test_restore_staged_failure_is_not_swallowed`

### 4. MEDIUM approval token binds to pre-mutation hash, enabling replay after external revert

- reproduce: yes
- material-to-live-risk: yes
- disposition: partial
- evidence: the token previously bound only slug and pre-approval file hash. A fully nonce-backed token needs orchestrator-side nonce issuance that this repo does not own.
- action: token now binds slug, pre-approval file hash, and exact `approved_at` timestamp, closing stale token reuse across changed file content and timestamp mismatch while keeping the adapter dependency-free.
- verification: `test_replayed_approval_token_refuses_after_file_content_changes`

### 5. MEDIUM no observability hooks for production failure reconstruction

- reproduce: yes
- material-to-live-risk: yes
- disposition: partial
- evidence: successful callable ship results did not expose structured gate state to the external orchestrator.
- action: `FullShipResult` now carries structured in-memory events for ship start, review completions, helper completion, git staging, commit success, and rollback attempts. Persistent logging is left to the external orchestrator because this adapter has no configured log sink.
- verification: `test_success_runs_plan_review_diff_review_helper_and_commit`

### 6. MEDIUM `_run_git_checked` allows None result to pass silently

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: an injected git runner returning `None` could be treated as success.
- action: `_run_git_checked` now requires a `CommandResult` and fails closed otherwise.
- verification: `test_run_git_checked_requires_command_result`

### 7. MEDIUM `run_review_with_script` trusts `review.sh` stdout without stderr validation

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: a zero-exit review wrapper could emit a JSON envelope on stdout while writing unexpected warnings to stderr.
- action: `run_review_with_script` now rejects non-empty stderr even when exit code is 0, before trusting the envelope.
- verification: `test_review_script_nonempty_stderr_refuses`

## Post-Fix Verification

- `python3 -m pytest tests/test_invest_orchestrator_adapters.py -q` -> 22 passed
- `python3 -m pytest tests/test_invest_orchestrator_adapters.py tests/test_invest_thesis.py tests/test_invest_coach.py tests/test_invest_ship_strategy.py -q` -> 264 passed
- `python3 -m pytest tests/ -q` -> 1790 passed, 1 skipped, 53 subtests passed, 2 dependency warnings
- `python3 scripts/lib/deploy_config.py preflight` -> passed
