# Apply Approved Limits Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task by task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `apply_approved_limits(...)`, a callable orchestrator adapter that applies an operator-approved limits proposal through the existing `handle_approve_limits(...)` helper, with bound approval-token validation, review gates, its own commit, and outer rollback.

**Architecture:** Add a typed `LimitsApproval` dataclass that mirrors `FullShipApproval`, plus `_validate_limits_approval(...)` that binds `APPROVE_LIMITS:<slug>:<proposal_sha256>:<config_sha256>:<approved_at>:<apply_lease_id>` to the exact proposal bytes and canonical config bytes before mutation. Add `apply_approved_limits(...)` beside `run_full_ship(...)`; the adapter performs review, delegates all limits parsing and config/proposal mutation to `handle_approve_limits(...)`, verifies both files are staged, commits with safe hints/trailers, and restores both files on any failed gate after mutation.

**Tech Stack:** Python 3.12, stdlib dataclasses, pathlib, subprocess-based git runner, existing `scripts.lib.invest_ship_strategy`, unittest via pytest.

---

## Locked Requirements

- Do not weaken `handle_approve_limits(...)`, `execution/validators/config.yaml`, pre-commit Check C, or validator behavior.
- The token must bind the exact approved proposal: slug, SHA-256 of proposal bytes, approved UTC timestamp, and external lease.
- The token must also bind the exact canonical config bytes by SHA-256 so a raced or pre-dirty `execution/validators/config.yaml` cannot ride along under a proposal-only token.
- Every token-bound field must be independently validated before token construction. In particular, slug uses `_validate_slug(...)`, lease uses `_validate_ship_lease_id(...)`, hashes must be 64 lowercase hex chars, and `approved_at` must be UTC ISO-8601 without newline or control characters.
- The adapter must expose a typed `LimitsApproval` dataclass and `_validate_limits_approval(...)`.
- The runtime clean-tree preflight must ignore untracked files using `git status --porcelain=v1 --untracked-files=no --ignore-submodules=none`.
- The dormant untracked `wiki/strategies/strategy_cdns.md` must not block the adapter.
- The preflight must refuse unrelated tracked changes beyond the proposal file and `execution/validators/config.yaml`.
- `handle_approve_limits(...)` remains the only writer for the config patch plus proposal status flip.
- `handle_approve_limits(...)` already accepts `now`. The adapter passes the validated `approval.approved_at` into that existing parameter so the written `approved_at` equals the token timestamp.
- `_validate_limits_approval(...)` must reject stale or future approval timestamps outside a bounded UTC clock-skew window. Default skew window: 300 seconds. Tests may inject `now_utc` for determinism.
- The adapter owns outer rollback for review or git failure after mutation: index cleanup plus working-tree restoration for both touched files.
- If rollback cannot prove both files are restored, leave a `.k2bi-orchestrator/rollback/*.json` marker and refuse future attempts until the marker is handled.
- Reviews must use the existing `ReviewRunner` interface and require `primary_used=minimax` and `fallback_used=false`.
- Commit must include existing limits trailers from `LimitsCommitHints` plus safe approval and review metadata.
- `config_path` must resolve to the canonical repo file `execution/validators/config.yaml`. Do not support arbitrary config paths in this adapter.
- No limits-apply code path may read `wiki/strategies/` or include untracked strategy files in review requests.
- After `handle_approve_limits(...)` returns, the adapter must verify the config bytes equal the expected single before-block to after-block replacement derived from the proposal before the handler ran.

## Files

- Modify: `scripts/lib/invest_orchestrator_adapters.py`
  - Add `LimitsApproval`.
  - Add `LimitsApplyResult`.
  - Add `apply_approved_limits(...)`.
  - Add limits-specific validation, lock, rollback-marker, commit-message, and helper functions.
  - Reuse existing `_assert_ship_clean_preflight(...)` behavior by extending it to accept multiple allowed paths.
