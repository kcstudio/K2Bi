"""Callable safety adapters for external K2Bi orchestrators.

The adapters in this module expose existing interactive K2Bi gates as typed
functions. They do not weaken the underlying helpers. They add the missing
operator evidence checks before delegating to the existing writer, strategy
frontmatter, approval, review, and git paths.
"""

from __future__ import annotations

import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlparse

import yaml

from scripts.lib import invest_coach as ic
from scripts.lib import invest_ship_strategy as iss
from scripts.lib import invest_thesis as it
from scripts.lib import strategy_frontmatter as sf


MIN_TEXT_LEN = 1
MIN_REASON_LEN = 20
ALLOWED_OPERATOR_MARKS = frozenset({"verified", "refused", "override", "advisory"})
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_REVIEW_JOB_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z_[A-Za-z0-9]+$")
_REVIEW_VERDICT_RE = re.compile(
    r"^#\s+.+\breview\s+--\s+(APPROVE|NEEDS-ATTENTION)\s*$"
)
_SHIP_LEASE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,127}$")
_ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {"add", "commit", "restore", "status", "diff", "reset"}
)
ORCHESTRATOR_STATE_DIR = ".k2bi-orchestrator"
GIT_TIMEOUT_S = 30
REPO_ROOT_TIMEOUT_S = 5
REVIEW_TIMEOUT_S = 420
MAX_REVIEW_FOCUS_LEN = 2_000


@dataclass(frozen=True)
class RollbackResult:
    """Structured rollback outcome for a failed full-ship attempt."""

    strategy_path: str
    original_sha256: str
    marker_path: str
    index_restored: bool
    working_tree_restored: bool
    marker_cleared: bool
    events: tuple[dict[str, Any], ...]


class OrchestratorGateError(ValueError):
    """Raised when an adapter refuses a safety or completeness gate."""

    def __init__(
        self,
        message: str,
        *,
        rollback_result: RollbackResult | None = None,
    ) -> None:
        super().__init__(message)
        self.rollback_result = rollback_result


@dataclass(frozen=True)
class ThesisClaimDecision:
    """Operator-marked T7 claim evidence from an external orchestrator."""

    claim_id: str
    claim_text: str
    claim_load_bearing: bool
    source_url: str | None
    source_excerpt: str
    curated_framing: str
    operator_mark: str
    operator_note: str | None
    source_vendor: str
    spot_check_vendor: str | None = None


@dataclass(frozen=True)
class ThesisGateResult:
    thesis_result: it.ThesisResult
    audit: dict[str, Any]


@dataclass(frozen=True)
class StrategySpecDecision:
    """Confirmed T10/T11 decisions needed to author a full strategy file."""

    slug: str
    symbol: str
    sigid: str
    risk_envelope_pct: Any
    order: dict[str, Any]
    forward_guidance_metrics: list[dict[str, Any]]
    forward_guidance_status: str
    how_this_works: str
    bucket_rules: list[str]
    entry_rules: list[str]
    stop_rules: list[str]
    target_rules: list[str]
    hold_rules: list[str]
    kill_rules: list[str]
    accepted_gaps: list[str]
    forward_guidance_override_reason: str | None = None
    forward_guidance_waive_reason: str | None = None
    regime_filter: list[Any] | None = None
    date: str | None = None
    extra_frontmatter: dict[str, Any] | None = None


@dataclass(frozen=True)
class StrategySpecWriteResult:
    path: Path
    frontmatter: dict[str, Any]
    content_sha256: str


@dataclass(frozen=True)
class FullShipApproval:
    """Human final approval captured by the orchestrator ship gate."""

    final_approval_token: str
    approved_by: str
    approved_at: str
    ship_lease_id: str


@dataclass(frozen=True)
class ReviewRequest:
    kind: str
    path: Path
    files: list[str]
    focus: str
    required_primary: str = "minimax"


@dataclass(frozen=True)
class ReviewGateResult:
    verdict: str
    primary_used: str | None
    fallback_used: bool | None
    log_path: str


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class FullShipResult:
    slug: str
    commit_message: str
    commit_hints: iss.StrategyCommitHints
    plan_review: ReviewGateResult
    diff_review: ReviewGateResult
    events: list[dict[str, Any]]


GenerateThesisFunc = Callable[..., it.ThesisResult]
ApproveStrategyFunc = Callable[..., iss.StrategyCommitHints]
ReviewRunner = Callable[[ReviewRequest], ReviewGateResult]
GitRunner = Callable[[list[str], Path], CommandResult]


