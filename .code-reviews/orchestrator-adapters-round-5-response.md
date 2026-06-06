# Orchestrator Adapters Review Round 5 Response

Review artifact: `.code-reviews/2026-06-04T15-21-59Z_cf1568.log`

Reviewer metadata: `primary_used=minimax`, `fallback_used=false`

Verdict: `NEEDS-ATTENTION`

## Finding Disposition

### 1. CRITICAL rollback does not verify index restoration before working-tree restore

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: restoring working-tree bytes while the index still contains approved content creates a split-brain git state.
- action: rollback now attempts `git restore --staged`, falls back to `git reset HEAD -- <path>`, and refuses to restore working-tree bytes unless `git diff --cached --quiet -- <path>` proves the index is clean.
- verification: `test_dirty_index_blocks_working_tree_restore_after_commit_failure`

### 2. HIGH advisory flock is lost on unclean shutdown

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: process death releases the advisory lock but can leave approved uncommitted state behind.
- action: lock acquisition now writes a pending ship marker after taking the lock, clears it only on normal context exit, and refuses future ships if a pending marker remains. The adapter still requires the external `ship_lease_id` for cross-checkout exclusion.
- verification: `test_stale_pending_strategy_lock_marker_refuses`

### 3. HIGH approval token binds to second-granularity timestamp

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: second-only timestamps leave a same-second replay window.
- action: `approved_at` must parse as ISO-8601 and include nonzero microseconds. The token still binds slug, file hash, timestamp, and ship lease.
- verification: `test_approval_timestamp_requires_microseconds`

### 4. HIGH `git commit --only` failure modes and `--no-verify`

- reproduce: partially
- material-to-live-risk: yes
- disposition: partial, reject `--no-verify`
- evidence: commit failure and index rollback risk are real and are now covered by stricter rollback checks. Adding `--no-verify` conflicts with K2Bi's safety architecture because pre-commit and commit-msg hooks enforce strategy transition and content immutability rules.
- action: kept hooks enabled, kept `git commit --only <path>`, added index-clean verification before working-tree restore, and left broader real-git conflict/submodule coverage as future hardening outside this adapter pass.
- verification: `test_dirty_index_blocks_working_tree_restore_after_commit_failure`, existing commit-order tests

### 5. HIGH `_extract_review_verdict` returns generic `UNKNOWN`

- reproduce: yes
- material-to-live-risk: medium
- disposition: accepted
- evidence: infra failures and parse failures were collapsed into one ambiguous verdict value.
- action: missing logs now return `LOG_MISSING`; logs without strict verdict headers return `UNKNOWN_VERDICT`. Both still fail closed through `_require_review_approved`.
- verification: `test_review_verdict_requires_strict_header`

### 6. MEDIUM `run_review_with_script` trusts `review.sh` existence and executability

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: a symlinked or non-executable review wrapper should not be spawned.
- action: `run_review_with_script` now verifies `scripts/review.sh` is a single-link regular executable file before `subprocess.run`.
- verification: `test_review_script_must_be_regular_executable_file`

### 7. MEDIUM `write_complete_strategy_spec` lacks post-write verification

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: callers had no digest proving the atomically written bytes matched the intended content.
- action: the writer now re-reads the file after atomic write, verifies byte equality, and returns `content_sha256`.
- verification: `test_writes_complete_strategy_spec_that_passes_ship_shape`

### 8. MEDIUM `verify_and_generate_thesis` does not validate `generate_func` return type

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: an injected helper returning `None` could produce a malformed success wrapper.
- action: the adapter now requires `generate_func` to return `scripts.lib.invest_thesis.ThesisResult`.
- verification: `test_generate_func_must_return_thesis_result`

## Post-Fix Verification

- `python3 -m pytest tests/test_invest_orchestrator_adapters.py -q` -> 31 passed
- `python3 -m pytest tests/test_invest_orchestrator_adapters.py tests/test_invest_thesis.py tests/test_invest_coach.py tests/test_invest_ship_strategy.py -q` -> 273 passed
- `python3 -m pytest tests/ -q` -> 1799 passed, 1 skipped, 53 subtests passed, 2 dependency warnings
- `python3 scripts/lib/deploy_config.py preflight` -> passed