- Modify: `tests/test_invest_orchestrator_adapters.py`
  - Add focused `ApplyApprovedLimitsAdapterTests`.
  - Reuse `_write_limits_proposal(...)` and `_write_config_yaml(...)` from `tests/test_invest_ship_strategy.py`.
  - Cover success, token binding, untracked-ignore behavior, unrelated tracked refusal, review refusal, rollback, commit failure, marker refusal, unsafe commit metadata, and typed timestamp behavior.

## API Shape

```python
@dataclass(frozen=True)
class LimitsApproval:
    final_approval_token: str
    approved_by: str
    approved_at: str
    apply_lease_id: str


@dataclass(frozen=True)
class LimitsApplyResult:
    slug: str
    commit_message: str
    commit_hints: iss.LimitsCommitHints
    plan_review: ReviewGateResult
    diff_review: ReviewGateResult
    events: list[dict[str, Any]]
```

```python
def apply_approved_limits(
    proposal_path: Path,
    *,
    approval: LimitsApproval,
    review_runner: ReviewRunner | None = None,
    approve_handler: Callable[..., iss.LimitsCommitHints] = iss.handle_approve_limits,
    git_runner: GitRunner | None = None,
    config_path: Path | None = None,
    now_utc: _dt.datetime | None = None,
    required_primary: str = "minimax",
) -> LimitsApplyResult:
    ...
```

Validation helper:

```python
def _validate_limits_approval(
    approval: LimitsApproval,
    slug: str,
    proposal_sha256: str,
    config_sha256: str,
    *,
    now_utc: _dt.datetime | None = None,
    max_clock_skew_s: int = 300,
) -> _dt.datetime:
    _validate_slug(slug)
    _validate_sha256_hex("proposal_sha256", proposal_sha256)
    _validate_sha256_hex("config_sha256", config_sha256)
    _require_text("approved_at", approval.approved_at)
    approved_at = _parse_utc_datetime(approval.approved_at)
    _require_recent_utc_timestamp(
        "approved_at",
        approved_at,
        now_utc=now_utc,
        max_clock_skew_s=max_clock_skew_s,
    )
    _validate_ship_lease_id(approval.apply_lease_id)
    expected = (
        f"APPROVE_LIMITS:{slug}:{proposal_sha256}:{config_sha256}:"
        f"{approval.approved_at}:{approval.apply_lease_id}"
    )
    if approval.final_approval_token != expected:
        raise OrchestratorGateError(...)
    _require_text("approved_by", approval.approved_by)
    return approved_at
```

`_validate_sha256_hex(...)` must reject non-strings, uppercase hex, wrong length, delimiters, whitespace, and control characters:

```python
_SHA256_HEX_RE = re.compile(r"^[a-f0-9]{64}$")
```

## Task 1: Approval Type And Token Gate

**Files:**
- Modify: `scripts/lib/invest_orchestrator_adapters.py`
- Test: `tests/test_invest_orchestrator_adapters.py`

- [ ] **Step 1: Write failing tests for typed approval validation**

Add tests under a new `ApplyApprovedLimitsAdapterTests` class:

```python
def _limits_approval(self, **overrides) -> ioa.LimitsApproval:
    approved_at = overrides.pop("approved_at", "2026-06-08T08:00:00.000000+00:00")
    lease_id = overrides.pop("apply_lease_id", "lease-limits-cdns-20260608")
    proposal_digest = ioa.strategy_file_sha256(self.proposal_path)
    config_digest = ioa.strategy_file_sha256(self.config_path)
    data = {
        "final_approval_token": (
            f"APPROVE_LIMITS:widen-size:{proposal_digest}:{config_digest}:"
            f"{approved_at}:{lease_id}"
        ),
        "approved_by": "Keith",
        "approved_at": approved_at,
        "apply_lease_id": lease_id,
    }
    data.update(overrides)
    return ioa.LimitsApproval(**data)

def test_limits_approval_token_binds_exact_proposal_bytes(self):
    approval = self._limits_approval()
    self.proposal_path.write_text(
        self.proposal_path.read_text(encoding="utf-8") + "\nchanged\n",
        encoding="utf-8",
    )
    with self.assertRaises(ioa.OrchestratorGateError) as cm:
        ioa.apply_approved_limits(
            self.proposal_path,
            approval=approval,
            review_runner=lambda request: self.fail("review must not run"),
            approve_handler=self._approve_handler,
        )
    self.assertIn("proposal hash", str(cm.exception).lower())
```