def verify_and_generate_thesis(
    thesis_input: it.ThesisInput,
    vault_root: Path,
    *,
    claim_decisions: list[ThesisClaimDecision],
    operator_override_reason: str | None = None,
    calx_override_acknowledged: bool = False,
    vendor_warning_acknowledged: bool = False,
    vendor_provenance: dict[str, Any] | None = None,
    generate_func: GenerateThesisFunc = it.generate_thesis,
    refresh: bool = False,
    learning_stage: str = "advanced",
    now: _dt.date | None = None,
) -> ThesisGateResult:
    """Enforce T7 evidence discipline before writing a thesis.

    The existing ``generate_thesis`` helper validates the aggregate
    verification block. This adapter validates the evidence the interactive
    coach requires before that block may exist: excerpts, source URLs, vendor
    provenance, vendor-must-differ, and CALX override framing.
    """

    audit = _validate_thesis_claim_decisions(
        claim_decisions,
        operator_override_reason=operator_override_reason,
        calx_override_acknowledged=calx_override_acknowledged,
        vendor_warning_acknowledged=vendor_warning_acknowledged,
        vendor_provenance=vendor_provenance,
    )

    claim_dicts = [_claim_decision_to_verification_dict(c) for c in claim_decisions]
    try:
        verification_dict = ic.build_verification_result(
            claim_dicts,
            vendor_provenance=vendor_provenance,
            operator_override_reason=operator_override_reason,
        )
    except ValueError as exc:
        raise OrchestratorGateError(str(exc)) from exc

    if verification_dict["status"] == "refuse":
        raise OrchestratorGateError(
            verification_dict.get("refuse_reason")
            or "verification gate refused before thesis generation"
        )

    verification = it.Verification(
        completed_at=str(verification_dict["completed_at"]),
        claims=[
            it.ClaimVerification(
                claim_id=str(c["claim_id"]),
                claim_text=str(c["claim_text"]),
                claim_load_bearing=bool(c["claim_load_bearing"]),
                source_url=c.get("source_url"),
                operator_check=str(c["operator_check"]),
                operator_note=c.get("operator_note"),
            )
            for c in claim_dicts
        ],
        status=str(verification_dict["status"]),
        override_reason=verification_dict.get("override_reason"),
        refuse_reason=verification_dict.get("refuse_reason"),
    )

    gated_input = replace(thesis_input, verification=verification)
    try:
        thesis_result = generate_func(
            gated_input,
            vault_root,
            refresh=refresh,
            learning_stage=learning_stage,
            now=now,
        )
    except ValueError as exc:
        raise OrchestratorGateError(str(exc)) from exc
    if not isinstance(thesis_result, it.ThesisResult):
        raise OrchestratorGateError(
            "generate_func must return scripts.lib.invest_thesis.ThesisResult"
        )
    return ThesisGateResult(thesis_result=thesis_result, audit=audit)


def write_complete_strategy_spec(
    decision: StrategySpecDecision,
    *,
    repo_root: Path = Path("."),
) -> StrategySpecWriteResult:
    """Atomically write a complete proposed strategy file.

    External callers pass confirmed T10/T11 decisions in. This function authors
    the canonical file rather than letting the caller hand-write K2Bi state.
    """

    _validate_strategy_decision(decision)
    strategy_path = repo_root / "wiki" / "strategies" / f"strategy_{decision.slug}.md"

    try:
        frontmatter = ic.build_canonical_strategy_frontmatter(
            name=decision.slug,
            symbol=decision.symbol,
            sigid=decision.sigid,
            risk_envelope_pct=decision.risk_envelope_pct,
            order=decision.order,
            forward_guidance_metrics=decision.forward_guidance_metrics,
            forward_guidance_status=decision.forward_guidance_status,
            forward_guidance_override_reason=decision.forward_guidance_override_reason,
            forward_guidance_waive_reason=decision.forward_guidance_waive_reason,
            regime_filter=decision.regime_filter,
            date=decision.date,
            extra=decision.extra_frontmatter,
        )
    except ValueError as exc:
        raise OrchestratorGateError(str(exc)) from exc

    body = _render_strategy_body(decision)
    content = _serialize_markdown(frontmatter, body)
    try:
        parsed = sf.parse(content)
        iss._validate_strategy_shape(strategy_path, parsed, content)
    except (ValueError, iss.ValidationError) as exc:
        raise OrchestratorGateError(str(exc)) from exc

    expected_sha = _sha256_bytes(content)
    sf.atomic_write_bytes(strategy_path, content)
    written_sha = _sha256_file_descriptor(strategy_path)
    if written_sha != expected_sha:
        raise OrchestratorGateError(
            f"strategy spec write verification failed for {strategy_path!s}"
        )
    return StrategySpecWriteResult(
        path=strategy_path,
        frontmatter=frontmatter,
        content_sha256=written_sha,
    )


