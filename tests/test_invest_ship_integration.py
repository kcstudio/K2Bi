"""End-to-end integration tests for /invest-ship strategy subcommands.

Preemptive decision #8 (architect brief): cycle-5 ships the first test
that exercises the cycle-4 post-commit hook for real. Prior cycle-4
tests drove the hook with synthetic commit trailers; this test drives
it via the actual `handle_retire_strategy` helper -> git commit flow
so any drift between the helper and the hook surfaces here.

Coverage:

    1. `/invest-ship --retire-strategy` happy path:
        - approved strategy on disk -> helper edits frontmatter
        - stage + commit with the helper's trailers
        - cycle-4 commit-msg hook accepts the transition
        - cycle-4 pre-commit Check D accepts the pure retire diff
        - cycle-4 post-commit hook writes the retirement sentinel
        - engine's `assert_strategy_not_retired(slug)` raises

    2. `/invest-ship --approve-strategy` happy path:
        - proposed strategy on disk + prior draft commit
        - helper edits frontmatter with parent sha + approved_at
        - commit with helper trailers passes the commit-msg hook
        - pre-commit Checks A/B/D all pass (status is approved, How
          This Works non-empty, Check D doesn't apply to a HEAD-new
          file transitioning proposed->approved)

    3. `/invest-ship --reject-strategy` happy path, hook gate passes.

    4. Negative: manual `status: approved -> retired` edit without
        running the helper -- commit-msg hook rejects because the
        Strategy-Transition / Retired-Strategy / Co-Shipped-By trailers
        are absent. (Confirms the helper is the ONLY path.)

    5. Negative: `handle_retire_strategy` on a file that has body
        changes co-mingled with the retire transition -- the commit is
        rejected by pre-commit Check D even though the commit message
        looks retire-shaped.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.lib import invest_ship_strategy as iss
from tests._hook_harness import (
    commit as harness_commit,
    default_order,
    hook_repo,
    run_git,
    seed_initial_commit,
    stage,
    strategy_text,
    write_file,
)


def _sentinel_path(retired_dir: Path, slug: str) -> Path:
    digest = hashlib.sha256(slug.encode("utf-8")).hexdigest()[:16]
    return retired_dir / f".retired-{digest}"


def _build_commit_message(
    subject: str, trailers: list[str], body: str = ""
) -> str:
    lines: list[str] = [subject, ""]
    if body:
        lines.append(body)
        lines.append("")
    lines.extend(trailers)
    return "\n".join(lines) + "\n"


def _seed_bear_case_proceed(repo: Path, env: dict, ticker: str = "SPY") -> Path:
    """Seed wiki/tickers/<TICKER>.md with a fresh PROCEED bear-case +
    commit it so subsequent `handle_approve_strategy` calls pass the
    Bundle 4 cycle 2 bear-case freshness gate. Idempotent-ish: later
    calls overwrite the file but re-commit cleanly.
    """
    from datetime import date as _date
    today = _date.today().isoformat()
    rel = f"wiki/tickers/{ticker}.md"
    content = (
        "---\n"
        f"tags: [ticker, {ticker}, thesis]\n"
        f"date: {today}\n"
        "type: ticker\n"
        "origin: k2bi-extract\n"
        'up: "[[tickers/index]]"\n'
        f"symbol: {ticker}\n"
        f"thesis-last-verified: {today}\n"
        "thesis_score: 73\n"
        f"bear-last-verified: {today}\n"
        "bear_conviction: 40\n"
        "bear_top_counterpoints:\n"
        "  - c1\n  - c2\n  - c3\n"
        "bear_invalidation_scenarios:\n"
        "  - s1\n  - s2\n"
        "bear_verdict: PROCEED\n"
        "---\n\n"
        "## Phase 1: Business Model\n\ndummy\n"
    )
    path = write_file(repo, rel, content)
    result = harness_commit(repo, env, f"chore: seed bear-case {ticker}", rel)
    if result.returncode != 0:
        raise AssertionError(f"bear-case seed commit failed: {result.stderr}")
    return path


def _seed_backtest_passed(
    repo: Path, env: dict, slug: str = "spy"
) -> Path:
    """Seed raw/backtests/<date>_<slug>_backtest.md with a passed-gate
    capture + commit it so `handle_approve_strategy`'s Bundle 4 cycle 3
    backtest gate finds a fresh PROCEED. Idempotent-ish: re-runs
    overwrite the file + re-commit cleanly.
    """
    from datetime import date as _date
    import yaml

    today = _date.today().isoformat()
    rel = f"raw/backtests/{today}_{slug}_backtest.md"
    fm = {
        "tags": ["backtest", slug, "raw"],
        "date": today,
        "type": "backtest",
        "origin": "k2bi-generate",
        "up": "[[backtests/index]]",
        "strategy_slug": slug,
        "strategy_commit_sha": "abc123def456",
        "backtest": {
            "window": {"start": "2024-04-19", "end": today},
            "source": "yfinance",
            "source_version": "1.3.0",
            "symbol": "SPY",
            "reference_symbol": "SPY",
            "metrics": {
                "sharpe": 1.0,
                "sortino": 1.5,
                "max_dd_pct": -5.0,
                "win_rate_pct": 55.0,
                "avg_winner_pct": 2.0,
                "avg_loser_pct": -1.5,
                "total_return_pct": 20.0,
                "n_trades": 30,
                "avg_trade_holding_days": 5.0,
            },
            "look_ahead_check": "passed",
            "look_ahead_check_reason": "",
            "last_run": f"{today}T10:00:00+00:00",
        },
    }
    content = (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        + "---\n\nbody\n"
    )
    path = write_file(repo, rel, content)
    result = harness_commit(repo, env, f"chore: seed backtest {slug}", rel)
    if result.returncode != 0:
        raise AssertionError(f"backtest seed commit failed: {result.stderr}")
    return path


def _seed_proposed_strategy(
    repo: Path, env: dict, slug: str = "spy"
) -> Path:
    """Write a proposed strategy + commit it. Returns the file path.

    Automatically seeds a PROCEED bear-case + passed backtest for the
    strategy's default ticker because `handle_approve_strategy` now
    refuses approval without both (Bundle 4 cycle 2 + cycle 3 gates).
    """
    _seed_bear_case_proceed(repo, env, ticker=default_order()["ticker"])
    _seed_backtest_passed(repo, env, slug=slug)
    content = strategy_text(
        name=slug,
        status="proposed",
        strategy_type="hand_crafted",
        risk_envelope_pct="0.01",
        regime_filter=["risk_on"],
        order=default_order(),
    )
    rel = f"wiki/strategies/strategy_{slug}.md"
    path = write_file(repo, rel, content)
    result = harness_commit(repo, env, f"draft: {slug}", rel)
    if result.returncode != 0:
        raise AssertionError(f"draft commit failed: {result.stderr}")
    return path


def _seed_approved_strategy(
    repo: Path, env: dict, slug: str = "spy"
) -> Path:
    """Write a proposed file + commit it + run the approve helper on it
    and commit with the helper's trailers. Returns the final path.

    Intermediate commits use the real /invest-ship approve path so the
    starting state for retire tests is exactly what ships in prod.
    """
    path = _seed_proposed_strategy(repo, env, slug=slug)
    hints = iss.handle_approve_strategy(path)
    message = _build_commit_message(hints.commit_subject, hints.trailers)
    result = harness_commit(repo, env, message, str(path.relative_to(repo)))
    if result.returncode != 0:
        raise AssertionError(
            f"approve commit failed (returncode={result.returncode}):"
            f"\n{result.stderr}"
        )
    return path


def _assert_hooks_wired(repo: Path, env: dict) -> None:
    """Defensive check (MiniMax R2 F3): guarantee hook_repo() actually
    wired the real cycle-4 hooks. If core.hooksPath got overridden or
    the symlink broke, the integration tests would pass against no-op
    hooks and silently validate nothing. Every integration test opens
    with this probe so the pass/fail signal is load-bearing."""
    hp = run_git(
        repo, "config", "core.hooksPath", env=env, check=True
    ).stdout.strip()
    assert hp == ".githooks", f"core.hooksPath != .githooks (got {hp!r})"
    # Confirm the real hook files exist + contain the cycle-4 markers.
    for name, needle in (
        ("commit-msg", "Strategy-Transition"),
        ("pre-commit", "Check A -- status-in-enum"),
        ("post-commit", "retire sentinel"),
    ):
        hook_file = repo / ".githooks" / name
        # Follow symlink if the harness used one.
        content = hook_file.read_text(encoding="utf-8", errors="ignore")
        assert needle in content, (
            f".githooks/{name} missing cycle-4 marker {needle!r}"
        )


class RetireHappyPathLandsSentinelAndBlocksEngine(unittest.TestCase):
    """Preemptive decision #8: the first test that exercises the cycle-4
    post-commit hook via the real cycle-5 helper path."""

    def test_retire_lands_sentinel_and_engine_blocks(self):
        with hook_repo() as (repo, env):
            _assert_hooks_wired(repo, env)
            seed_initial_commit(repo, env)
            path = _seed_approved_strategy(repo, env, slug="spy")

            hints = iss.handle_retire_strategy(
                path, reason="obsolete after regime flip"
            )
            message = _build_commit_message(
                hints.commit_subject, hints.trailers
            )
            result = harness_commit(
                repo, env, message, str(path.relative_to(repo))
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commit_sha = run_git(
                repo, "rev-parse", "HEAD", env=env, check=True
            ).stdout.strip()

            # 1. Sentinel wrote at the expected path.
            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            sentinel = _sentinel_path(retired_dir, "spy")
            self.assertTrue(
                sentinel.exists(),
                f"sentinel not found at {sentinel}; dir contents: "
                f"{list(retired_dir.iterdir())}",
            )
            record = json.loads(sentinel.read_text(encoding="utf-8"))
            self.assertEqual(record["slug"], "spy")
            self.assertEqual(record["commit_sha"], commit_sha)
            self.assertIn("obsolete", record["reason"])

            # 2. Engine's assert_strategy_not_retired raises when
            #    pointed at the same base dir the hook wrote to.
            from execution.risk.kill_switch import (
                StrategyRetiredError,
                assert_strategy_not_retired,
            )

            with self.assertRaises(StrategyRetiredError) as cm:
                assert_strategy_not_retired("spy", base_dir=retired_dir)
            self.assertEqual(cm.exception.strategy_slug, "spy")
            self.assertIsNotNone(cm.exception.record)
            self.assertEqual(cm.exception.record["commit_sha"], commit_sha)


class ApproveHappyPath(unittest.TestCase):
    def test_approve_commit_lands(self):
        with hook_repo() as (repo, env):
            _assert_hooks_wired(repo, env)
            seed_initial_commit(repo, env)
            _seed_proposed_strategy(repo, env, slug="qqq")

            # MiniMax R2 F2: independently derive the expected parent
            # sha BEFORE the helper runs so the test proves the contract
            # (parent sha = HEAD at the moment of approval, per spec
            # Q1), not just self-consistency of hints.parent_commit_sha
            # vs. the frontmatter it wrote.
            expected_parent = run_git(
                repo, "rev-parse", "--short", "HEAD", env=env, check=True
            ).stdout.strip()

            path = repo / "wiki" / "strategies" / "strategy_qqq.md"
            hints = iss.handle_approve_strategy(path)
            self.assertEqual(hints.slug, "qqq")
            self.assertIn(
                "Approved-Strategy: strategy_qqq", hints.trailers
            )
            # Independent-source assertion of the parent-sha contract.
            self.assertEqual(
                hints.parent_commit_sha,
                expected_parent,
                "approved_commit_sha must be the PARENT sha (HEAD at "
                "approval time), not the approval commit's own sha",
            )
            message = _build_commit_message(
                hints.commit_subject, hints.trailers
            )
            result = harness_commit(
                repo, env, message, str(path.relative_to(repo))
            )
            self.assertEqual(
                result.returncode,
                0,
                f"approve commit should land cleanly; stderr:\n{result.stderr}",
            )

            # After approval, the file is at status=approved.
            rewritten = path.read_text(encoding="utf-8")
            self.assertIn("status: approved", rewritten)
            self.assertIn("approved_at:", rewritten)
            # MiniMax R3 F3: re-parse the frontmatter to assert the
            # full approval-side shape, not just surface string
            # presence. Catches regressions where a required field
            # drops silently (null / missing / wrong type).
            import yaml as _yaml

            fm_after = _yaml.safe_load(rewritten.split("---", 2)[1])
            self.assertEqual(
                str(fm_after["approved_commit_sha"]), expected_parent
            )
            self.assertEqual(fm_after["status"], "approved")
            # approved_at must be a parseable ISO-8601 datetime.
            from datetime import datetime as _dt

            ts_raw = str(fm_after["approved_at"])
            parsed = _dt.fromisoformat(ts_raw.replace("Z", "+00:00"))
            self.assertIsNotNone(parsed.tzinfo)
            self.assertEqual(fm_after["name"], "qqq")
            self.assertIsInstance(fm_after["regime_filter"], list)
            self.assertGreater(len(fm_after["regime_filter"]), 0)
            # risk_envelope_pct is a float in the original frontmatter
            # (spec §2.1). After round-trip it should still be numeric.
            self.assertIsInstance(
                fm_after["risk_envelope_pct"], (int, float)
            )
            # Order block preserved with all six required keys.
            self.assertIsInstance(fm_after["order"], dict)
            for k in (
                "ticker",
                "side",
                "qty",
                "limit_price",
                "stop_loss",
                "time_in_force",
            ):
                self.assertIn(k, fm_after["order"])
            # Sanity-check that the approval-commit SHA is different
            # from the parent SHA (otherwise the "parent, not self"
            # invariant is trivially satisfied by sha collision).
            approval_sha = run_git(
                repo, "rev-parse", "--short", "HEAD", env=env, check=True
            ).stdout.strip()
            self.assertNotEqual(approval_sha, expected_parent)


class RejectHappyPath(unittest.TestCase):
    def test_reject_commit_lands(self):
        with hook_repo() as (repo, env):
            _assert_hooks_wired(repo, env)
            seed_initial_commit(repo, env)
            _seed_proposed_strategy(repo, env, slug="failed")

            path = repo / "wiki" / "strategies" / "strategy_failed.md"
            hints = iss.handle_reject_strategy(path, "backtest negative")
            message = _build_commit_message(
                hints.commit_subject, hints.trailers
            )
            result = harness_commit(
                repo, env, message, str(path.relative_to(repo))
            )
            self.assertEqual(result.returncode, 0, result.stderr)

            retired_dir = Path(env["K2BI_RETIRED_DIR"])
            # Reject is NOT retire -- no sentinel should land.
            sentinels = [
                p
                for p in retired_dir.iterdir()
                if p.name.startswith(".retired-")
            ]
            self.assertEqual(sentinels, [])


class ManualRetireWithoutTrailersBlocked(unittest.TestCase):
    """Adversarial path: if someone hand-edits status: approved -> retired
    without running /invest-ship --retire-strategy, the commit-msg hook
    rejects (trailers missing). This is the primary enforcement we rely
    on to make the helper the only retire path."""

    def test_manual_retire_without_trailers_rejected(self):
        with hook_repo() as (repo, env):
            _assert_hooks_wired(repo, env)
            seed_initial_commit(repo, env)
            path = _seed_approved_strategy(repo, env, slug="spy")

            # Manually flip status WITHOUT adding trailers.
            text = path.read_text(encoding="utf-8")
            text = text.replace("status: approved", "status: retired")
            # Must also add retired_at + retired_reason or Check D would
            # reject on frontmatter-shape grounds; we want to isolate
            # commit-msg trailer enforcement. Use a runtime timestamp
            # instead of a hardcoded date (R4-minimax F2) so the test
            # stays valid beyond 2026-04-19.
            retired_at = (
                datetime.now(timezone.utc)
                .isoformat(timespec="seconds")
            )
            text = text.replace(
                "---\n\n## How This Works",
                (
                    f"retired_at: '{retired_at}'\n"
                    "retired_reason: manual\n"
                    "---\n\n## How This Works"
                ),
            )
            path.write_text(text, encoding="utf-8")
            result = harness_commit(
                repo, env, "chore: manual retire", str(path.relative_to(repo))
            )
            self.assertNotEqual(
                result.returncode,
                0,
                "manual retire without trailers MUST be rejected by "
                "commit-msg hook; repo is in an inconsistent state",
            )
            combined = (result.stderr or "") + (result.stdout or "")
            self.assertIn(
                "Strategy-Transition",
                combined,
                f"expected trailer-missing error; combined output:\n{combined}",
            )


class ApproveBodyBreakingCheckBRejected(unittest.TestCase):
    """MiniMax R3 F1 coverage: adversarial approve path.

    Check D does NOT apply to proposed -> approved (HEAD is proposed,
    not approved), so body edits during approval are not byte-locked.
    But Check B (`## How This Works` non-empty) applies to both
    proposed AND approved staged state. This test verifies that if an
    operator blanks out the How This Works body around the approve
    commit, Check B rejects the commit before the helper's
    frontmatter edits can land as an approved-state file.
    """

    def test_approve_with_empty_how_this_works_body_rejected(self):
        with hook_repo() as (repo, env):
            _assert_hooks_wired(repo, env)
            seed_initial_commit(repo, env)
            _seed_proposed_strategy(repo, env, slug="broken")

            path = repo / "wiki" / "strategies" / "strategy_broken.md"
            hints = iss.handle_approve_strategy(path)
            # Now wipe the How This Works body -- simulates a
            # co-mingled body edit made during the approve flow.
            text = path.read_text(encoding="utf-8")
            # Replace the body content between the "## How This Works"
            # heading and EOF with whitespace.
            idx = text.rfind("## How This Works")
            self.assertGreater(idx, 0)
            corrupted = text[: idx + len("## How This Works")] + "\n\n   \n"
            path.write_text(corrupted, encoding="utf-8")

            message = _build_commit_message(
                hints.commit_subject, hints.trailers
            )
            result = harness_commit(
                repo, env, message, str(path.relative_to(repo))
            )
            self.assertNotEqual(
                result.returncode,
                0,
                "approve with empty How This Works body MUST be "
                "rejected by pre-commit Check B",
            )
            combined = (result.stderr or "") + (result.stdout or "")
            self.assertIn(
                "How This Works",
                combined,
                f"expected Check B rejection; combined output:\n{combined}",
            )


class RetireCoMingledWithBodyEditRejected(unittest.TestCase):
    """Adversarial path: if someone tries to retire AND change the body
    in the same commit (e.g. update `## How This Works` mid-retire),
    cycle-4 Check D rejects the commit. The helper itself never
    produces this because `_edit_frontmatter` preserves body bytes --
    this test drives the manual case."""

    def test_body_change_co_mingled_with_retire_rejected(self):
        with hook_repo() as (repo, env):
            _assert_hooks_wired(repo, env)
            seed_initial_commit(repo, env)
            path = _seed_approved_strategy(repo, env, slug="spy")

            # Helper-correct frontmatter edits:
            hints = iss.handle_retire_strategy(path, reason="shift regime")
            # ...then co-mingle a body edit:
            text = path.read_text(encoding="utf-8")
            text = text.replace(
                "Plain-English explanation body.",
                "MODIFIED body at retire time.",
            )
            path.write_text(text, encoding="utf-8")

            message = _build_commit_message(
                hints.commit_subject, hints.trailers
            )
            result = harness_commit(
                repo, env, message, str(path.relative_to(repo))
            )
            self.assertNotEqual(
                result.returncode,
                0,
                "co-mingled body edit MUST be rejected by Check D",
            )
            combined = (result.stderr or "") + (result.stdout or "")
            self.assertIn(
                "Check D",
                combined,
                f"expected Check D rejection; combined output:\n{combined}",
            )


if __name__ == "__main__":
    unittest.main()