Also add tests for missing token, non-UTC timestamp, naive timestamp, timestamp outside the 300 second skew window, missing lease, wrong slug prefix, malformed hash strings, and config hash drift.

- [ ] **Step 2: Run the focused failing test**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests::test_limits_approval_token_binds_exact_proposal_bytes -q
```

Expected: fail because `LimitsApproval` and `apply_approved_limits` do not exist.

- [ ] **Step 3: Add dataclass and validation helper**

Add `LimitsApproval` near `FullShipApproval`. Add `_parse_utc_datetime(...)` to share UTC parsing between `_validate_full_ship_approval(...)` and `_validate_limits_approval(...)`, or keep a private limits parser with equivalent behavior if smaller.

Expected token:

```text
APPROVE_LIMITS:<slug>:<proposal_sha256>:<config_sha256>:<approved_at>:<apply_lease_id>
```

- [ ] **Step 4: Run validation tests**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests -q
```

Expected: remaining tests fail until adapter body exists, but validation-only failures should be resolved.

## Task 2: Outer Apply Orchestration

**Files:**
- Modify: `scripts/lib/invest_orchestrator_adapters.py`
- Test: `tests/test_invest_orchestrator_adapters.py`

- [ ] **Step 1: Write success test**

Test setup:

```python
def setUp(self) -> None:
    self.repo, self.parent_sha = _make_tmp_repo()
    self.config_path = _write_config_yaml(self.repo)
    self.proposal_path = _write_limits_proposal(self.repo)
```

Success assertions:

```python
def test_success_reviews_applies_stages_both_files_and_commits(self):
    review_kinds = []
    git_calls = []

    def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
        review_kinds.append((request.kind, list(request.files)))
        return ioa.ReviewGateResult(
            verdict="APPROVE",
            primary_used="minimax",
            fallback_used=False,
            log_path=f".code-reviews/{request.kind}.log",
        )

    def git_runner(cmd, cwd):
        git_calls.append(cmd)
        return ioa.CommandResult(returncode=0, stdout="", stderr="")

    result = ioa.apply_approved_limits(
        self.proposal_path,
        approval=self._limits_approval(),
        review_runner=review_runner,
        approve_handler=self._approve_handler,
        git_runner=git_runner,
        config_path=self.config_path,
    )

    self.assertEqual(result.slug, "widen-size")
    self.assertEqual([kind for kind, _ in review_kinds], ["plan", "diff"])
    self.assertIn("status: approved", self.proposal_path.read_text(encoding="utf-8"))
    self.assertIn("max_trade_risk_pct: 0.02", self.config_path.read_text(encoding="utf-8"))
    self.assertEqual(git_calls[0][:2], ["git", "status"])
    self.assertEqual(git_calls[1][:2], ["git", "add"])
    self.assertIn("review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md", git_calls[1])
    self.assertIn("execution/validators/config.yaml", git_calls[1])
    self.assertEqual(git_calls[2][:2], ["git", "commit"])
    self.assertIn("--only", git_calls[2])
    self.assertIn("Approved-Limits: widen-size", result.commit_message)
```

- [ ] **Step 2: Implement minimal adapter body**

Flow:

