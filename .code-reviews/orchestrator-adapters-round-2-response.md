# Orchestrator Adapters Review Round 2 Response

Review artifact: `.code-reviews/2026-06-04T14-29-01Z_8c0ec8.log`

Reviewer metadata: `primary_used=minimax`, `fallback_used=false`

Verdict: `NEEDS-ATTENTION`

## Finding Disposition

### 1. CRITICAL git restore failure silently swallowed during rollback

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: failed `git restore --staged` could leave approved content in the index while working-tree bytes were restored.
- action: rollback now preserves the staged-restore exception and raises a distinct `OrchestratorGateError` after restoring the working tree, instead of swallowing the index failure.
- verification: `test_restore_staged_failure_is_not_swallowed`

### 2. HIGH review script execution has no timeout

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: `run_review_with_script` used `subprocess.run` without timeout.
- action: added `REVIEW_TIMEOUT_S=420` and explicit `TimeoutExpired` handling.
- verification: `test_review_script_timeout_refuses`

### 3. HIGH rollback verification TOCTOU race

- reproduce: partially
- material-to-live-risk: yes
- disposition: partial
- evidence: verification after `atomic_write_bytes` cannot be made fully race-free without changing the shared write primitive, but the adapter can reduce the post-write verification window.
- action: changed verification to compute the digest through a freshly opened file descriptor immediately after the atomic replace and improved rollback failure messaging.
- verification: `test_failed_diff_review_restores_original_bytes_and_does_not_commit`

### 4. HIGH commit message log path injection

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: review result fields were embedded directly into commit message metadata.
- action: added commit-field sanitization that rejects empty values, newlines, control characters, and trailer-like values before building the message.
- verification: `test_commit_message_rejects_log_path_with_newline`

### 5. HIGH commit not scoped to exactly one file

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: plain `git commit -m` can include unrelated pre-staged content.
- action: commit now uses `git commit --only <strategy path> -m <message>` after staging the intended strategy path.
- verification: `test_success_runs_plan_review_diff_review_helper_and_commit`

### 6. MEDIUM `_repo_relative` falls back to absolute path

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: an escaped path could be passed into git operations as an absolute path.
- action: `_repo_relative` now fails closed with `OrchestratorGateError` when a path cannot be resolved under the repo root.
- verification: `test_repo_relative_refuses_path_outside_repo`

### 7. MEDIUM review envelope lacks request correlation

- reproduce: partially
- material-to-live-risk: yes
- disposition: partial
- evidence: the wrapper state file already records `scope`, `files`, `plan`, and `focus`, but the adapter did not check those fields in Round 2.
- action: deferred nonce-level correlation because `scripts/review.sh` does not currently expose an echo nonce. Existing hardening now requires a fresh valid job id, contained log path, state file, `primary_used`, and `fallback_used`. A future review-runner change can add nonce echoing centrally.
- verification: Round 1 envelope tests still pass.

### 8. MEDIUM approve handler mutates before diff review

- reproduce: no as stated
- material-to-live-risk: no
- disposition: rejected
- evidence: this ordering matches `/invest-ship --approve-strategy`: Step A/D mutates status to approved, then Checkpoint 2 reviews the resulting uncommitted diff against HEAD. `git diff HEAD` includes unstaged working-tree changes, so staging is not required for review visibility.
- action: no code change.
- verification: existing `run_full_ship` success path records review order as `["plan", "diff"]`, with diff review after helper mutation and before commit.

## Post-Fix Verification

- `python3 -m pytest tests/test_invest_orchestrator_adapters.py -q` -> 19 passed
- `python3 -m pytest tests/test_invest_orchestrator_adapters.py tests/test_invest_thesis.py tests/test_invest_coach.py tests/test_invest_ship_strategy.py -q` -> 261 passed
- `python3 -m pytest tests/ -q` -> 1787 passed, 1 skipped, 53 subtests passed, 2 dependency warnings
