# Orchestrator Adapters Round 7 Disposition

Date: 2026-06-06

Architect ruling: K2B PM reframed this adapter as upstream of the engine gate. The engine only loads `status: approved` strategies with traceable `approved_commit_sha`, so a half-shipped or malformed strategy cannot reach the engine. Round 7 findings are correctness and recoverability issues, not capital-leak paths.

Terminal instruction: apply one final bounded patch, run one more Kimi review, then raise the PR no matter what the verdict says. Remaining `NEEDS-ATTENTION` items after this disposition are reviewer-scope-bounded and return to Keith's K2Bi PM review of the PR.

## Final Review

- job_id: `2026-06-06T07-35-03Z_2459b9`
- reviewer: `primary_used=minimax`, `fallback_used=false`
- verdict: `NEEDS-ATTENTION`
- outcome: no further code changes per Round 7 hard stop. PR is the terminal handoff to Keith's K2Bi PM review.

## Finding Disposition

### 1. Rollback recoverability

- disposition: ACCEPTED, bounded.
- action: added a persistent same-checkout rollback marker under `.k2bi-orchestrator/rollback/`.
- action: `run_full_ship` refuses before review if an incomplete rollback marker exists.
- action: rollback failures now attach a structured `RollbackResult` to `OrchestratorGateError`, including marker path, index restoration status, working-tree restoration status, and marker cleanup status.
- boundary: no recovery daemon, no distributed durability, no TTL or heartbeat.

### 2. Lock directory inside `.git`

- disposition: ACCEPTED, narrow.
- action: moved same-checkout lock files to `.k2bi-orchestrator/locks/`.
- action: added containment validation for derived lock paths.
- action: added `.k2bi-orchestrator/` to `.gitignore`.
- boundary: still a local `flock` only. No distributed lock.

### 3. Approval token timezone

- disposition: ACCEPTED.
- action: `approved_at` must parse as ISO-8601 with timezone and UTC offset.
- boundary: replay prevention remains deferred to the orchestrator protocol.

### 4. Strategy write verification

- disposition: ACCEPTED.
- action: `write_complete_strategy_spec` now verifies the post-write digest through `_sha256_file_descriptor`.
- boundary: no `O_NOFOLLOW` shared atomic-writer redesign in this adapter.

### 5. Broad stderr check

- disposition: REJECTED.
- reason: the current behavior fails safe. A false failure blocks a ship and cannot leak capital.
- action: no code change.

### 6. `git commit --only` edge cases

- disposition: PARTIAL.
- check: no existing adapter-local Ship-1a clean-tree preflight was present before dispatch.
- action: added one narrow `git status --porcelain=v1 --untracked-files=no --ignore-submodules=none` assertion before mutation. It permits ordinary target strategy dirt and refuses unrelated, unmerged, or submodule state.
- boundary: no broader Git workflow redesign.

### 7. Mutable events

- disposition: REJECTED.
- reason: cleanliness only. It is not a safety risk under the PM reframing.
- action: no code change.

### 8. Subprocess default encoding

- disposition: ACCEPTED, trivial only.
- action: subprocess calls now pass `encoding="utf-8", errors="replace"`.

## Deferred By Design

Approval-token replay protection belongs in the orchestrator protocol handoff #2, where consumed-token or nonce state can be owned centrally. This adapter continues to bind the token to slug, content hash, UTC `approved_at`, and `ship_lease_id`, but it does not implement a server-side nonce store.