1. Resolve repo root from proposal path.
2. Resolve config path to repo `execution/validators/config.yaml` unless provided.
3. Compute relative proposal and config paths.
4. Derive slug with `iss._derive_limits_slug(proposal_path)` or a local wrapper that catches `iss.ValidationError` and raises `OrchestratorGateError`.
5. Lock on `limits_<slug>`.
6. Refuse existing rollback marker.
7. Assert `config_path` resolves exactly to `repo_root / "execution/validators/config.yaml"` via `_assert_config_path_canonical(...)`.
8. Assert both proposal and config are single-link regular files.
9. Read original bytes and hashes for both files.
10. Validate approval token against proposal hash, config hash, and approval timestamp recency.
11. Parse the proposal's YAML Patch before mutation and compute `expected_config_after_bytes` by applying exactly one before-block to after-block replacement against `original_config`.
12. Run clean-tree preflight with allowed paths `{proposal_rel, config_rel}`.
13. Build the plan `ReviewRequest` through `_make_limits_plan_review_request(...)`, then assert it contains exactly `kind="plan"` and `files=[proposal_rel]`.
14. Run plan review on proposal file. Review files must be exactly `[proposal_rel]`, not strategy files.
15. Require review approved.
16. Call `approve_handler(proposal_path, config_path=config_path, parent_sha=None, now=approved_at)`. This uses the existing handler parameter.
17. Verify `config_path.read_bytes() == expected_config_after_bytes`. If not, rollback and refuse before diff review.
18. Build the diff `ReviewRequest` through `_make_limits_diff_review_request(...)`, then assert it contains exactly `kind="diff"` and `files=[proposal_rel, config_rel]`.
19. Run diff review with files exactly `[proposal_rel, config_rel]`.
20. Require review approved.
21. Build commit message.
22. `git add <proposal_rel> <config_rel>`.
23. Verify cached diff contains both and only the two allowed files before commit.
24. `git commit --only <proposal_rel> <config_rel> -m <message>`.

Expected config helper:

```python
def _expected_config_after_apply(
    original_config: bytes,
    before_excerpt: bytes,
    after_excerpt: bytes,
) -> bytes:
    count = original_config.count(before_excerpt)
    if count != 1:
        raise OrchestratorGateError(
            f"approved config before-block must occur exactly once, got {count}"
        )
    return original_config.replace(before_excerpt, after_excerpt, 1)
```

Extract the YAML Patch before/after blocks as bytes from the proposal bytes. Do not decode and re-encode config bytes for verification.

- [ ] **Step 3: Run success test**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests::test_success_reviews_applies_stages_both_files_and_commits -q
```

Expected: pass.

## Task 3: Clean-Tree Preflight For Two Allowed Files

**Files:**
- Modify: `scripts/lib/invest_orchestrator_adapters.py`
- Test: `tests/test_invest_orchestrator_adapters.py`

- [ ] **Step 1: Write tests for tracked-only refusal**

Add:

```python
def test_untracked_strategy_file_does_not_block_limits_apply(self):
    untracked = self.repo / "wiki" / "strategies" / "strategy_cdns.md"
    untracked.parent.mkdir(parents=True, exist_ok=True)
    untracked.write_text("draft\n", encoding="utf-8")
    result = ioa.apply_approved_limits(
        self.proposal_path,
        approval=self._limits_approval(),
        review_runner=self._approve_review,
        approve_handler=self._approve_handler,
        git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
        config_path=self.config_path,
    )
    self.assertEqual(result.slug, "widen-size")
    self.assertEqual(self.review_files_seen, [["review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md"], ["review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md", "execution/validators/config.yaml"]])

def test_unrelated_tracked_file_refuses_before_review(self):
    note = self.repo / "notes.md"
    note.write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "add", "notes.md"], cwd=str(self.repo), check=True)
    subprocess.run(["git", "commit", "-m", "add note", "-q"], cwd=str(self.repo), check=True)
    note.write_text("dirty\n", encoding="utf-8")
    with self.assertRaises(ioa.OrchestratorGateError) as cm:
        ioa.apply_approved_limits(
            self.proposal_path,
            approval=self._limits_approval(),
            review_runner=lambda request: self.fail("review must not run"),
            approve_handler=self._approve_handler,
            config_path=self.config_path,
        )
    self.assertIn("clean-tree", str(cm.exception).lower())
    self.assertIn("notes.md", str(cm.exception))
