# Orchestrator Adapters Review Round 1 Response

Review artifact: `.code-reviews/2026-06-04T14-19-39Z_f5fa3b.log`

Reviewer metadata: `primary_used=minimax`, `fallback_used=false`

Verdict: `NEEDS-ATTENTION`

## Finding Disposition

### 1. HIGH rollback restoration uses same write primitive without integrity check

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: `run_full_ship` could leave `status: approved` on disk after a failed post-helper gate if rollback failed.
- action: added pre-mutation SHA-256 capture, verified rollback bytes, staged-reset attempt, and distinct rollback failure error.
- verification: `test_failed_diff_review_restores_original_bytes_and_does_not_commit`

### 2. HIGH review verdict extraction is substring-based

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: a log containing the word `APPROVE` outside a verdict header could previously return `APPROVE`.
- action: replaced broad substring scan with strict `# ... review -- APPROVE|NEEDS-ATTENTION` header parsing plus optional JSON verdict line parsing.
- verification: `test_review_verdict_requires_strict_header`

### 3. HIGH review script JSON envelope parsed without schema validation

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: missing `job_id` or bad `log_path` could produce confusing or unsafe review-state reads.
- action: added envelope schema checks for job id, repo-relative log path containment, log existence, state-file existence, `primary_used`, and `fallback_used`.
- verification: `test_review_envelope_missing_job_id_refuses`

### 4. MEDIUM approval token is replayable

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: static `APPROVE_STRATEGY:<slug>` did not bind Keith's approval to the reviewed file bytes.
- action: approval token now requires `APPROVE_STRATEGY:<slug>:<strategy_sha256>` and refuses when the file changes after capture.
- verification: `test_replayed_approval_token_refuses_after_file_content_changes`

### 5. MEDIUM git commit lacks signature verification or branch protection check

- reproduce: partially
- material-to-live-risk: no for GPG, yes for unintended staged content
- disposition: partial
- evidence: K2Bi repo rules require exact staging and no `--no-verify`; they do not currently require signed commits or branch-protection checks inside helper functions.
- action: accepted adjacent safety by un-staging the intended file on failed post-stage gates and keeping commit scoped to the one strategy path. GPG and branch-protection enforcement are deferred until K2Bi declares them as hard repo policy.
- verification: full pytest plus review-disposition record

### 6. MEDIUM run_git_command lacks timeout and command validation

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: a default git runner without timeout can hang the orchestrator, and a non-git command should not be accepted by the default path.
- action: added `GIT_TIMEOUT_S=30`, restricted default runner to `git` commands, and allowed only the subcommands the adapter uses or inspects.
- verification: `test_run_git_command_rejects_non_git_command`

### 7. MEDIUM repo root fallback is heuristic

- reproduce: yes
- material-to-live-risk: yes
- disposition: accepted
- evidence: full ship requires a real git checkout; falling back to `parents[2]` could target the wrong tree.
- action: `_repo_root_for` now fails closed with `OrchestratorGateError` when `git rev-parse --show-toplevel` fails or returns empty output.
- verification: `test_repo_root_probe_fails_closed_outside_git`

## Post-Fix Verification

- `python3 -m pytest tests/test_invest_orchestrator_adapters.py -q` -> 15 passed
- `python3 -m pytest tests/test_invest_orchestrator_adapters.py tests/test_invest_thesis.py tests/test_invest_coach.py tests/test_invest_ship_strategy.py -q` -> 257 passed
- `python3 -m pytest tests/ -q` -> 1783 passed, 1 skipped, 53 subtests passed, 2 dependency warnings
