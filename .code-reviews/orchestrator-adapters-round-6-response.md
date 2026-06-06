# Orchestrator Adapters Review Round 6 Response

Review artifact: `.code-reviews/2026-06-04T15-33-18Z_e1f034.log`

Reviewer metadata: `primary_used=minimax`, `fallback_used=false`

Verdict: `NEEDS-ATTENTION`

Architect ruling: K2B PM endorsed a bounded closeout. Fix only adapter-local issues that preserve the original expose-as-callable goal. Reject distributed locking, server-side token stores, and atomic-writer redesign as out of scope for single-operator, single-Mac, paper-trading deployment.

## Finding Disposition

### 1. CRITICAL rollback working-tree restore proceeds after failed index cleanup

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: refusing before working-tree restoration can leave the file in approved state after commit/index failures.
- action: rollback now attempts working-tree restoration even when index cleanup fails, while preserving and surfacing index cleanup errors in the final `OrchestratorGateError`.
- verification: `test_dirty_index_blocks_working_tree_restore_after_commit_failure`

### 2. CRITICAL stale lock marker not cleared on unclean shutdown

- reproduce: yes for marker design, but threat model rejected
- material-to-live-risk: no for this deployment
- disposition: rejected as out of scope
- evidence: K2B PM clarified this is a single-operator, single-Mac, paper-trading deployment. Same-checkout `flock` plus external `ship_lease_id` covers the real concurrency risk.
- action: removed the pending-marker mechanism rather than adding TTL, heartbeat, or PID-liveness recovery.
- deferral: distributed lease durability belongs in the external orchestrator if deployment topology changes.

### 3. HIGH approval token uses client-provided timestamp enabling replay

- reproduce: yes
- material-to-live-risk: not in this adapter's ownership boundary
- disposition: rejected as out of scope, deferred by design
- evidence: the adapter consumes an explicit human approval token captured by the orchestrator. Server-side nonces, consumed-token stores, and replay ledgers require orchestrator protocol state.
- action: kept adapter token binding to slug, pre-approval file hash, `approved_at`, and `ship_lease_id`; removed nonce/microsecond hardening from this adapter pass.
- deferral: approval-token replay protection belongs in the orchestrator protocol handoff #2 and should be implemented there.

### 4. HIGH path traversal in review envelope `log_path` via symlink attack

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: a review envelope log path should not point at arbitrary repo-relative or symlinked files.
- action: envelope validation now requires exactly `.code-reviews/<job_id>.log`, rejects traversal, verifies containment, and uses `lstat()` to require a regular non-symlink log file.
- verification: `test_review_envelope_rejects_symlink_log_path`

### 5. HIGH commit message trailer injection via `hints.trailers`

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: injected approve handlers could return trailer strings with newlines or malformed trailer content.
- action: `_build_strategy_commit_message` now validates every trailer line with `_safe_trailer_line` before appending it.
- verification: `test_commit_message_rejects_unsafe_trailer_line`

### 6. HIGH race condition between file stat and read in rollback verification

- reproduce: theoretical local attacker race
- material-to-live-risk: no for this deployment
- disposition: rejected as out of scope
- evidence: the current adapter already refuses symlink, hardlink, and special-file strategy paths before mutation and before rollback. The remaining concern requires a hostile local actor racing file replacement between checks.
- action: no O_NOFOLLOW atomic-writer redesign in this adapter pass.
- deferral: if K2Bi later runs in a multi-user or hostile-filesystem environment, redesign the shared atomic writer centrally rather than inside this adapter.

### 7. MEDIUM no validation that review script arguments match expected schema

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: file-list values are comma-joined before being passed to `scripts/review.sh`; commas, traversal, or control characters would corrupt scope.
- action: review file and plan arguments now pass through repo-contained, comma-free, control-character-free validation before spawning the review wrapper.
- verification: `test_review_script_rejects_unsafe_file_list`

### 8. MEDIUM missing timeout constant on git rev-parse in `_repo_root_for`

- reproduce: yes
- material-to-live-risk: low
- disposition: accepted
- evidence: the repo-root probe used an inline timeout value while other subprocess gates use named constants.
- action: added `REPO_ROOT_TIMEOUT_S`.
- verification: covered by `test_repo_root_probe_fails_closed_outside_git` plus py_compile.

## Post-Fix Verification

- `python3 -m pytest tests/test_invest_orchestrator_adapters.py -q` -> 32 passed
- `python3 -m pytest tests/test_invest_orchestrator_adapters.py tests/test_invest_thesis.py tests/test_invest_coach.py tests/test_invest_ship_strategy.py -q` -> 274 passed
- `python3 -m pytest tests/ -q` -> 1800 passed, 1 skipped, 53 subtests passed, 2 dependency warnings
- `python3 scripts/lib/deploy_config.py preflight` -> passed
- `python3 -m py_compile scripts/lib/invest_orchestrator_adapters.py` -> passed
- `git diff --check` -> passed