```

Also add:

```python
def test_deleted_config_refuses_before_review(self):
    self.config_path.unlink()
    with self.assertRaises(ioa.OrchestratorGateError) as cm:
        ioa.apply_approved_limits(
            self.proposal_path,
            approval=self._limits_approval(),
            review_runner=lambda request: self.fail("review must not run"),
            approve_handler=self._approve_handler,
            config_path=self.config_path,
        )
    self.assertIn("regular file", str(cm.exception).lower())

def test_status_rename_line_refuses_even_when_both_paths_allowed(self):
    self.assertFalse(
        ioa._status_line_is_allowed_target_change(
            "R  execution/validators/config.yaml -> review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
            {
                "execution/validators/config.yaml",
                "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
            },
        )
    )
```

Add explicit status refusal tests:

```python
def test_status_rename_and_delete_refuse(self):
    allowed = {
        "execution/validators/config.yaml",
        "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
    }
    for raw in [
        "D  execution/validators/config.yaml",
        " D execution/validators/config.yaml",
        "R  execution/validators/config.yaml -> execution/validators/config.yaml.bak",
    ]:
        self.assertFalse(ioa._status_line_is_allowed_target_change(raw, allowed), raw)
```

- [ ] **Step 2: Extend preflight helper**

Change `_assert_ship_clean_preflight(...)` to accept `allowed_rel_paths: set[str]` while preserving the current `run_full_ship(...)` behavior by passing `{rel_path}`.

Required git command remains:

```python
[
    "git",
    "status",
    "--porcelain=v1",
    "--untracked-files=no",
    "--ignore-submodules=none",
]
```

Acceptance rule:

```python
def _status_line_is_allowed_target_change(raw_line: str, allowed_rel_paths: set[str]) -> bool:
    if len(raw_line) < 4:
        return False
    if " -> " in raw_line:
        return False
    status = raw_line[:2]
    if any(ch in status for ch in "DRU"):
        return False
    paths = _status_line_paths(raw_line)
    if set(paths) - allowed_rel_paths:
        return False
    if "U" in status:
        return False
    return status in {" M", "M ", "MM", "A ", "AM"}
```

This keeps untracked files invisible through `--untracked-files=no`, refuses renames via the ` -> ` guard, refuses deletes via `D`, refuses conflicts via `U`, and refuses any tracked path outside the allowed pair. The allowed set remains deliberately small: only ordinary modifications and additions for the exact two allowed files.

- [ ] **Step 3: Run preflight tests**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests::test_untracked_strategy_file_does_not_block_limits_apply tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests::test_unrelated_tracked_file_refuses_before_review -q
```

Expected: pass.

## Task 4: Rollback For Both Files

**Files:**
- Modify: `scripts/lib/invest_orchestrator_adapters.py`
- Test: `tests/test_invest_orchestrator_adapters.py`

- [ ] **Step 1: Write rollback tests**

Add:

```python
def test_failed_diff_review_restores_proposal_and_config(self):
    original_proposal = self.proposal_path.read_bytes()
    original_config = self.config_path.read_bytes()

    def review_runner(request):
        verdict = "APPROVE" if request.kind == "plan" else "NEEDS-ATTENTION"
        return ioa.ReviewGateResult(
            verdict=verdict,
            primary_used="minimax",
            fallback_used=False,
            log_path=f".code-reviews/{request.kind}.log",
        )

    with self.assertRaises(ioa.OrchestratorGateError) as cm:
        ioa.apply_approved_limits(
            self.proposal_path,
            approval=self._limits_approval(),
            review_runner=review_runner,
            approve_handler=self._approve_handler,
            config_path=self.config_path,
        )

    self.assertIn("diff review", str(cm.exception).lower())
    self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
    self.assertEqual(self.config_path.read_bytes(), original_config)
```

Also add:

- commit failure restores both files and unstages both paths.
- failed working-tree restore leaves marker and structured rollback result.
- incomplete marker refuses before review.
- v2 rollback marker with `paths` refuses before review and names both paths in the error.

- [ ] **Step 2: Generalize rollback result path data**

Keep `RollbackResult` backward compatible. Add optional `restored_paths: tuple[str, ...]` and `adapter_kind: str = "strategy"` rather than breaking existing fields.