def run_full_ship(
    strategy_path: Path,
    *,
    approval: FullShipApproval,
    review_runner: ReviewRunner | None = None,
    approve_handler: ApproveStrategyFunc = iss.handle_approve_strategy,
    git_runner: GitRunner | None = None,
    vault_root: Path | None = None,
    now: _dt.datetime | None = None,
    today: _dt.date | None = None,
    required_primary: str = "minimax",
) -> FullShipResult:
    """Run the callable full strategy approval ship path.

    This wraps the existing ``approve-strategy`` helper with plan review,
    explicit human final approval, diff review, staging, and commit. Any failed
    pre-commit gate restores the original file bytes and refuses before commit.
    The local flock prevents same-checkout interleaving. Cross-checkout or
    distributed orchestrators must also pass a non-empty ``ship_lease_id`` from
    their own mutual-exclusion layer, and the approval token binds that lease.
    """

    review_runner = review_runner or run_review_with_script
    git_runner = git_runner or run_git_command
    repo_root = _repo_root_for(strategy_path)
    rel_path = _repo_relative(strategy_path, repo_root)
    slug = _slug_from_strategy_path(strategy_path)
    events: list[dict[str, Any]] = []

    with _strategy_ship_lock(strategy_path, repo_root):
        _refuse_if_incomplete_rollback(strategy_path, repo_root)
        _assert_single_link_regular_file(strategy_path, "strategy file")
        original = strategy_path.read_bytes()
        original_sha = _sha256_bytes(original)
        _validate_full_ship_approval(approval, slug, original_sha)
        _assert_ship_clean_preflight(git_runner, repo_root, rel_path)
        events.append(
            {
                "event": "ship_start",
                "strategy": slug,
                "path": rel_path,
                "original_sha256": original_sha,
                "approved_at": approval.approved_at,
                "ship_lease_id": approval.ship_lease_id,
            }
        )
        staged = False

        plan_review = review_runner(
            ReviewRequest(
                kind="plan",
                path=strategy_path,
                files=[rel_path],
                focus=_strategy_plan_review_focus(),
                required_primary=required_primary,
            )
        )
        events.append(_review_event("plan_review_completed", plan_review))
        try:
            _require_review_approved(plan_review, "plan review", required_primary)

            hints = approve_handler(
                strategy_path,
                vault_root=vault_root,
                now=now,
                today=today,
            )
            events.append(
                {
                    "event": "approve_handler_completed",
                    "strategy": hints.slug,
                    "transition": hints.transition,
                }
            )

            diff_review = review_runner(
                ReviewRequest(
                    kind="diff",
                    path=strategy_path,
                    files=[rel_path],
                    focus="Review the strategy approval diff for gate bypass, missing trailers, or safety regression.",
                    required_primary=required_primary,
                )
            )
            events.append(_review_event("diff_review_completed", diff_review))
            _require_review_approved(diff_review, "diff review", required_primary)

            commit_message = _build_strategy_commit_message(
                hints,
                approval=approval,
                plan_review=plan_review,
                diff_review=diff_review,
            )
            _run_git_checked(
                git_runner,
                ["git", "add", rel_path],
                repo_root,
            )
            staged = True
            events.append({"event": "git_add_succeeded", "path": rel_path})
            _run_git_checked(
                git_runner,
                ["git", "commit", "--only", rel_path, "-m", commit_message],
                repo_root,
            )
            events.append({"event": "commit_succeeded", "path": rel_path})
        except Exception as exc:
            rollback_result = _rollback_ship_failure(
                git_runner=git_runner,
                repo_root=repo_root,
                rel_path=rel_path,
                strategy_path=strategy_path,
                original=original,
                original_sha=original_sha,
                prior_exc=exc,
                staged=staged,
                events=events,
            )
            if isinstance(exc, OrchestratorGateError):
                exc.rollback_result = rollback_result
            raise

        return FullShipResult(
            slug=slug,
            commit_message=commit_message,
            commit_hints=hints,
            plan_review=plan_review,
            diff_review=diff_review,
            events=events,
        )


def _rollback_ship_failure(
    *,
    git_runner: GitRunner,
    repo_root: Path,
    rel_path: str,
    strategy_path: Path,
    original: bytes,
    original_sha: str,
    prior_exc: Exception,
    staged: bool,
    events: list[dict[str, Any]],
) -> RollbackResult:
    """Restore working-tree and index state after a ship gate failure."""

    marker_path = _rollback_marker_path_for(strategy_path, repo_root)
    _write_rollback_marker(
        marker_path,
        rel_path=rel_path,
        original_sha=original_sha,
        phase="started",
    )
    unstage_exc: Exception | None = None
    restore_exc: Exception | None = None
    index_exc: Exception | None = None
    index_restored = not staged
    working_tree_restored = False
    marker_cleared = False
    if staged:
        _write_rollback_marker(
            marker_path,
            rel_path=rel_path,
            original_sha=original_sha,
            phase="index_cleanup",
        )
        try:
            _run_git_checked(
                git_runner,
                ["git", "restore", "--staged", rel_path],
                repo_root,
            )
            events.append({"event": "git_restore_staged_succeeded", "path": rel_path})
            index_restored = True
        except Exception as caught:
            unstage_exc = caught
            events.append(
                {
                    "event": "git_restore_staged_failed",
                    "path": rel_path,
                    "error": str(caught),
                }
            )
            try:
                _run_git_checked(
                    git_runner,
                    ["git", "reset", "HEAD", "--", rel_path],
                    repo_root,
                )
                events.append({"event": "git_reset_head_succeeded", "path": rel_path})
                index_restored = True
            except Exception as reset_caught:
                index_exc = reset_caught
                events.append(
                    {
                        "event": "git_reset_head_failed",
                        "path": rel_path,
                        "error": str(reset_caught),
                    }
                )
            if index_exc is None:
                try:
                    if not _is_index_clean(git_runner, repo_root, rel_path):
                        index_exc = OrchestratorGateError(
                            f"index still contains staged content for {rel_path}"
                        )
                except Exception as diff_caught:
                    index_exc = diff_caught
                if index_exc is not None:
                    events.append(
                        {
                            "event": "index_clean_check_failed",
                            "path": rel_path,
                            "error": str(index_exc),
                        }
                    )
                    index_restored = False
    try:
        _write_rollback_marker(
            marker_path,
            rel_path=rel_path,
            original_sha=original_sha,
            phase="working_tree_restore",
        )
        _restore_original_or_raise(strategy_path, original, original_sha, prior_exc)
        events.append({"event": "working_tree_restore_succeeded", "path": rel_path})
        working_tree_restored = True
    except Exception as caught:
        restore_exc = caught
        events.append(
            {
                "event": "working_tree_restore_failed",
                "path": rel_path,
                "error": str(caught),
            }
        )

    rollback_result = RollbackResult(
        strategy_path=rel_path,
        original_sha256=original_sha,
        marker_path=str(marker_path),
        index_restored=index_restored,
        working_tree_restored=working_tree_restored,
        marker_cleared=False,
        events=tuple(events),
    )

    if unstage_exc is not None or restore_exc is not None:
        details = [
            f"original ship error: {prior_exc!r}",
        ]
        if unstage_exc is not None:
            details.append(f"git restore --staged error: {unstage_exc!r}")
        if index_exc is not None:
            details.append(f"index cleanup error: {index_exc!r}")
        if restore_exc is not None:
            details.append(f"working-tree restore error: {restore_exc!r}")
        raise OrchestratorGateError(
            "; ".join(details),
            rollback_result=rollback_result,
        ) from (
            restore_exc or index_exc or unstage_exc
        )

    try:
        marker_path.unlink()
        marker_cleared = True
    except FileNotFoundError:
        marker_cleared = True
    except OSError as exc:
        failed_result = RollbackResult(
            strategy_path=rel_path,
            original_sha256=original_sha,
            marker_path=str(marker_path),
            index_restored=index_restored,
            working_tree_restored=working_tree_restored,
            marker_cleared=False,
            events=tuple(events),
        )
        raise OrchestratorGateError(
            f"rollback marker cleanup failed for {marker_path!s}",
            rollback_result=failed_result,
        ) from exc

    return RollbackResult(
        strategy_path=rel_path,
        original_sha256=original_sha,
        marker_path=str(marker_path),
        index_restored=index_restored,
        working_tree_restored=working_tree_restored,
        marker_cleared=marker_cleared,
        events=tuple(events),
    )


