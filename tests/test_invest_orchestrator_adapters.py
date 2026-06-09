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
import datetime as _dt
from datetime import date
from pathlib import Path

from scripts.lib import invest_orchestrator_adapters as ioa
from scripts.lib import invest_ship_strategy as iss
from scripts.lib import invest_thesis as it
from scripts.lib import strategy_frontmatter as sf
from tests.test_invest_ship_strategy import (
    _make_tmp_repo,
    _write_config_yaml,
    _write_limits_proposal,
    _write_strategy,
)
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


class ApplyApprovedLimitsAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo, self.parent_sha = _make_tmp_repo()
        self.config_path = _write_config_yaml(self.repo)
        self.proposal_path = _write_limits_proposal(self.repo)
        self.now_utc = _dt.datetime(2026, 6, 8, 8, 0, 30, tzinfo=_dt.timezone.utc)
        self.review_files_seen: list[list[str]] = []

    def tearDown(self) -> None:
        subprocess.run(["rm", "-rf", str(self.repo)], check=False)

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

    def _approve_handler(self, path: Path, **kwargs) -> iss.LimitsCommitHints:
        kwargs["parent_sha"] = self.parent_sha
        return iss.handle_approve_limits(path, **kwargs)

    def _approve_review(self, request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
        self.review_files_seen.append(list(request.files))
        return ioa.ReviewGateResult(
            verdict="APPROVE",
            primary_used="minimax",
            fallback_used=False,
            log_path=f".code-reviews/{request.kind}.log",
        )

    def _fake_success_git(self, calls: list[list[str]] | None = None) -> ioa.GitRunner:
        expected_cached = (
            "execution/validators/config.yaml\n"
            "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md\n"
        )

        def git_runner(cmd, cwd):
            if calls is not None:
                calls.append(cmd)
            if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
                return ioa.CommandResult(0, expected_cached, "")
            return ioa.CommandResult(0, "", "")

        return git_runner

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
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("proposal hash", str(cm.exception).lower())

    def test_limits_approval_token_binds_exact_config_bytes(self):
        approval = self._limits_approval()
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8") + "# drift\n",
            encoding="utf-8",
        )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=approval,
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("config hash", str(cm.exception).lower())

    def test_limits_approval_rejects_malformed_hash_and_token_fields(self):
        cases = [
            ("", "final approval"),
            ("APPROVE_LIMITS:widen-size:ABC", "final approval"),
            (
                self._limits_approval().final_approval_token.replace(
                    "APPROVE_LIMITS:widen-size", "APPROVE_LIMITS:other"
                ),
                "final approval",
            ),
        ]
        for token, expected in cases:
            with self.subTest(token=token):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.apply_approved_limits(
                        self.proposal_path,
                        approval=self._limits_approval(final_approval_token=token),
                        review_runner=lambda request: self.fail("review must not run"),
                        approve_handler=self._approve_handler,
                        config_path=self.config_path,
                        now_utc=self.now_utc,
                    )
                self.assertIn(expected, str(cm.exception).lower())

    def test_limits_approval_rejects_naive_non_utc_and_stale_timestamps(self):
        cases = [
            ("2026-06-08T08:00:00", "timezone"),
            ("2026-06-08T16:00:00.000000+08:00", "utc"),
            ("2026-06-08T07:00:00.000000+00:00", "clock"),
            ("2026-06-08T08:10:00.000000+00:00", "clock"),
        ]
        for approved_at, expected in cases:
            with self.subTest(approved_at=approved_at):
                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.apply_approved_limits(
                        self.proposal_path,
                        approval=self._limits_approval(approved_at=approved_at),
                        review_runner=lambda request: self.fail("review must not run"),
                        approve_handler=self._approve_handler,
                        config_path=self.config_path,
                        now_utc=self.now_utc,
                    )
                self.assertIn(expected, str(cm.exception).lower())

    def test_success_reviews_applies_stages_both_files_and_commits(self):
        review_kinds = []
        review_files = []
        git_calls: list[list[str]] = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            review_files.append(list(request.files))
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        result = ioa.apply_approved_limits(
            self.proposal_path,
            approval=self._limits_approval(),
            review_runner=review_runner,
            approve_handler=self._approve_handler,
            git_runner=self._fake_success_git(git_calls),
            config_path=self.config_path,
            now_utc=self.now_utc,
        )

        proposal_rel = "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md"
        self.assertEqual(result.slug, "widen-size")
        self.assertEqual(review_kinds, ["plan", "diff"])
        self.assertEqual(review_files, [[proposal_rel], [proposal_rel, "execution/validators/config.yaml"]])
        self.assertIn("status: approved", self.proposal_path.read_text(encoding="utf-8"))
        self.assertIn("max_trade_risk_pct: 0.02", self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(git_calls[0][:2], ["git", "status"])
        self.assertEqual(git_calls[1][:4], ["git", "diff", "--cached", "--quiet"])
        self.assertEqual(git_calls[2][:3], ["git", "diff", "--quiet"])
        self.assertEqual(git_calls[3][:2], ["git", "add"])
        self.assertIn(proposal_rel, git_calls[3])
        self.assertIn("execution/validators/config.yaml", git_calls[3])
        self.assertEqual(git_calls[4][:3], ["git", "diff", "--cached"])
        self.assertEqual(git_calls[5][:2], ["git", "commit"])
        self.assertIn("--only", git_calls[5])
        self.assertIn("Approved-Limits: widen-size", result.commit_message)
        self.assertIn("Approval-Captured-At: 2026-06-08T08:00:00.000000+00:00", result.commit_message)

    def test_untracked_strategy_file_does_not_block_limits_apply(self):
        untracked = self.repo / "wiki" / "strategies" / "strategy_cdns.md"
        untracked.parent.mkdir(parents=True, exist_ok=True)
        untracked.write_text("draft\n", encoding="utf-8")

        result = ioa.apply_approved_limits(
            self.proposal_path,
            approval=self._limits_approval(),
            review_runner=self._approve_review,
            approve_handler=self._approve_handler,
            git_runner=self._fake_success_git(),
            config_path=self.config_path,
            now_utc=self.now_utc,
        )

        self.assertEqual(result.slug, "widen-size")
        self.assertEqual(
            self.review_files_seen,
            [
                ["review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md"],
                [
                    "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
                    "execution/validators/config.yaml",
                ],
            ],
        )

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
                now_utc=self.now_utc,
            )

        self.assertIn("clean-tree", str(cm.exception).lower())
        self.assertIn("notes.md", str(cm.exception))

    def test_preexisting_staged_target_change_refuses_before_review(self):
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        subprocess.run(
            [
                "git",
                "add",
                "execution/validators/config.yaml",
                "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
            ],
            cwd=str(self.repo),
            check=True,
        )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        msg = str(cm.exception)
        self.assertIn("staged changes", msg)
        self.assertIn("execution/validators/config.yaml", msg)
        self.assertIn(
            "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
            msg,
        )
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_preexisting_unstaged_target_change_refuses_before_review(self):
        # A tracked target file carrying a pre-existing UNSTAGED working-tree edit
        # must be refused before the handler mutates anything; otherwise the stray
        # edit would be folded into the committed validator change by the later
        # `git add`. Symmetric companion to the staged-target preflight gate.
        subprocess.run(
            ["git", "add", "execution/validators/config.yaml"],
            cwd=str(self.repo),
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "seed config", "-q"],
            cwd=str(self.repo),
            check=True,
        )
        # Stray, unrelated working-tree edit to the now-tracked target.
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8") + "# stray unstaged edit\n",
            encoding="utf-8",
        )
        dirty_config = self.config_path.read_bytes()
        # Build the approval AFTER the edit so the config sha binds the on-disk
        # (dirty) bytes and the token gate passes, isolating the preflight gate.
        approval = self._limits_approval()

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=approval,
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        msg = str(cm.exception)
        self.assertIn("unstaged", msg)
        self.assertIn("execution/validators/config.yaml", msg)
        self.assertEqual(self.config_path.read_bytes(), dirty_config)

    def test_status_rename_and_delete_refuse(self):
        allowed = {
            "execution/validators/config.yaml",
            "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
        }
        for raw in [
            "D  execution/validators/config.yaml",
            " D execution/validators/config.yaml",
            "R  execution/validators/config.yaml -> execution/validators/config.yaml.bak",
            "R  execution/validators/config.yaml -> review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md",
        ]:
            with self.subTest(raw=raw):
                self.assertFalse(ioa._status_line_is_allowed_target_change(raw, allowed))

    def test_change5_needs_attention_diff_review_is_advisory_and_commits(self):
        # Change 5 (2026-06-09): the limits-apply diff review is ADVISORY. The diff
        # is deterministic and already byte-verified before commit, so a
        # NEEDS-ATTENTION diff verdict must be recorded for audit but must NOT block
        # or roll back. The apply still commits.
        review_kinds: list[str] = []
        git_calls: list[list[str]] = []

        def review_runner(request):
            review_kinds.append(request.kind)
            verdict = "APPROVE" if request.kind == "plan" else "NEEDS-ATTENTION"
            return ioa.ReviewGateResult(
                verdict=verdict,
                primary_used="minimax",
                fallback_used=False,
                log_path=f".code-reviews/{request.kind}.log",
            )

        result = ioa.apply_approved_limits(
            self.proposal_path,
            approval=self._limits_approval(),
            review_runner=review_runner,
            approve_handler=self._approve_handler,
            git_runner=self._fake_success_git(git_calls),
            config_path=self.config_path,
            now_utc=self.now_utc,
        )

        # The change LANDED: both reviews ran, the diff verdict was NEEDS-ATTENTION,
        # yet the apply committed and the files carry the approved change.
        self.assertEqual(review_kinds, ["plan", "diff"])
        self.assertEqual(result.diff_review.verdict, "NEEDS-ATTENTION")
        self.assertIn("status: approved", self.proposal_path.read_text(encoding="utf-8"))
        self.assertIn("max_trade_risk_pct: 0.02", self.config_path.read_text(encoding="utf-8"))
        self.assertTrue(any(cmd[:2] == ["git", "commit"] for cmd in git_calls))
        # The advisory verdict is recorded in the event log for audit.
        advisory = [e for e in result.events if e.get("event") == "diff_review_advisory"]
        self.assertEqual(len(advisory), 1)
        self.assertEqual(advisory[0]["verdict"], "NEEDS-ATTENTION")
        self.assertEqual(advisory[0]["log_path"], ".code-reviews/diff.log")

    def test_change5_needs_attention_plan_review_still_blocks_and_rolls_back(self):
        # Part (a) of the Change 5 proof: the PLAN gate stays intact. A
        # NEEDS-ATTENTION plan verdict refuses before the handler mutates anything,
        # the diff review never runs, and nothing commits.
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        review_kinds: list[str] = []
        git_calls: list[list[str]] = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            return ioa.ReviewGateResult(
                verdict="NEEDS-ATTENTION",
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
                git_runner=self._fake_success_git(git_calls),
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("plan review", str(cm.exception).lower())
        self.assertEqual(review_kinds, ["plan"])  # plan refused before the diff review
        self.assertFalse(any(cmd[:2] == ["git", "commit"] for cmd in git_calls))
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_change5_post_handler_byte_mismatch_still_blocks_despite_advisory_diff(self):
        # Part (b) of the Change 5 proof: the deterministic byte-check is the real
        # mechanical gate. Even with the diff review downgraded to advisory, a
        # handler that mutates config bytes off the expected patch must raise and
        # roll back BEFORE the diff review even runs.
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        review_kinds: list[str] = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            # A NEEDS-ATTENTION diff would be advisory if it ran -- it must not.
            verdict = "APPROVE" if request.kind == "plan" else "NEEDS-ATTENTION"
            return ioa.ReviewGateResult(verdict, "minimax", False, f".code-reviews/{request.kind}.log")

        def bad_handler(path: Path, **kwargs) -> iss.LimitsCommitHints:
            hints = self._approve_handler(path, **kwargs)
            self.config_path.write_text(
                self.config_path.read_text(encoding="utf-8") + "# unexpected\n",
                encoding="utf-8",
            )
            return hints

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=review_runner,
                approve_handler=bad_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("config bytes", str(cm.exception).lower())
        self.assertEqual(review_kinds, ["plan"])  # byte gate raised before the diff review
        self.assertIsNotNone(cm.exception.rollback_result)
        self.assertTrue(cm.exception.rollback_result.working_tree_restored)
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_commit_failure_restores_both_files_and_unstages(self):
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        git_calls: list[list[str]] = []
        expected_cached = (
            "execution/validators/config.yaml\n"
            "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md\n"
        )

        def git_runner(cmd, cwd):
            git_calls.append(cmd)
            if cmd[:2] == ["git", "commit"]:
                return ioa.CommandResult(1, "", "commit failed")
            if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
                return ioa.CommandResult(0, expected_cached, "")
            if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
                return ioa.CommandResult(0, "", "")
            return ioa.CommandResult(0, "", "")

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=self._approve_review,
                approve_handler=self._approve_handler,
                git_runner=git_runner,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("commit failed", str(cm.exception))
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)
        self.assertTrue(any(cmd[:3] == ["git", "restore", "--staged"] for cmd in git_calls))

    def test_failed_rollback_leaves_v2_marker_and_structured_result(self):
        def git_runner(cmd, cwd):
            if cmd[:2] == ["git", "commit"]:
                return ioa.CommandResult(1, "", "commit failed")
            if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
                return ioa.CommandResult(
                    0,
                    "execution/validators/config.yaml\n"
                    "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md\n",
                    "",
                )
            return ioa.CommandResult(0, "", "")

        with patch(
            "scripts.lib.invest_orchestrator_adapters._restore_original_or_raise",
            side_effect=ioa.OrchestratorGateError("restore failed"),
        ):
            with self.assertRaises(ioa.OrchestratorGateError) as cm:
                ioa.apply_approved_limits(
                    self.proposal_path,
                    approval=self._limits_approval(),
                    review_runner=self._approve_review,
                    approve_handler=self._approve_handler,
                    git_runner=git_runner,
                    config_path=self.config_path,
                    now_utc=self.now_utc,
                )

        result = cm.exception.rollback_result
        self.assertIsNotNone(result)
        self.assertEqual(result.adapter_kind, "limits")
        self.assertFalse(result.working_tree_restored)
        marker_path = Path(result.marker_path)
        self.assertTrue(marker_path.exists())
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        self.assertEqual(marker["version"], 2)
        self.assertEqual(marker["adapter_kind"], "limits")
        self.assertIn("execution/validators/config.yaml", marker["paths"])

    def test_incomplete_v2_rollback_marker_refuses_before_review(self):
        marker_path = self.repo / ".k2bi-orchestrator" / "rollback" / "limits_widen-size.json"
        marker_path.parent.mkdir(parents=True, exist_ok=True)
        marker_path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "adapter_kind": "limits",
                    "paths": {
                        "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md": "0" * 64,
                        "execution/validators/config.yaml": "1" * 64,
                    },
                    "phase": "working_tree_restore",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        msg = str(cm.exception)
        self.assertIn("limits", msg)
        self.assertIn("working_tree_restore", msg)
        self.assertIn("execution/validators/config.yaml", msg)
        self.assertIn("review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md", msg)

    def test_commit_message_rejects_review_log_path_with_newline(self):
        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            return ioa.ReviewGateResult(
                verdict="APPROVE",
                primary_used="minimax",
                fallback_used=False,
                log_path=".code-reviews/x.log\nInjected-Trailer: yes",
            )

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=review_runner,
                approve_handler=self._approve_handler,
                git_runner=self._fake_success_git(),
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("unsafe commit field", str(cm.exception).lower())
        self.assertIn("status: proposed", self.proposal_path.read_text(encoding="utf-8"))

    def test_rejects_plan_review_fallback_or_wrong_primary(self):
        # The PLAN review stays blocking (Change 5 only downgraded the diff review),
        # so a fallback-used or wrong-primary plan verdict must still refuse before
        # any mutation. The diff review's primary/fallback is no longer enforced.
        cases = [
            ("minimax", True, "fallback"),
            ("codex", False, "primary_used"),
        ]
        for primary_used, fallback_used, expected in cases:
            with self.subTest(primary_used=primary_used, fallback_used=fallback_used):
                self.tearDown()
                self.setUp()

                def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
                    self.assertEqual(request.kind, "plan")  # diff review must never run
                    return ioa.ReviewGateResult("APPROVE", primary_used, fallback_used, ".code-reviews/plan.log")

                with self.assertRaises(ioa.OrchestratorGateError) as cm:
                    ioa.apply_approved_limits(
                        self.proposal_path,
                        approval=self._limits_approval(),
                        review_runner=review_runner,
                        approve_handler=self._approve_handler,
                        config_path=self.config_path,
                        now_utc=self.now_utc,
                    )
                self.assertIn(expected, str(cm.exception))
                self.assertIn("status: proposed", self.proposal_path.read_text(encoding="utf-8"))

    def test_handler_receives_approval_timestamp(self):
        approval = self._limits_approval(approved_at="2026-06-08T08:00:34.000000+00:00")
        ioa.apply_approved_limits(
            self.proposal_path,
            approval=approval,
            review_runner=self._approve_review,
            approve_handler=iss.handle_approve_limits,
            git_runner=self._fake_success_git(),
            config_path=self.config_path,
            now_utc=_dt.datetime(2026, 6, 8, 8, 0, 35, tzinfo=_dt.timezone.utc),
        )

        fm = sf.parse(self.proposal_path.read_bytes())
        self.assertEqual(
            str(fm["approved_at"]),
            "2026-06-08T08:00:34.000000+00:00",
        )

    def test_non_canonical_config_path_refuses_before_review(self):
        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=lambda request: self.fail("review must not run"),
                approve_handler=self._approve_handler,
                config_path=self.repo / "execution" / "validators" / ".." / "validators" / "config.yaml",
                now_utc=self.now_utc,
            )

        self.assertIn("config_path must be execution/validators/config.yaml", str(cm.exception))

    def test_partial_staging_refuses_before_commit_and_rolls_back(self):
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        git_calls: list[list[str]] = []

        def git_runner(cmd, cwd):
            git_calls.append(cmd)
            if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
                return ioa.CommandResult(0, "execution/validators/config.yaml\n", "")
            return ioa.CommandResult(0, "", "")

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=self._approve_review,
                approve_handler=self._approve_handler,
                git_runner=git_runner,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("cached diff", str(cm.exception).lower())
        self.assertFalse(any(cmd[:2] == ["git", "commit"] for cmd in git_calls))
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_post_handler_unexpected_config_mutation_refuses_before_diff_review(self):
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        review_kinds = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            return ioa.ReviewGateResult("APPROVE", "minimax", False, f".code-reviews/{request.kind}.log")

        def bad_handler(path: Path, **kwargs) -> iss.LimitsCommitHints:
            hints = self._approve_handler(path, **kwargs)
            self.config_path.write_text(
                self.config_path.read_text(encoding="utf-8") + "# unexpected\n",
                encoding="utf-8",
            )
            return hints

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=review_runner,
                approve_handler=bad_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("config bytes", str(cm.exception).lower())
        self.assertEqual(review_kinds, ["plan"])
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_post_handler_unexpected_proposal_mutation_refuses_before_diff_review(self):
        original_proposal = self.proposal_path.read_bytes()
        original_config = self.config_path.read_bytes()
        review_kinds = []

        def review_runner(request: ioa.ReviewRequest) -> ioa.ReviewGateResult:
            review_kinds.append(request.kind)
            return ioa.ReviewGateResult("APPROVE", "minimax", False, f".code-reviews/{request.kind}.log")

        def bad_handler(path: Path, **kwargs) -> iss.LimitsCommitHints:
            hints = self._approve_handler(path, **kwargs)
            path.write_text(
                path.read_text(encoding="utf-8") + "\n# unexpected\n",
                encoding="utf-8",
            )
            return hints

        with self.assertRaises(ioa.OrchestratorGateError) as cm:
            ioa.apply_approved_limits(
                self.proposal_path,
                approval=self._limits_approval(),
                review_runner=review_runner,
                approve_handler=bad_handler,
                config_path=self.config_path,
                now_utc=self.now_utc,
            )

        self.assertIn("proposal bytes", str(cm.exception).lower())
        self.assertEqual(review_kinds, ["plan"])
        self.assertEqual(self.proposal_path.read_bytes(), original_proposal)
        self.assertEqual(self.config_path.read_bytes(), original_config)

    def test_rollback_marker_cleanup_failure_preserves_result(self):
        def git_runner(cmd, cwd):
            if cmd[:2] == ["git", "commit"]:
                return ioa.CommandResult(1, "", "commit failed")
            if cmd[:3] == ["git", "diff", "--cached"] and "--name-only" in cmd:
                return ioa.CommandResult(
                    0,
                    "execution/validators/config.yaml\n"
                    "review/strategy-approvals/2026-04-19_limits-proposal_widen-size.md\n",
                    "",
                )
            if cmd[:4] == ["git", "diff", "--cached", "--quiet"]:
                return ioa.CommandResult(0, "", "")
            return ioa.CommandResult(0, "", "")

        with patch("pathlib.Path.unlink", side_effect=OSError("unlink failed")):
            with self.assertRaises(ioa.OrchestratorGateError) as cm:
                ioa.apply_approved_limits(
                    self.proposal_path,
                    approval=self._limits_approval(),
                    review_runner=self._approve_review,
                    approve_handler=self._approve_handler,
                    git_runner=git_runner,
                    config_path=self.config_path,
                    now_utc=self.now_utc,
                )

        result = cm.exception.rollback_result
        self.assertIsNotNone(result)
        self.assertTrue(result.index_restored)
        self.assertTrue(result.working_tree_restored)
        self.assertFalse(result.marker_cleared)
        self.assertIn("rollback marker cleanup failed", str(cm.exception).lower())


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