Implement a new `_rollback_files_failure(...)` helper for multi-file rollback. Do not retrofit `run_full_ship(...)` unless the shared helper makes the call site simpler and tests stay clear.

Marker payload should include versioned multi-path data:

```json
{
  "version": 2,
  "adapter_kind": "limits",
  "paths": {
    "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md": "<sha256>",
    "execution/validators/config.yaml": "<sha256>"
  },
  "phase": "working_tree_restore",
  "updated_at": "<UTC ISO timestamp>"
}
```

Update `_refuse_if_incomplete_rollback(...)` or add `_refuse_if_incomplete_rollback_marker(...)` so it handles both:

- v1 marker: `strategy_path` plus `original_sha256`.
- v2 marker: `adapter_kind` plus `paths`.

The refusal message for v2 must include `adapter_kind`, `phase`, and all path keys.

- [ ] **Step 3: Run rollback tests**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests -q
```

Expected: pass.

## Task 5: Commit Message And Review Safety

**Files:**
- Modify: `scripts/lib/invest_orchestrator_adapters.py`
- Test: `tests/test_invest_orchestrator_adapters.py`

- [ ] **Step 1: Write commit-safety tests**

Add tests for:

- unsafe review log path with newline is rejected.
- unsafe limits trailer with newline is rejected.
- diff review with `fallback_used=True` refuses.
- diff review with `primary_used="codex"` refuses when required primary is `minimax`.
- `approved_at` passed to handler is exact and appears in proposal frontmatter.
- non-canonical `config_path` refuses before review.
- partial staging refuses before commit.
- post-handler unexpected config mutation refuses before diff review and rolls back.

Timestamp assertion:

```python
def test_handler_receives_approval_timestamp(self):
    approval = self._limits_approval(approved_at="2026-06-08T08:12:34.000000+00:00")
    ioa.apply_approved_limits(
        self.proposal_path,
        approval=approval,
        review_runner=self._approve_review,
        approve_handler=iss.handle_approve_limits,
        git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
        config_path=self.config_path,
        now_utc=_dt.datetime(2026, 6, 8, 8, 12, 35, tzinfo=_dt.timezone.utc),
    )
    text = self.proposal_path.read_text(encoding="utf-8")
    self.assertIn("approved_at: 2026-06-08 08:12:34+00:00", text)
```

If PyYAML emits a different scalar representation, parse frontmatter and compare `str(fm["approved_at"])`.

Staging assertion helper:

```python
def _assert_cached_diff_exactly(
    git_runner: GitRunner,
    repo_root: Path,
    expected_paths: set[str],
) -> None:
    result = git_runner(["git", "diff", "--cached", "--name-only"], repo_root)
    if result.returncode != 0:
        raise OrchestratorGateError(...)
    cached = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if cached != expected_paths:
        raise OrchestratorGateError(
            f"cached diff must contain exactly {sorted(expected_paths)}, got {sorted(cached)}"
        )
```

Call this after `git add` and before `git commit`.

Canonical config helper:

```python
CANONICAL_CONFIG_REL = "execution/validators/config.yaml"


def _assert_config_path_canonical(config_path: Path, repo_root: Path) -> str:
    expected = repo_root / CANONICAL_CONFIG_REL
    rel_input = _repo_relative(config_path, repo_root)
    if rel_input != CANONICAL_CONFIG_REL:
        raise OrchestratorGateError(
            f"config_path must be {CANONICAL_CONFIG_REL}, got {rel_input}"
        )
    return rel_input
```

This intentionally rejects case-variant path spellings such as `execution/validators/Config.YAML` through exact repo-relative spelling. `_assert_single_link_regular_file(config_path, "config file")` still runs before mutation, matching the existing regular-file discipline without adding extra TOCTOU machinery.

Review request scope assertion:

```python
def _assert_review_request_scope(
    request: ReviewRequest,
    *,
    expected_kind: str,
    expected_files: list[str],
) -> None:
    if request.kind != expected_kind or request.files != expected_files:
        raise OrchestratorGateError(
            f"{expected_kind} review scope drift: files={request.files!r}"
        )
