"""Tests for callable K2Bi orchestrator safety adapters."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
import fcntl
import json
import os
from unittest.mock import patch
from datetime import date
from pathlib import Path

from scripts.lib import invest_orchestrator_adapters as ioa
from scripts.lib import invest_ship_strategy as iss
from scripts.lib import invest_thesis as it
from scripts.lib import strategy_frontmatter as sf
from tests.test_invest_ship_strategy import _make_tmp_repo, _write_strategy
from tests.test_invest_thesis import _make_default_input, _seed_vault


def _verified_claim(**overrides):
    data = {
        "claim_id": "claim-1",
        "claim_text": "Revenue grew 20 percent year over year.",
        "claim_load_bearing": True,
        "source_url": "https://example.com/source",
        "source_excerpt": "The company reported revenue grew 20 percent year over year.",
        "curated_framing": "The curated info set framed revenue growth as 20 percent.",
        "operator_mark": "verified",
        "operator_note": None,
        "source_vendor": "SEC filing",
        "spot_check_vendor": "Perplexity",
    }
    data.update(overrides)
    return ioa.ThesisClaimDecision(**data)


class ThesisGateAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_vault = Path(tempfile.mkdtemp(prefix="ioa_thesis_"))
        _seed_vault(self.tmp_vault)
        self.thesis_input = _make_default_input()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_vault, ignore_errors=True)

    def test_missing_source_excerpt_refuses_before_generate(self):
        calls = []

        def fake_generate(*args, **kwargs):
            calls.append((args, kwargs))
            return it.ThesisResult(path=self.tmp_vault / "wiki/tickers/NVDA.md", written=True)

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.verify_and_generate_thesis(
                self.thesis_input,
                self.tmp_vault,
                claim_decisions=[_verified_claim(source_excerpt="")],
                generate_func=fake_generate,
            )

        self.assertIn("source_excerpt", str(cm.exception))
        self.assertEqual(calls, [])

    def test_vendor_must_differ_refuses_before_generate(self):
        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.verify_and_generate_thesis(
                self.thesis_input,
                self.tmp_vault,
                claim_decisions=[
                    _verified_claim(source_vendor="Kimi DR", spot_check_vendor="Kimi DR")
                ],
                generate_func=lambda *args, **kwargs: self.fail("generate must not run"),
            )

        self.assertIn("vendor-must-differ", str(cm.exception))

    def test_refused_claim_without_calx_framed_override_refuses(self):
        claim = _verified_claim(
            operator_mark="refused",
            operator_note="The source does not support the curated claim.",
        )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.verify_and_generate_thesis(
                self.thesis_input,
                self.tmp_vault,
                claim_decisions=[claim],
                operator_override_reason="The thesis can stand without this claim.",
                calx_override_acknowledged=False,
                generate_func=lambda *args, **kwargs: self.fail("generate must not run"),
            )

        self.assertIn("L-2026-04-30-001", str(cm.exception))

    def test_full_t7_discipline_calls_generate_with_operator_override(self):
        captured = {}

        def fake_generate(thesis_input, vault_root, **kwargs):
            captured["verification"] = thesis_input.verification
            captured["vault_root"] = vault_root
            captured["kwargs"] = kwargs
            return it.ThesisResult(path=vault_root / "wiki/tickers/NVDA.md", written=True)

        claim = _verified_claim(
            operator_mark="refused",
            operator_note="The primary source contradicts this claim, but it is advisory.",
        )
        result = ioa.verify_and_generate_thesis(
            self.thesis_input,
            self.tmp_vault,
            claim_decisions=[claim],
            operator_override_reason=(
                "Override accepted after CALX L-2026-04-30-001 framing; "
                "the refused claim is advisory and not load-bearing to conviction."
            ),
            calx_override_acknowledged=True,
            generate_func=fake_generate,
            now=date(2026, 4, 19),
            refresh=True,
            learning_stage="novice",
        )

        self.assertTrue(result.thesis_result.written)
        self.assertEqual(captured["vault_root"], self.tmp_vault)
        self.assertEqual(captured["kwargs"]["now"], date(2026, 4, 19))
        self.assertTrue(captured["kwargs"]["refresh"])
        self.assertEqual(captured["kwargs"]["learning_stage"], "novice")
        verification = captured["verification"]
        self.assertEqual(verification.status, "operator-override")
        self.assertEqual(verification.claims[0].operator_check, "refused")
        self.assertEqual(result.audit["claims"][0]["source_excerpt"], claim.source_excerpt)
        self.assertEqual(result.audit["claims"][0]["source_vendor"], "SEC filing")

    def test_generate_func_must_return_thesis_result(self):
        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.verify_and_generate_thesis(
                self.thesis_input,
                self.tmp_vault,
                claim_decisions=[_verified_claim()],
                generate_func=lambda *args, **kwargs: None,
            )

        self.assertIn("ThesisResult", str(cm.exception))


class StrategySpecWriterAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="ioa_strategy_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)

    def _decision(self, **overrides) -> ioa.StrategySpecDecision:
        data = {
            "slug": "spy",
            "symbol": "SPY",
            "sigid": "2026-06-04-test-signal",
            "risk_envelope_pct": "0.01",
            "order": {
                "ticker": "SPY",
                "side": "buy",
                "qty": 1,
                "order_type": "LMT",
                "limit_price": "500.00",
                "stop_loss": "490.00",
                "time_in_force": "DAY",
            },
            "forward_guidance_metrics": [
                {
                    "metric": "none",
                    "locked_threshold_text": "No thresholded metric",
                    "guide_source_text": "operator-pasted: no guide applies",
                    "guide_range_text": "no quantitative guide",
                    "sits_inside_guide": False,
                }
            ],
            "forward_guidance_status": "pass",
            "how_this_works": "Buy SPY only when the operator-approved thesis remains intact.",
            "bucket_rules": ["Bucket 4 exits when thesis-breaking news lands."],
            "entry_rules": ["Enter only after operator confirms the rule set."],
            "stop_rules": ["Stop at 490.00."],
            "target_rules": ["Review at 510.00 and 525.00."],
            "hold_rules": ["Maximum hold is 30 trading days."],
            "kill_rules": ["Kill if the thesis is invalidated."],
            "accepted_gaps": ["No regime filter for this first paper trade."],
        }
        data.update(overrides)
        return ioa.StrategySpecDecision(**data)

    def test_writes_complete_strategy_spec_that_passes_ship_shape(self):
        result = ioa.write_complete_strategy_spec(self._decision(), repo_root=self.repo)

        self.assertEqual(result.path, self.repo / "wiki/strategies/strategy_spy.md")
        content = result.path.read_bytes()
        fm = sf.parse(content)
        iss._validate_strategy_shape(result.path, fm, content)
        self.assertEqual(fm["status"], "proposed")
        self.assertIn(b"## How This Works", content)
        self.assertIn(b"## Bucket Rules", content)
        self.assertIn(b"## Accepted Gaps", content)
        self.assertEqual(result.content_sha256, ioa.strategy_file_sha256(result.path))

    def test_write_verification_uses_descriptor_digest(self):
        with patch(
            "scripts.lib.invest_orchestrator_adapters._sha256_file_descriptor",
            return_value="0" * 64,
        ) as digest:
            with self.assertRaises(ioa.OrchestratorGateError) as cm:
                ioa.write_complete_strategy_spec(self._decision(), repo_root=self.repo)

        self.assertTrue(digest.called)
        self.assertIn("write verification", str(cm.exception).lower())

    def test_rejects_empty_how_this_works(self):
        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.write_complete_strategy_spec(
                self._decision(how_this_works="   "),
                repo_root=self.repo,
            )

        self.assertIn("How This Works", str(cm.exception))
        self.assertFalse((self.repo / "wiki/strategies/strategy_spy.md").exists())


class FullShipWrapperAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.parent_sha = _make_tmp_repo()
        self.strategy_path = _write_strategy(self.repo, slug="spy")
        review_script = self.repo / "scripts" / "review.sh"
        review_script.parent.mkdir(parents=True, exist_ok=True)
        review_script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        review_script.chmod(0o755)

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

    def _approval(self, **overrides) -> ioa.FullShipApproval:
        approved_at = overrides.pop("approved_at", "2026-06-04T04:00:00.123456+00:00")
        ship_lease_id = overrides.pop("ship_lease_id", "lease-spy-20260604")
        digest = ioa.strategy_file_sha256(self.strategy_path)
        data = {
            "final_approval_token": (
                f"APPROVE_STRATEGY:spy:{digest}:{approved_at}:{ship_lease_id}"
            ),
            "approved_by": "Keith",
            "approved_at": approved_at,
            "ship_lease_id": ship_lease_id,
        }
        data.update(overrides)
        return ioa.FullShipApproval(**data)

    def _approve_handler(self, path: Path, **kwargs) -> iss.StrategyCommitHints:
        text = path.read_text(encoding="utf-8").replace("status: proposed", "status: approved")
        path.write_text(text, encoding="utf-8")
        return iss.StrategyCommitHints(
            file=str(path),
            slug="spy",
            transition="proposed -> approved",
            commit_subject="feat(strategy): approve spy",
            trailers=iss.build_trailers("strategy", "proposed -> approved", "spy"),
            timestamp_field="approved_at",
            timestamp_value="2026-06-04T04:00:00+00:00",
            parent_commit_sha=self.parent_sha,
        )

    def test_missing_final_approval_token_fails_before_mutation(self):
        git_calls = []

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(final_approval_token=""),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: git_calls.append(cmd),
            )

        self.assertIn("final approval", str(cm.exception).lower())
        self.assertIn("status: proposed", self.strategy_path.read_text(encoding="utf-8"))
        self.assertEqual(git_calls, [])

    def test_missing_external_ship_lease_fails_before_review(self):
        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(ship_lease_id=""),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
            )

        self.assertIn("ship_lease_id", str(cm.exception))

    def test_failed_plan_review_restores_file_and_does_not_commit(self):
        git_calls = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            return ioa.ReviewGateResult(
                verdict="NEEDS-ATTENTION",
                primary_used="minimax",
                fallback_used=False,
                log_path=".code-reviews/plan.log",
            )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: (
                    git_calls.append(cmd) or ioa.CommandResult(0, "", "")
                ),
            )

        self.assertIn("plan review", str(cm.exception).lower())
        self.assertIn("status: proposed", self.strategy_path.read_text(encoding="utf-8"))
        self.assertEqual(git_calls[0][:2], ["git", "status"])

    def test_failed_diff_review_restores_original_bytes_and_does_not_commit(self):
        git_calls = []
        original = self.strategy_path.read_bytes()
        review_kinds = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            verdict = "APPROVE" if request.kind == "plan" else "NEEDS-ATTENTION"
            return ioa.ReviewGateResult(
                verdict=verdict,
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: (
                    git_calls.append(cmd) or ioa.CommandResult(0, "", "")
                ),
            )

        self.assertIn("diff review", str(cm.exception).lower())
        self.assertEqual(review_kinds, ["plan", "diff"])
        self.assertEqual(self.strategy_path.read_bytes(), original)
        self.assertEqual(git_calls[0][:2], ["git", "status"])

    def test_replayed_approval_token_refuses_after_file_content_changes(self):
        approval = self._approval()
        self.strategy_path.write_text(
            self.strategy_path.read_text(encoding="utf-8")
            + "\n## Extra Rule\n\nChanged after approval.\n",
            encoding="utf-8",
        )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=approval,
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
            )

        self.assertIn("content hash", str(cm.exception).lower())

    def test_naive_approval_timestamp_refuses_before_review(self):
        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(approved_at="2026-06-04T12:00:00"),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
            )

        self.assertIn("timezone", str(cm.exception).lower())

    def test_success_runs_plan_review_diff_review_helper_and_commit(self):
        review_kinds = []
        git_calls = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        def git_runner(cmd, cwd):
            git_calls.append(cmd)
            return ioa.CommandResult(returncode=0, stdout="", stderr="")

        result = ioa.run_full_ship(
            self.strategy_path,
            approval=self._approval(),
            review_runner=review_runner,
            approve_handler=self._approve_handler,
            git_runner=git_runner,
        )

        self.assertEqual(result.slug, "spy")
        self.assertEqual(review_kinds, ["plan", "diff"])
        self.assertIn("status: approved", self.strategy_path.read_text(encoding="utf-8"))
        self.assertEqual(git_calls[0][:2], ["git", "status"])
        self.assertEqual(git_calls[1][:2], ["git", "add"])
        self.assertEqual(git_calls[2][:2], ["git", "commit"])
        self.assertIn("--only", git_calls[2])
        self.assertIn("wiki/strategies/strategy_spy.md", git_calls[2])
        self.assertIn("Co-Shipped-By: invest-ship", result.commit_message)
        self.assertEqual(result.events[0]["event"], "ship_start")
        self.assertEqual(result.events[-1]["event"], "commit_succeeded")

    def test_dirty_unrelated_tracked_file_refuses_before_review(self):
        note = self.repo / "notes.md"
        note.write_text("clean\n", encoding="utf-8")
        subprocess.run(["git", "add", "notes.md"], cwd=str(self.repo), check=True)
        subprocess.run(
            ["git", "commit", "-m", "add note", "-q"],
            cwd=str(self.repo),
            check=True,
        )
        note.write_text("dirty\n", encoding="utf-8")

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
            )

        self.assertIn("clean-tree", str(cm.exception).lower())
        self.assertIn("notes.md", str(cm.exception))

    def test_restore_staged_failure_is_not_swallowed(self):
        review_kinds = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        def git_runner(cmd, cwd):
            if cmd[:2] == ["git", "commit"]:
                return ioa.CommandResult(returncode=1, stdout="", stderr="commit failed")
            if cmd[:3] == ["git", "restore", "--staged"]:
                return ioa.CommandResult(returncode=1, stdout="", stderr="restore failed")
            return ioa.CommandResult(returncode=0, stdout="", stderr="")

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
                git_runner=git_runner,
            )

        self.assertIn("restore --staged", str(cm.exception))

    def test_dirty_index_blocks_working_tree_restore_after_commit_failure(self):
        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        def git_runner(cmd, cwd):
            if cmd[:2] == ["git", "commit"]:
                return ioa.CommandResult(returncode=1, stdout="", stderr="commit failed")
            if cmd[:3] == ["git", "restore", "--staged"]:
                return ioa.CommandResult(returncode=1, stdout="", stderr="restore failed")
            if cmd[:3] == ["git", "reset", "HEAD"]:
                return ioa.CommandResult(returncode=0, stdout="", stderr="")
            if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
                return ioa.CommandResult(returncode=1, stdout="", stderr="")
            return ioa.CommandResult(returncode=0, stdout="", stderr="")

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
                git_runner=git_runner,
            )

        self.assertIn("index still contains", str(cm.exception).lower())
        self.assertIn("status: proposed", self.strategy_path.read_text(encoding="utf-8"))

    def test_diff_review_failure_attaches_rollback_result_and_clears_marker(self):
        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            verdict = "APPROVE" if request.kind == "plan" else "NEEDS-ATTENTION"
            return ioa.ReviewGateResult(
                verdict=verdict,
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
            )

        result = cm.exception.rollback_result
        self.assertIsNotNone(result)
        self.assertTrue(result.working_tree_restored)
        self.assertTrue(result.marker_cleared)
        self.assertFalse(Path(result.marker_path).exists())

    def test_failed_rollback_leaves_marker_and_structured_result(self):
        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        def git_runner(cmd, cwd):
            if cmd[:2] == ["git", "commit"]:
                return ioa.CommandResult(returncode=1, stdout="", stderr="commit failed")
            return ioa.CommandResult(returncode=0, stdout="", stderr="")

        with patch(
            "scripts.lib.invest_orchestrator_adapters._restore_original_or_raise",
            side_effect=ioa.OrchestratorGateError("restore failed"),
        ):
            with self.assertRaises(ioa.OrchestratorGateError) as cm:
                ioa.run_full_ship(
                    self.strategy_path,
                    approval=self._approval(),
                    review_runner=review_runner,
                    approve_handler=self._approve_handler,
                    git_runner=git_runner,
                )

        result = cm.exception.rollback_result
        self.assertIsNotNone(result)
        self.assertFalse(result.working_tree_restored)
        self.assertFalse(result.marker_cleared)
        self.assertTrue(Path(result.marker_path).exists())

    def test_incomplete_rollback_marker_refuses_before_review(self):
        marker_path = ioa._rollback_marker_path_for(self.strategy_path, self.repo)
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "strategy_path": "wiki/strategies/strategy_spy.md",
                    "original_sha256": "0" * 64,
                    "phase": "working_tree_restore",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
            )

        self.assertIn("incomplete rollback", str(cm.exception).lower())

    def test_commit_message_rejects_log_path_with_newline(self):
        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=".code-reviews/x.log\nInjected-Trailer: yes",
            )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
            )

        self.assertIn("unsafe commit field", str(cm.exception).lower())

    def test_commit_message_rejects_unsafe_trailer_line(self):
        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        def bad_approve_handler(path: Path, **kwargs) -> iss.StrategyCommitHints:
            hints = self._approve_handler(path, **kwargs)
            return iss.StrategyCommitHints(
                file=hints.file,
                slug=hints.slug,
                transition=hints.transition,
                commit_subject=hints.commit_subject,
                trailers=["Strategy-Transition: proposed -> approved\nInjected: yes"],
                timestamp_field=hints.timestamp_field,
                timestamp_value=hints.timestamp_value,
                parent_commit_sha=hints.parent_commit_sha,
            )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=review_runner,
                approve_handler=bad_approve_handler,
                git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
            )

        self.assertIn("unsafe trailer", str(cm.exception).lower())

    def test_existing_strategy_lock_refuses_concurrent_ship(self):
        lock_path = ioa._lock_path_for(self.strategy_path, self.repo)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ioa.OrchestratorGateError) as cm:
                ioa.run_full_ship(
                    self.strategy_path,
                    approval=self._approval(),
                    review_runner=lambda request: self.fail("review must not run"),
                    approve_handler=self._approve_handler,
                    git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
                )
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        self.assertIn("already locked", str(cm.exception).lower())

    def test_lock_path_is_outside_git_and_validates_containment(self):
        lock_path = ioa._lock_path_for(self.strategy_path, self.repo)
        self.assertNotIn(".git", lock_path.parts)
        self.assertEqual(lock_path.parent, self.repo / ".k2bi-orchestrator" / "locks")
        with patch(
            "scripts.lib.invest_orchestrator_adapters._slug_from_strategy_path",
            return_value="../spy",
        ):
            with self.assertRaises(ioa.OrchestratorGateError):
                ioa._lock_path_for(self.strategy_path, self.repo)

    def test_full_ship_refuses_symlink_strategy_before_review(self):
        backup = self.strategy_path.with_name("strategy_spy_real.md")
        backup.write_bytes(self.strategy_path.read_bytes())
        self.strategy_path.unlink()
        self.strategy_path.symlink_to(backup)

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
            )

        self.assertIn("regular file", str(cm.exception).lower())

    def test_full_ship_refuses_hardlinked_strategy_before_review(self):
        os.link(self.strategy_path, self.strategy_path.with_name("strategy_spy_copy.md"))

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_full_ship(
                self.strategy_path,
                approval=self._approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                git_runner=lambda cmd, cwd: ioa.CommandResult(0, "", ""),
            )

        self.assertIn("hardlink", str(cm.exception).lower())

    def test_review_verdict_requires_strict_header(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "review.log"
            log.write_text("review text says APPROVE was seen before\n", encoding="utf-8")
            self.assertEqual(ioa._extract_review_verdict(log), "UNKNOWN_VERDICT")
            log.write_text("# kimi-for-coding review -- APPROVE\n", encoding="utf-8")
            self.assertEqual(ioa._extract_review_verdict(log), "APPROVE")
            log.write_text("# kimi-for-coding review -- NEEDS-ATTENTION\n", encoding="utf-8")
            self.assertEqual(ioa._extract_review_verdict(log), "NEEDS-ATTENTION")
            self.assertEqual(ioa._extract_review_verdict(Path(td) / "missing.log"), "LOG_MISSING")

    def test_review_script_must_be_regular_executable_file(self):
        review_script = self.repo / "scripts" / "review.sh"
        review_script.unlink()
        review_script.symlink_to(self.strategy_path)

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.run_review_with_script(
                ioa.ReviewRequest(
                    kind="diff",
                    path=self.strategy_path,
                    files=["wiki/strategies/strategy_spy.md"],
                    focus="test",
                )
            )

        self.assertIn("review script", str(cm.exception).lower())

    def test_review_envelope_rejects_symlink_log_path(self):
        job_id = "2026-06-04T14-00-00Z_abc123"
        review_dir = self.repo / ".code-reviews"
        review_dir.mkdir()
        (review_dir / f"{job_id}.json").write_text(
            json.dumps({"primary_used": "minimax", "fallback_used": False}),
            encoding="utf-8",
        )
        (review_dir / "link.log").symlink_to(self.strategy_path)
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["scripts/review.sh"],
                returncode=0,
                stdout=json.dumps(
                    {"job_id": job_id, "log_path": ".code-reviews/link.log"}
                ),
                stderr="",
            )
            with patch("scripts.lib.invest_orchestrator_adapters._repo_root_for", return_value=self.repo):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.run_review_with_script(
                        ioa.ReviewRequest(
                            kind="diff",
                            path=self.strategy_path,
                            files=["wiki/strategies/strategy_spy.md"],
                            focus="test",
                        )
                    )

        self.assertIn("review log_path", str(cm.exception))

    def test_review_script_rejects_unsafe_file_list(self):
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            with patch("scripts.lib.invest_orchestrator_adapters._repo_root_for", return_value=self.repo):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.run_review_with_script(
                        ioa.ReviewRequest(
                            kind="diff",
                            path=self.strategy_path,
                            files=["wiki/strategies/strategy_spy.md,bad"],
                            focus="test",
                        )
                    )

        self.assertFalse(run.called)
        self.assertIn("review file", str(cm.exception).lower())

    def test_review_envelope_missing_job_id_refuses(self):
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["scripts/review.sh"],
                returncode=0,
                stdout='{"job_id": null, "log_path": ".code-reviews/fake.log"}',
                stderr="",
            )
            with patch("scripts.lib.invest_orchestrator_adapters._repo_root_for", return_value=self.repo):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.run_review_with_script(
                        ioa.ReviewRequest(
                            kind="diff",
                            path=self.strategy_path,
                            files=["wiki/strategies/strategy_spy.md"],
                            focus="test",
                        )
                    )

        self.assertIn("review envelope", str(cm.exception).lower())

    def test_review_script_timeout_refuses(self):
        request = ioa.ReviewRequest(
            kind="diff",
            path=self.strategy_path,
            files=["wiki/strategies/strategy_spy.md"],
            focus="test",
        )
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            run.side_effect = subprocess.TimeoutExpired(
                cmd=["scripts/review.sh"],
                timeout=300,
            )
            with patch("scripts.lib.invest_orchestrator_adapters._repo_root_for", return_value=self.repo):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.run_review_with_script(request)

        self.assertIn("timed out", str(cm.exception).lower())

    def test_review_script_rejects_unsafe_focus_before_spawn(self):
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            with patch("scripts.lib.invest_orchestrator_adapters._repo_root_for", return_value=self.repo):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.run_review_with_script(
                        ioa.ReviewRequest(
                            kind="diff",
                            path=self.strategy_path,
                            files=["wiki/strategies/strategy_spy.md"],
                            focus="safe line\nInjected: yes",
                        )
                    )

        self.assertFalse(run.called)
        self.assertIn("review focus", str(cm.exception).lower())

    def test_review_script_nonempty_stderr_refuses(self):
        job_id = "2026-06-04T14-00-00Z_abc123"
        review_dir = self.repo / ".code-reviews"
        review_dir.mkdir()
        (review_dir / f"{job_id}.log").write_text(
            "# kimi-for-coding review -- APPROVE\n",
            encoding="utf-8",
        )
        (review_dir / f"{job_id}.json").write_text(
            json.dumps(
                {
                    "scope": "diff",
                    "files": "wiki/strategies/strategy_spy.md",
                    "focus": "test",
                    "primary_used": "minimax",
                    "fallback_used": False,
                }
            ),
            encoding="utf-8",
        )
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["scripts/review.sh"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "job_id": job_id,
                        "log_path": f".code-reviews/{job_id}.log",
                    }
                ),
                stderr="unexpected warning",
            )
            with patch("scripts.lib.invest_orchestrator_adapters._repo_root_for", return_value=self.repo):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.run_review_with_script(
                        ioa.ReviewRequest(
                            kind="diff",
                            path=self.strategy_path,
                            files=["wiki/strategies/strategy_spy.md"],
                            focus="test",
                        )
                    )

        self.assertIn("stderr", str(cm.exception).lower())

    def test_repo_root_probe_fails_closed_outside_git(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "wiki" / "strategies" / "strategy_spy.md"
            path.parent.mkdir(parents=True)
            path.write_text("x", encoding="utf-8")
            with self.assertRaises(ioa.OrchestratorGateError):
                ioa._repo_root_for(path)

    def test_run_git_command_rejects_non_git_command(self):
        with self.assertRaises(ioa.OrchestratorGateError):
            ioa.run_git_command(["/bin/echo", "hello"], self.repo)

    def test_subprocess_calls_decode_as_utf8_with_replacement(self):
        with patch("scripts.lib.invest_orchestrator_adapters.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                args=["git", "status"],
                returncode=0,
                stdout="",
                stderr="",
            )
            ioa.run_git_command(["git", "status"], self.repo)

        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run.call_args.kwargs["errors"], "replace")

    def test_run_git_checked_requires_command_result(self):
        with self.assertRaises(ioa.OrchestratorGateError):
            ioa._run_git_checked(lambda cmd, cwd: None, ["git", "status"], self.repo)

    def test_repo_relative_refuses_path_outside_repo(self):
        with self.assertRaises(ioa.OrchestratorGateError):
            ioa._repo_relative(Path("/tmp/outside_strategy.md"), self.repo)