class ReviewFocusCalibrationTests(unittest.TestCase):
    """Lock the single-operator-paper calibration into all FOUR review focuses.

    There are exactly four orchestrator review focuses: limits-plan, limits-diff,
    strategy-plan, strategy-diff. Each blocks legitimate, minimal proposals when
    its focus prompt lets the reviewer treat a single-operator PAPER proposal as
    production infrastructure -- the diff focuses additionally asked the reviewer
    to verify adapter-provided guarantees (token/config-sha binding, staging)
    that are invisible in the diff, so it reported them missing. These asserts
    are a cheap regression so the calibration (deployment context + an explicit
    out-of-scope boundary) cannot silently revert. They do NOT assert the gate is
    weakened: the genuine defect checks must stay present.
    """

    DEPLOYMENT_CONTEXT_MARKER = "SINGLE-OPERATOR PAPER-TRADING"
    OUT_OF_SCOPE_MARKER = "OUT OF SCOPE"

    def test_limits_plan_review_focus_carries_context_and_scope_boundary(self):
        focus = ioa._make_limits_plan_review_request(
            Path("review/strategy-approvals/limits_x.md"),
            proposal_rel="review/strategy-approvals/limits_x.md",
            required_primary="minimax",
        ).focus
        self.assertIn(self.DEPLOYMENT_CONTEXT_MARKER, focus)
        self.assertIn(self.OUT_OF_SCOPE_MARKER, focus)
        # Calibration keeps the real defect checks, does not weaken the gate.
        self.assertIn("WEAKENS a validator", focus)
        self.assertLessEqual(len(focus), ioa.MAX_REVIEW_FOCUS_LEN)

    def test_strategy_plan_review_focus_carries_context_and_scope_boundary(self):
        focus = ioa._strategy_plan_review_focus()
        self.assertIn(self.DEPLOYMENT_CONTEXT_MARKER, focus)
        self.assertIn(self.OUT_OF_SCOPE_MARKER, focus)
        # Existing strategy-quality checks must survive the calibration.
        self.assertIn("look-ahead", focus)
        self.assertLessEqual(len(focus), ioa.MAX_REVIEW_FOCUS_LEN)

    def test_limits_diff_review_focus_carries_context_and_scope_boundary(self):
        focus = ioa._make_limits_diff_review_request(
            Path("review/strategy-approvals/limits_x.md"),
            proposal_rel="review/strategy-approvals/limits_x.md",
            config_rel="execution/validators/config.yaml",
            required_primary="minimax",
        ).focus
        self.assertIn(self.DEPLOYMENT_CONTEXT_MARKER, focus)
        self.assertIn(self.OUT_OF_SCOPE_MARKER, focus)
        # Calibration keeps the real in-scope diff checks, does not weaken the gate.
        self.assertIn("WEAKENS a validator", focus)
        self.assertIn("commit trailers", focus)
        self.assertLessEqual(len(focus), ioa.MAX_REVIEW_FOCUS_LEN)

    def test_strategy_diff_review_focus_carries_context_and_scope_boundary(self):
        focus = ioa._strategy_diff_review_focus()
        self.assertIn(self.DEPLOYMENT_CONTEXT_MARKER, focus)
        self.assertIn(self.OUT_OF_SCOPE_MARKER, focus)
        # Gate-bypass / trailer / weakening checks must survive the calibration.
        self.assertIn("safety-gate bypass", focus)
        self.assertIn("commit trailer", focus)
        self.assertIn("weakens a validator", focus)
        self.assertLessEqual(len(focus), ioa.MAX_REVIEW_FOCUS_LEN)