```

Call this immediately before each `review_runner(request)` call.

- [ ] **Step 2: Implement limits commit message builder**

Add:

```python
def _build_limits_commit_message(
    hints: iss.LimitsCommitHints,
    *,
    approval: LimitsApproval,
    plan_review: ReviewGateResult,
    diff_review: ReviewGateResult,
) -> str:
    lines = [
        hints.commit_subject,
        "",
        f"Approved-By: {_safe_commit_field('approved_by', approval.approved_by)}",
        f"Approval-Captured-At: {_safe_commit_field('approved_at', approval.approved_at)}",
        f"Plan-Review-Log: {_safe_commit_field('plan_review.log_path', plan_review.log_path)}",
        f"Diff-Review-Log: {_safe_commit_field('diff_review.log_path', diff_review.log_path)}",
        "",
    ]
    lines.extend(_safe_trailer_line(line) for line in hints.trailers)
    return "\n".join(lines)
```

- [ ] **Step 3: Run commit-safety tests**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py::ApplyApprovedLimitsAdapterTests -q
```

Expected: pass.

## Task 6: Regression Scope

**Files:**
- Modify only if failures are real and in scope.

- [ ] **Step 1: Run adapter tests**

Run:

```bash
pytest tests/test_invest_orchestrator_adapters.py -q
```

Expected: all pass.

- [ ] **Step 2: Run limits handler tests**

Run:

```bash
pytest tests/test_invest_ship_strategy.py::HandleApproveLimitsTests tests/test_propose_limits.py::HandlerIntegrationTests -q
```

Expected: all pass.

- [ ] **Step 3: Run hook coverage for limits Check C**

Run:

```bash
pytest tests/test_pre_commit_hook.py tests/test_engine_gateway_discipline.py -q
```

Expected: all pass or pre-existing unrelated failures documented.

- [ ] **Step 4: Run full local tests if time allows before Checkpoint 2**

Run:

```bash
pytest
```

Expected: pass or document unrelated baseline failures.

## Plan Review Gate

Before any implementation:

```bash
./scripts/review.sh plan \
  --plan proposals/2026-06-08_apply-approved-limits-adapter-plan.md \
  --primary minimax \
  --focus "Review this K2Bi safety adapter plan for token-binding gaps, clean-tree preflight drift, handle_approve_limits weakening, rollback holes, git staging hazards, and any path where untracked strategy_cdns.md could block or influence limits apply." \
  --wait
```

Required acceptance:

- Verdict is `APPROVE`.
- Review state records `primary_used=minimax`.
- Review state records `fallback_used=false`.
- Any `NEEDS-ATTENTION` finding must be fixed in this plan before handoff.

## Builder Handoff

Route: Codex builder. Reason: this is safety-sensitive K2Bi adapter work with rollback, git, and validator-adjacent behavior.

Create `.codex/job.md` after plan review approval. The job must instruct Codex to:

- Read `AGENTS.md`, `CLAUDE.md`, this plan, and current adapter/tests first.
- Preserve unrelated untracked `wiki/strategies/strategy_cdns.md`.
- Implement only the files listed in this plan.
- Run the exact test commands in Task 6.
- Stop before commit, push, deploy, or `/ship`.

## Checkpoint 2 And Ship

After builder completion:

1. Inspect `git status --short`.
2. Confirm only intended files changed.
3. Run Checkpoint 2 diff review with MiniMax primary and no fallback acceptance.
4. Present findings to Keith and wait for gate decision.
5. If approved, run `/ship` discipline:
   - commit implementation,
   - push main only after Keith approves the commit message,
   - append `DEVLOG.md` in a follow-up commit,
   - decide whether scripts category requires VPS sync.

## Out Of Scope

- No changes to `handle_approve_limits(...)` semantics unless a test exposes an adapter integration bug that cannot be fixed in the adapter.
- No validator config edits except test fixtures.
- No broker, engine, strategy approval, or vault mirror changes.
- No automatic deploy or engine restart.
- No handling for batched limits proposals.