def _is_index_clean(git_runner: GitRunner, repo_root: Path, rel_path: str) -> bool:
    result = git_runner(
        ["git", "diff", "--cached", "--quiet", "--", rel_path],
        repo_root,
    )
    if not isinstance(result, CommandResult):
        raise OrchestratorGateError(
            f"git runner must return CommandResult for index check on {rel_path}"
        )
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise OrchestratorGateError(
        f"git diff --cached --quiet failed with exit {result.returncode}: "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )


def _assert_ship_clean_preflight(
    git_runner: GitRunner,
    repo_root: Path,
    rel_path: str,
) -> None:
    result = git_runner(
        [
            "git",
            "status",
            "--porcelain=v1",
            "--untracked-files=no",
            "--ignore-submodules=none",
        ],
        repo_root,
    )
    if not isinstance(result, CommandResult):
        raise OrchestratorGateError(
            f"git runner must return CommandResult for clean-tree preflight on {rel_path}"
        )
    if result.returncode != 0:
        raise OrchestratorGateError(
            "Ship-1a clean-tree preflight git status failed with exit "
            f"{result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )

    dirty = []
    for raw_line in result.stdout.splitlines():
        if not raw_line:
            continue
        if _status_line_is_allowed_target_change(raw_line, rel_path):
            continue
        dirty.append(raw_line)

    if dirty:
        preview = "; ".join(dirty[:5])
        raise OrchestratorGateError(
            "Ship-1a clean-tree preflight refused unrelated, unmerged, or "
            f"submodule state before ship: {preview}"
        )


def _status_line_is_allowed_target_change(raw_line: str, rel_path: str) -> bool:
    if len(raw_line) < 4:
        return False
    status = raw_line[:2]
    paths = _status_line_paths(raw_line)
    if paths != [rel_path]:
        return False
    if "U" in status:
        return False
    return status in {" M", "M ", "MM", "A ", "AM"}


def _status_line_paths(raw_line: str) -> list[str]:
    path_text = raw_line[3:]
    if " -> " in path_text:
        return path_text.split(" -> ", 1)
    return [path_text]


@contextmanager
def _strategy_ship_lock(strategy_path: Path, repo_root: Path) -> Iterator[None]:
    lock_path = _lock_path_for(strategy_path, repo_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise OrchestratorGateError(
                f"strategy {strategy_path.name!r} is already locked for full ship"
            ) from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _lock_path_for(strategy_path: Path, repo_root: Path) -> Path:
    slug = _slug_from_strategy_path(strategy_path)
    _validate_slug(slug)
    lock_dir = _orchestrator_state_path(repo_root, "locks")
    return _contained_child_path(lock_dir / f"{slug}.lock", lock_dir, "lock path")


def _rollback_marker_path_for(strategy_path: Path, repo_root: Path) -> Path:
    slug = _slug_from_strategy_path(strategy_path)
    _validate_slug(slug)
    marker_dir = _orchestrator_state_path(repo_root, "rollback")
    return _contained_child_path(marker_dir / f"{slug}.json", marker_dir, "rollback marker")


def _orchestrator_state_path(repo_root: Path, name: str) -> Path:
    _validate_slug(name)
    state_root = repo_root / ORCHESTRATOR_STATE_DIR
    return _contained_child_path(state_root / name, state_root, "orchestrator state path")


def _contained_child_path(path: Path, base: Path, label: str) -> Path:
    resolved_base = base.resolve(strict=False)
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_base)
    except ValueError as exc:
        raise OrchestratorGateError(
            f"{label} escapes expected directory {resolved_base!s}: {path!s}"
        ) from exc
    return path


def _write_rollback_marker(
    marker_path: Path,
    *,
    rel_path: str,
    original_sha: str,
    phase: str,
) -> None:
    payload = {
        "strategy_path": rel_path,
        "original_sha256": original_sha,
        "phase": phase,
        "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }
    sf.atomic_write_bytes(
        marker_path,
        (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"),
    )


def _refuse_if_incomplete_rollback(strategy_path: Path, repo_root: Path) -> None:
    marker_path = _rollback_marker_path_for(strategy_path, repo_root)
    if not marker_path.exists():
        return
    rel_marker = _repo_relative(marker_path, repo_root)
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OrchestratorGateError(
            f"incomplete rollback marker exists and is unreadable: {rel_marker}"
        ) from exc
    marker_strategy = payload.get("strategy_path")
    phase = payload.get("phase", "unknown")
    raise OrchestratorGateError(
        "incomplete rollback marker exists for "
        f"{marker_strategy or strategy_path.name} at phase {phase!r}: {rel_marker}"
    )


def _review_event(event: str, review: ReviewGateResult) -> dict[str, Any]:
    return {
        "event": event,
        "verdict": review.verdict,
        "primary_used": review.primary_used,
        "fallback_used": review.fallback_used,
        "log_path": review.log_path,
    }


def run_git_command(cmd: list[str], cwd: Path) -> CommandResult:
    """Run a git command and return a small typed result."""

    _validate_git_command(cmd)
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=GIT_TIMEOUT_S,
    )
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_review_with_script(request: ReviewRequest) -> ReviewGateResult:
    """Run ``scripts/review.sh`` synchronously and return typed review state."""

    repo_root = _repo_root_for(request.path)
    focus = _safe_review_focus(request.focus)
    review_script = repo_root / "scripts" / "review.sh"
    _assert_executable_regular_file(review_script, "review script")
    cmd = [
        str(review_script),
        request.kind,
    ]
    if request.kind == "plan":
        cmd.extend(["--plan", _safe_review_path(request.path, repo_root, "review plan")])
    elif request.kind == "diff":
        cmd.extend(["--files", ",".join(_safe_review_files(request.files, repo_root))])
    else:
        raise OrchestratorGateError(
            f"unsupported review kind {request.kind!r}; expected plan or diff"
        )
    cmd.extend([
        "--primary",
        request.required_primary,
        "--focus",
        focus,
        "--wait",
    ])
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(repo_root),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=REVIEW_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired as exc:
        raise OrchestratorGateError(
            f"{request.kind} review timed out after {REVIEW_TIMEOUT_S}s"
        ) from exc
    if completed.returncode != 0:
        raise OrchestratorGateError(
            f"{request.kind} review failed with exit {completed.returncode}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
    if completed.stderr.strip():
        raise OrchestratorGateError(
            f"{request.kind} review returned stderr despite exit 0: "
            f"{completed.stderr.strip()}"
        )
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise OrchestratorGateError(
            f"{request.kind} review returned non-JSON output"
        ) from exc

    job_id = envelope.get("job_id")
    log_path = _validate_review_envelope(envelope, repo_root)
    state_path = repo_root / ".code-reviews" / f"{job_id}.json"
    state = _load_review_state(state_path)
    verdict = _extract_review_verdict(log_path)
    return ReviewGateResult(
        verdict=verdict,
        primary_used=state.get("primary_used"),
        fallback_used=state.get("fallback_used"),
        log_path=(
            str(log_path.relative_to(repo_root.resolve()))
            if log_path.exists()
            else str(log_path)
        ),
    )


def strategy_file_sha256(path: Path) -> str:
    """Return the SHA-256 digest of the strategy file bytes."""

    return _sha256_bytes(path.read_bytes())


def _validate_thesis_claim_decisions(
    claim_decisions: list[ThesisClaimDecision],
    *,
    operator_override_reason: str | None,
    calx_override_acknowledged: bool,
    vendor_warning_acknowledged: bool,
    vendor_provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    if not claim_decisions:
        raise OrchestratorGateError("claim_decisions must not be empty")
    if vendor_provenance and not vendor_warning_acknowledged:
        vendor = vendor_provenance.get("vendor", "unknown vendor")
        raise OrchestratorGateError(
            f"T7 vendor warning for {vendor!r} must be acknowledged before generation"
        )

    override_marks_present = False
    audit_claims: list[dict[str, Any]] = []
    for claim in claim_decisions:
        _validate_claim_basics(claim)
        if claim.claim_load_bearing:
            _validate_load_bearing_claim_evidence(claim, vendor_provenance)
        mark = claim.operator_mark.strip().lower()
        if mark in {"refused", "override"} and claim.claim_load_bearing:
            override_marks_present = True
        audit_claims.append(_claim_decision_to_audit(claim))

    if override_marks_present:
        if not operator_override_reason or len(operator_override_reason.strip()) < MIN_REASON_LEN:
            raise OrchestratorGateError(
                f"operator_override_reason >= {MIN_REASON_LEN} chars is required "
                "when a load-bearing claim is refused or overridden"
            )
        if not calx_override_acknowledged:
            raise OrchestratorGateError(
                "override path requires explicit CALX framing acknowledgement "
                "(L-2026-04-30-001)"
            )

    return {
        "completed_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "calx_override_acknowledged": calx_override_acknowledged,
        "vendor_warning_acknowledged": vendor_warning_acknowledged,
        "claims": audit_claims,
    }


def _validate_claim_basics(claim: ThesisClaimDecision) -> None:
    _require_text("claim_id", claim.claim_id)
    _require_text("claim_text", claim.claim_text)
    mark = claim.operator_mark.strip().lower() if isinstance(claim.operator_mark, str) else ""
    if mark not in ALLOWED_OPERATOR_MARKS:
        raise OrchestratorGateError(
            f"claim {claim.claim_id!r} operator_mark {claim.operator_mark!r} "
            f"must be one of {sorted(ALLOWED_OPERATOR_MARKS)}"
        )
    if mark in {"refused", "override"}:
        note = claim.operator_note or ""
        if len(note.strip()) < MIN_REASON_LEN:
            raise OrchestratorGateError(
                f"claim {claim.claim_id!r} operator_mark={mark!r} requires "
                f"operator_note >= {MIN_REASON_LEN} chars"
            )


def _validate_load_bearing_claim_evidence(
    claim: ThesisClaimDecision,
    vendor_provenance: dict[str, Any] | None,
) -> None:
    _require_text(f"claim {claim.claim_id} source_excerpt", claim.source_excerpt)
    _require_text(f"claim {claim.claim_id} curated_framing", claim.curated_framing)
    _require_text(f"claim {claim.claim_id} source_vendor", claim.source_vendor)
    _validate_http_url(f"claim {claim.claim_id} source_url", claim.source_url)
    if claim.spot_check_vendor:
        if _same_vendor(claim.spot_check_vendor, claim.source_vendor):
            raise OrchestratorGateError(
                f"claim {claim.claim_id!r} violates vendor-must-differ: "
                "spot_check_vendor matches source_vendor"
            )
        t55_vendor = (vendor_provenance or {}).get("vendor")
        if t55_vendor and _same_vendor(claim.spot_check_vendor, str(t55_vendor)):
            raise OrchestratorGateError(
                f"claim {claim.claim_id!r} violates vendor-must-differ: "
                "spot_check_vendor matches T5.5 vendor"
            )


def _claim_decision_to_verification_dict(claim: ThesisClaimDecision) -> dict[str, Any]:
    mark = claim.operator_mark.strip().lower()
    operator_check = "refused" if mark == "override" else mark
    return {
        "claim_id": claim.claim_id,
        "claim_text": claim.claim_text,
        "claim_load_bearing": claim.claim_load_bearing,
        "source_url": claim.source_url,
        "operator_check": operator_check,
        "operator_note": claim.operator_note,
    }


def _claim_decision_to_audit(claim: ThesisClaimDecision) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "claim_text": claim.claim_text,
        "claim_load_bearing": claim.claim_load_bearing,
        "source_url": claim.source_url,
        "source_excerpt": claim.source_excerpt,
        "curated_framing": claim.curated_framing,
        "operator_mark": claim.operator_mark,
        "operator_note": claim.operator_note,
        "source_vendor": claim.source_vendor,
        "spot_check_vendor": claim.spot_check_vendor,
    }


def _validate_strategy_decision(decision: StrategySpecDecision) -> None:
    _validate_slug(decision.slug)
    _require_text("symbol", decision.symbol)
    _require_text("sigid", decision.sigid)
    _require_text("How This Works", decision.how_this_works)
    for label, items in [
        ("bucket_rules", decision.bucket_rules),
        ("entry_rules", decision.entry_rules),
        ("stop_rules", decision.stop_rules),
        ("target_rules", decision.target_rules),
        ("hold_rules", decision.hold_rules),
        ("kill_rules", decision.kill_rules),
        ("accepted_gaps", decision.accepted_gaps),
    ]:
        _require_text_list(label, items)


def _render_strategy_body(decision: StrategySpecDecision) -> str:
    sections = [
        f"# Strategy: {decision.slug}",
        "",
        "## How This Works",
        "",
        decision.how_this_works.strip(),
        "",
        _render_list_section("Bucket Rules", decision.bucket_rules),
        _render_list_section("Entry Rules", decision.entry_rules),
        _render_list_section("Stop Rules", decision.stop_rules),
        _render_list_section("Target Rules", decision.target_rules),
        _render_list_section("Hold Rules", decision.hold_rules),
        _render_list_section("Kill Rules", decision.kill_rules),
        _render_forward_guidance_summary(decision),
        _render_list_section("Accepted Gaps", decision.accepted_gaps),
        ic.render_accepted_gaps_section().strip(),
        "",
    ]
    return "\n".join(sections)


def _render_list_section(title: str, items: list[str]) -> str:
    body = "\n".join(f"- {item.strip()}" for item in items)
    return f"## {title}\n\n{body}\n"


def _render_forward_guidance_summary(decision: StrategySpecDecision) -> str:
    lines = [
        "## Forward Guidance Reconciliation",
        "",
        f"- Status: {decision.forward_guidance_status}",
    ]
    if decision.forward_guidance_override_reason:
        lines.append(f"- Override reason: {decision.forward_guidance_override_reason}")
    if decision.forward_guidance_waive_reason:
        lines.append(f"- Waive reason: {decision.forward_guidance_waive_reason}")
    for metric in decision.forward_guidance_metrics:
        name = metric.get("metric", "unknown")
        threshold = metric.get("locked_threshold_text", "unknown threshold")
        guide = metric.get("guide_range_text", "unknown guide")
        inside = metric.get("sits_inside_guide")
        lines.append(f"- {name}: {threshold}; guide={guide}; inside={inside}")
    return "\n".join(lines) + "\n"


def _serialize_markdown(frontmatter: dict[str, Any], body: str) -> bytes:
    yaml_text = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    )
    return f"---\n{yaml_text}---\n\n{body.rstrip()}\n".encode("utf-8")


def _validate_full_ship_approval(
    approval: FullShipApproval,
    slug: str,
    strategy_sha256: str,
) -> None:
    _require_text("approved_at", approval.approved_at)
    try:
        approved_at = _dt.datetime.fromisoformat(approval.approved_at)
    except ValueError as exc:
        raise OrchestratorGateError(
            f"approved_at must be ISO-8601 datetime, got {approval.approved_at!r}"
        ) from exc
    if approved_at.tzinfo is None or approved_at.utcoffset() is None:
        raise OrchestratorGateError(
            "approved_at must include an explicit timezone offset"
        )
    if approved_at.utcoffset() != _dt.timedelta(0):
        raise OrchestratorGateError("approved_at must be UTC")
    _validate_ship_lease_id(approval.ship_lease_id)
    expected = (
        f"APPROVE_STRATEGY:{slug}:{strategy_sha256}:"
        f"{approval.approved_at}:{approval.ship_lease_id}"
    )
    if approval.final_approval_token != expected:
        raise OrchestratorGateError(
            "final approval token must bind slug, content hash, approved_at, "
            "and ship_lease_id as "
            f"{expected!r}; got "
            f"{approval.final_approval_token!r}"
        )
    _require_text("approved_by", approval.approved_by)


def _require_review_approved(
    review: ReviewGateResult,
    label: str,
    required_primary: str,
) -> None:
    if review.verdict != "APPROVE":
        raise OrchestratorGateError(
            f"{label} refused: verdict={review.verdict!r} log={review.log_path}"
        )
    if review.primary_used != required_primary or review.fallback_used is not False:
        raise OrchestratorGateError(
            f"{label} must use primary_used={required_primary} and "
            f"fallback_used=false; got primary_used={review.primary_used!r}, "
            f"fallback_used={review.fallback_used!r}"
        )


def _run_git_checked(git_runner: GitRunner, cmd: list[str], cwd: Path) -> None:
    result = git_runner(cmd, cwd)
    if not isinstance(result, CommandResult):
        raise OrchestratorGateError(
            f"git runner must return CommandResult for {' '.join(cmd)}"
        )
    if result.returncode != 0:
        raise OrchestratorGateError(
            f"{' '.join(cmd)} failed with exit {result.returncode}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


def _build_strategy_commit_message(
    hints: iss.StrategyCommitHints,
    *,
    approval: FullShipApproval,
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


def _strategy_plan_review_focus() -> str:
    return (
        "Review this proposed strategy spec for look-ahead bias, unrealistic "
        "order assumptions, regime_filter mismatch, weak How This Works clarity, "
        "and any K2Bi safety gate bypass."
    )


def _extract_review_verdict(log_path: Path) -> str:
    if not log_path.is_file():
        return "LOG_MISSING"
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = _REVIEW_VERDICT_RE.match(line.strip())
        if match:
            return match.group(1)
        if line.lstrip().startswith("{"):
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            verdict = payload.get("verdict")
            if verdict in {"APPROVE", "NEEDS-ATTENTION"}:
                return str(verdict)
    return "UNKNOWN_VERDICT"


def _slug_from_strategy_path(path: Path) -> str:
    stem = path.stem
    if not stem.startswith("strategy_"):
        raise OrchestratorGateError(
            f"strategy path must be named strategy_<slug>.md, got {path.name!r}"
        )
    slug = stem[len("strategy_"):]
    _validate_slug(slug)
    return slug


def _repo_root_for(path: Path) -> Path:
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(path.parent),
            text=True,
            encoding="utf-8",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=REPO_ROOT_TIMEOUT_S,
        ).strip()
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise OrchestratorGateError(
            f"cannot determine git repository root for {path!s}; refusing "
            "to continue because full ship requires a real git checkout"
        ) from exc
    if not root:
        raise OrchestratorGateError(
            f"cannot determine git repository root for {path!s}; got empty git output"
        )
    return Path(root)


def _repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return path.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise OrchestratorGateError(
            f"path {path!s} resolves outside repo root {repo_root!s}; "
            "refusing to pass an absolute or escaped path to git"
        ) from exc


def _validate_slug(slug: str) -> None:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise OrchestratorGateError(
            f"slug must match {_SLUG_RE.pattern}, got {slug!r}"
        )


def _validate_http_url(label: str, value: str | None) -> None:
    _require_text(label, value)
    parsed = urlparse(str(value))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OrchestratorGateError(
            f"{label} must be HTTP/HTTPS with a host, got {value!r}"
        )


def _require_text(label: str, value: Any, min_len: int = MIN_TEXT_LEN) -> None:
    if not isinstance(value, str) or len(value.strip()) < min_len:
        raise OrchestratorGateError(
            f"{label} must be a non-empty string"
        )


def _require_text_list(label: str, items: list[str]) -> None:
    if not isinstance(items, list) or not items:
        raise OrchestratorGateError(f"{label} must be a non-empty list")
    for i, item in enumerate(items):
        _require_text(f"{label}[{i}]", item)


def _same_vendor(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _restore_original_or_raise(
    path: Path,
    original: bytes,
    original_sha: str,
    prior_exc: Exception,
) -> None:
    try:
        _assert_single_link_regular_file(path, "rollback target")
        sf.atomic_write_bytes(path, original)
        if _sha256_file_descriptor(path) != original_sha:
            raise OrchestratorGateError(
                f"rollback verification failed for {path!s}; restored bytes "
                "do not match the pre-mutation checksum"
            )
    except Exception as restore_exc:
        raise OrchestratorGateError(
            f"rollback failed after ship gate error {prior_exc!r}; "
            f"{path!s} may be approved but uncommitted"
        ) from restore_exc


def _validate_review_envelope(envelope: dict[str, Any], repo_root: Path) -> Path:
    job_id = envelope.get("job_id")
    if not isinstance(job_id, str) or not _REVIEW_JOB_ID_RE.match(job_id):
        raise OrchestratorGateError(
            f"review envelope missing valid job_id: {job_id!r}"
        )
    raw_log = envelope.get("log_path")
    if not isinstance(raw_log, str) or not raw_log.strip():
        raise OrchestratorGateError("review envelope missing log_path")
    log_path = Path(raw_log)
    if log_path.is_absolute():
        raise OrchestratorGateError(
            f"review envelope log_path must be repo-relative, got {raw_log!r}"
        )
    expected_log = Path(".code-reviews") / f"{job_id}.log"
    if log_path.as_posix() != expected_log.as_posix():
        raise OrchestratorGateError(
            f"review log_path {raw_log!r} does not match expected "
            f"{expected_log.as_posix()!r}"
        )
    if any(part == ".." for part in log_path.parts):
        raise OrchestratorGateError(
            f"review log_path must not contain traversal: {raw_log!r}"
        )
    resolved = (repo_root / log_path).resolve(strict=False)
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise OrchestratorGateError(
            f"review envelope log_path escapes repo root: {raw_log!r}"
        ) from exc
    try:
        log_info = (repo_root / log_path).lstat()
    except OSError as exc:
        raise OrchestratorGateError(
            f"review log_path does not exist: {raw_log!r}"
        ) from exc
    if stat.S_ISLNK(log_info.st_mode) or not stat.S_ISREG(log_info.st_mode):
        raise OrchestratorGateError(
            f"review log_path must be a regular non-symlink file: {raw_log!r}"
        )
    return resolved


def _load_review_state(state_path: Path) -> dict[str, Any]:
    if not state_path.is_file():
        raise OrchestratorGateError(
            f"review state file missing: {state_path!s}"
        )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state.get("primary_used"), str):
        raise OrchestratorGateError("review state missing primary_used")
    if not isinstance(state.get("fallback_used"), bool):
        raise OrchestratorGateError("review state missing fallback_used")
    return state


def _validate_git_command(cmd: list[str]) -> None:
    if not cmd or cmd[0] != "git":
        raise OrchestratorGateError(
            f"run_git_command only accepts git commands, got {cmd!r}"
        )
    if len(cmd) < 2 or cmd[1] not in _ALLOWED_GIT_SUBCOMMANDS:
        raise OrchestratorGateError(
            f"git subcommand must be one of {sorted(_ALLOWED_GIT_SUBCOMMANDS)}, "
            f"got {cmd[1] if len(cmd) > 1 else None!r}"
        )


def _sha256_file_descriptor(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_commit_field(label: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestratorGateError(f"unsafe commit field {label}: empty")
    if any(ch in value for ch in "\r\n"):
        raise OrchestratorGateError(
            f"unsafe commit field {label}: contains newline"
        )
    if any(ord(ch) < 32 for ch in value):
        raise OrchestratorGateError(
            f"unsafe commit field {label}: contains control character"
        )
    if re.match(r"^[A-Za-z0-9-]+:\s", value):
        raise OrchestratorGateError(
            f"unsafe commit field {label}: looks like a git trailer"
        )
    return value.strip()


def _safe_trailer_line(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestratorGateError("unsafe trailer line: empty")
    if any(ch in value for ch in "\r\n"):
        raise OrchestratorGateError("unsafe trailer line: contains newline")
    if any(ord(ch) < 32 for ch in value):
        raise OrchestratorGateError(
            "unsafe trailer line: contains control character"
        )
    if not re.match(r"^[A-Za-z0-9-]+:\s.+$", value.strip()):
        raise OrchestratorGateError(
            f"unsafe trailer line: malformed trailer {value!r}"
        )
    return value.strip()


def _safe_review_focus(value: str) -> str:
    if not isinstance(value, str):
        raise OrchestratorGateError("review focus must be a string")
    focus = value.strip()
    if not focus:
        return ""
    if len(focus) > MAX_REVIEW_FOCUS_LEN:
        raise OrchestratorGateError(
            f"review focus exceeds {MAX_REVIEW_FOCUS_LEN} characters"
        )
    if any(ch in focus for ch in "\r\n\x00"):
        raise OrchestratorGateError("review focus contains unsafe control characters")
    if any(ord(ch) < 32 for ch in focus):
        raise OrchestratorGateError("review focus contains unsafe control characters")
    if "../" in focus or "..\\" in focus:
        raise OrchestratorGateError("review focus contains path traversal text")
    return focus


def _safe_review_files(files: list[str], repo_root: Path) -> list[str]:
    if not isinstance(files, list) or not files:
        raise OrchestratorGateError("review files must be a non-empty list")
    return [_safe_review_file(value, repo_root) for value in files]


def _safe_review_file(value: str, repo_root: Path) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OrchestratorGateError("review file must be a non-empty string")
    if "," in value or any(ch in value for ch in "\r\n\x00"):
        raise OrchestratorGateError(
            f"review file contains unsafe characters: {value!r}"
        )
    if any(ord(ch) < 32 for ch in value):
        raise OrchestratorGateError(
            f"review file contains unsafe control character: {value!r}"
        )
    path = Path(value)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise OrchestratorGateError(
            f"review file must be repo-relative and contained: {value!r}"
        )
    return _repo_relative(repo_root / path, repo_root)


def _safe_review_path(path: Path, repo_root: Path, label: str) -> str:
    rel = _repo_relative(path, repo_root)
    return _safe_review_file(rel, repo_root)


def _validate_ship_lease_id(value: str) -> None:
    _require_text("ship_lease_id", value)
    if not _SHIP_LEASE_RE.match(value):
        raise OrchestratorGateError(
            "ship_lease_id must be 3-128 chars of letters, digits, dot, "
            "underscore, or hyphen"
        )


def _assert_single_link_regular_file(path: Path, label: str) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise OrchestratorGateError(
            f"{label} must be a regular file; cannot stat {path!s}"
        ) from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise OrchestratorGateError(
            f"{label} must be a regular file, not a symlink or special file: {path!s}"
        )
    if info.st_nlink != 1:
        raise OrchestratorGateError(
            f"{label} must not be hardlinked; link count is {info.st_nlink}: {path!s}"
        )


def _assert_executable_regular_file(path: Path, label: str) -> None:
    _assert_single_link_regular_file(path, label)
    if not os.access(path, os.X_OK):
        raise OrchestratorGateError(f"{label} must be executable: {path!s}")
