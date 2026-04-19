"""Tests for scan_bear_case_for_ticker (Bundle 4 cycle 2 Part B).

Covers the 4 spec §3.2 / cycle-prompt Part B gate tests: the additive
Step-A check wired into scripts.lib.invest_ship_strategy.handle_approve_strategy
that refuses strategy approval when the primary ticker lacks a fresh
bear-case PROCEED.

ScanResult enum shape mirrors Bundle 3's existing scan pattern (spec §3.5
scan_backtests_for_slug authoritative shape for cycle 5 -- cycle 2 uses
the same PROCEED / REFUSE-with-reason construction so cycle 5 can slot
its backtest scan in immediately after without interface drift).

Four REFUSE conditions per spec:
  1. Missing bear_verdict field  -> "run /invest bear-case ... first"
  2. bear-last-verified > 30d    -> "bear-case stale ..."
  3. bear_verdict: VETO          -> "bear-case VETO'd ..."
  4. Malformed frontmatter       -> "cannot parse ..."
PROCEED only when bear_verdict: PROCEED AND fresh within 30 days.
"""

from __future__ import annotations

import datetime as _dt
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts.lib import invest_ship_strategy as iss


def _write_ticker(
    vault_root: Path,
    ticker: str,
    *,
    bear_verdict: str | None = None,
    bear_conviction: int | None = None,
    bear_last_verified: str | None = None,
    extra_frontmatter: str = "",
    raw_content: str | None = None,
    include_default_lists: bool = True,
) -> Path:
    """Write wiki/tickers/<ticker>.md with the requested bear-case state.

    `raw_content` (if given) is written verbatim -- used by the malformed-
    frontmatter test. Otherwise this helper composes a schema-complete
    thesis-shaped frontmatter:

      - `include_default_lists=True` (default) auto-populates valid
        bear_top_counterpoints (3 items) + bear_invalidation_scenarios
        (2 items). Tests that want to exercise the persisted-schema-
        enforcement paths pass `include_default_lists=False` + provide
        their own values (or omissions) via `extra_frontmatter`.
    """
    tickers_dir = vault_root / "wiki" / "tickers"
    tickers_dir.mkdir(parents=True, exist_ok=True)
    path = tickers_dir / f"{ticker}.md"
    if raw_content is not None:
        path.write_text(raw_content)
        return path

    lines = [
        "---",
        f"tags: [ticker, {ticker}, thesis]",
        "date: 2026-04-19",
        "type: ticker",
        "origin: k2bi-extract",
        'up: "[[tickers/index]]"',
        f"symbol: {ticker}",
        "thesis_score: 73",
    ]
    if bear_verdict is not None:
        lines.append(f"bear_verdict: {bear_verdict}")
    if bear_conviction is not None:
        lines.append(f"bear_conviction: {bear_conviction}")
    if bear_last_verified is not None:
        lines.append(f"bear-last-verified: {bear_last_verified}")
    if include_default_lists and extra_frontmatter == "":
        lines += [
            "bear_top_counterpoints:",
            "  - c1",
            "  - c2",
            "  - c3",
            "bear_invalidation_scenarios:",
            "  - s1",
            "  - s2",
        ]
    if extra_frontmatter:
        lines.append(extra_frontmatter.rstrip("\n"))
    lines += ["---", "", "## Phase 1: Business Model", "dummy", ""]
    path.write_text("\n".join(lines) + "\n")
    return path


class BearCaseGateBase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_vault = Path(tempfile.mkdtemp(prefix="gate_bear_"))
        self.today = _dt.date(2026, 4, 19)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_vault, ignore_errors=True)


class MissingThesisTests(BearCaseGateBase):
    def test_missing_ticker_file_refuses(self) -> None:
        """No wiki/tickers/<TICKER>.md at all = no bear-case run."""
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("NVDA", result.reason)
        self.assertIn("bear-case", result.reason.lower())


class MissingBearVerdictTests(BearCaseGateBase):
    def test_file_exists_without_bear_verdict_refuses(self) -> None:
        _write_ticker(self.tmp_vault, "NVDA")
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("/invest bear-case NVDA", result.reason)


class StaleBearCaseTests(BearCaseGateBase):
    def test_bear_case_over_30d_old_refuses(self) -> None:
        stale_date = (self.today - _dt.timedelta(days=45)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=stale_date,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("stale", result.reason.lower())
        self.assertIn(stale_date, result.reason)
        self.assertIn("--refresh", result.reason)

    def test_bear_case_exactly_30d_still_fresh(self) -> None:
        boundary = (self.today - _dt.timedelta(days=30)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=boundary,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "PROCEED")

    def test_bear_case_31d_is_stale(self) -> None:
        stale = (self.today - _dt.timedelta(days=31)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=stale,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("stale", result.reason.lower())

    def test_bear_case_missing_last_verified_refuses(self) -> None:
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=None,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")


class VetoGateTests(BearCaseGateBase):
    def test_veto_refuses_with_conviction_in_message(self) -> None:
        fresh = (self.today - _dt.timedelta(days=10)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="VETO",
            bear_conviction=85,
            bear_last_verified=fresh,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("VETO", result.reason)
        self.assertIn("85", result.reason)


class FreshProceedTests(BearCaseGateBase):
    def test_fresh_proceed_is_proceed(self) -> None:
        fresh = (self.today - _dt.timedelta(days=10)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=fresh,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "PROCEED")
        self.assertEqual(result.reason, "")

    def test_same_day_proceed_is_proceed(self) -> None:
        today_str = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=today_str,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "PROCEED")


class MalformedFrontmatterTests(BearCaseGateBase):
    def test_unterminated_frontmatter_refuses(self) -> None:
        # Missing closing `---` fence -> sf.parse raises ValueError.
        _write_ticker(
            self.tmp_vault, "NVDA",
            raw_content="---\nsymbol: NVDA\nno closing fence\n",
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("parse", result.reason.lower())
        self.assertIn("NVDA", result.reason)

    def test_yaml_syntax_error_refuses(self) -> None:
        _write_ticker(
            self.tmp_vault, "NVDA",
            raw_content="---\n: not valid yaml:\n  - [unterminated\n---\n",
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("parse", result.reason.lower())


class BearVerdictEnumTests(BearCaseGateBase):
    def test_unknown_verdict_value_refuses(self) -> None:
        fresh = (self.today - _dt.timedelta(days=10)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="MAYBE",
            bear_conviction=50,
            bear_last_verified=fresh,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")


class PathTraversalTests(BearCaseGateBase):
    """Codex cycle-2 R1 HIGH: order.ticker cannot be used to redirect
    the scan outside wiki/tickers/. Crafted traversal values must be
    refused before any filesystem read."""

    def test_parent_dir_traversal_refuses(self) -> None:
        # Seed a legitimate-looking PROCEED elsewhere in the vault to
        # prove that WITHOUT the guard, the scan could be redirected.
        other_dir = self.tmp_vault / "wiki" / "reference"
        other_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (other_dir / "foo.md").write_text(
            "---\nbear_verdict: PROCEED\nbear_conviction: 40\n"
            f"bear-last-verified: {fresh}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "../reference/foo",
            vault_root=self.tmp_vault,
            now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        # Message must describe the format/scan refusal, NOT the
        # bear-case verdict in the redirected file.
        reason_lower = result.reason.lower()
        self.assertTrue(
            "invalid" in reason_lower or "outside" in reason_lower,
            f"expected format-invalid or containment REFUSE, got: {result.reason!r}",
        )

    def test_absolute_path_refuses(self) -> None:
        result = iss.scan_bear_case_for_ticker(
            "/etc/passwd",
            vault_root=self.tmp_vault,
            now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("invalid", result.reason.lower())

    def test_lowercase_ticker_refuses(self) -> None:
        result = iss.scan_bear_case_for_ticker(
            "nvda", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("invalid", result.reason.lower())

    def test_empty_ticker_refuses(self) -> None:
        result = iss.scan_bear_case_for_ticker(
            "", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")


class TimestampTypedBearLastVerifiedTests(BearCaseGateBase):
    """Codex cycle-2 round-4 MEDIUM: YAML parses timestamps (e.g.
    `2026-04-19T00:00:00Z`) as datetime.datetime, which IS-A date in
    Python. Without the datetime-first check, subtracting `now (date)
    - last (datetime)` raises TypeError and crashes the approval flow.
    Scan must normalise to `.date()` and return a deterministic
    PROCEED/REFUSE."""

    def test_zulu_timestamp_accepted_as_date(self) -> None:
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "symbol: NVDA\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            # Timestamp-shaped value (YAML parses as datetime.datetime).
            f"bear-last-verified: {self.today.isoformat()}T00:00:00Z\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        # Must NOT raise; must return a deterministic verdict.
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "PROCEED")

    def test_naive_timestamp_accepted_as_date(self) -> None:
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "symbol: NVDA\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            # Naive datetime (no timezone).
            f"bear-last-verified: {self.today.isoformat()} 00:00:00\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "PROCEED")

    def test_stale_timestamp_refuses_cleanly(self) -> None:
        stale = self.today - _dt.timedelta(days=45)
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "symbol: NVDA\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {stale.isoformat()}T00:00:00Z\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("stale", result.reason.lower())


class FutureDatedBearCaseTests(BearCaseGateBase):
    """Codex cycle-2 R2 HIGH: a bear-last-verified stamped in the future
    (clock skew / hand-edit) must NOT be treated as fresh. Scan path
    must match writer-side freshness window `[0, FRESH_DAYS]` (both
    bounds inclusive), not just the upper bound."""

    def test_future_bear_last_verified_refuses(self) -> None:
        future = (self.today + _dt.timedelta(days=30)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=future,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("future", result.reason.lower())

    def test_same_day_still_fresh(self) -> None:
        today_str = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=today_str,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "PROCEED")


class PersistedSchemaEnforcementTests(BearCaseGateBase):
    """Codex cycle-2 R3 MEDIUM: enforce the full persisted bear-case
    schema at scan time. A hand-edit setting only verdict + date with
    garbage conviction / missing counterpoints must NOT clear approval."""

    def test_non_int_bear_conviction_refuses(self) -> None:
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_last_verified=fresh,
            extra_frontmatter=(
                "bear_conviction: maybe-40\n"
                "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
                "bear_invalidation_scenarios:\n  - s1\n  - s2"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("integer", result.reason.lower())

    def test_bool_bear_conviction_refuses(self) -> None:
        # YAML `true` parses as Python bool. Must be rejected as integer.
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_last_verified=fresh,
            extra_frontmatter=(
                "bear_conviction: true\n"
                "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
                "bear_invalidation_scenarios:\n  - s1\n  - s2"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")

    def test_conviction_over_100_refuses(self) -> None:
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=999,
            bear_last_verified=fresh,
            extra_frontmatter=(
                "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
                "bear_invalidation_scenarios:\n  - s1\n  - s2"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")

    def test_missing_counterpoints_refuses(self) -> None:
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=fresh,
            extra_frontmatter=(
                # bear_top_counterpoints missing
                "bear_invalidation_scenarios:\n  - s1\n  - s2"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("counterpoints", result.reason)

    def test_wrong_length_counterpoints_refuses(self) -> None:
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=fresh,
            extra_frontmatter=(
                "bear_top_counterpoints:\n  - only-one\n"
                "bear_invalidation_scenarios:\n  - s1\n  - s2"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")

    def test_too_few_scenarios_refuses(self) -> None:
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=fresh,
            extra_frontmatter=(
                "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
                "bear_invalidation_scenarios:\n  - only-one"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")

    def test_too_many_scenarios_refuses(self) -> None:
        fresh = self.today.isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=40,
            bear_last_verified=fresh,
            extra_frontmatter=(
                "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
                "bear_invalidation_scenarios:\n"
                "  - s1\n  - s2\n  - s3\n  - s4\n  - s5\n  - s6"
            ),
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")


class SymlinkedTickersDirTests(BearCaseGateBase):
    """Codex cycle-2 round-2 R1 HIGH: if wiki/tickers is a symlink
    pointing OUTSIDE the vault, the resolved ticker path would look
    contained under the symlink's target but would actually read from
    an external directory. The scan must verify the tickers_dir itself
    stays under vault_root."""

    def test_symlinked_tickers_dir_outside_vault_refuses(self) -> None:
        # Create an external dir with a valid-looking bear-case file.
        external = Path(tempfile.mkdtemp(prefix="gate_external_"))
        try:
            (external / "NVDA.md").write_text(
                "---\n"
                f"bear_verdict: PROCEED\nbear_conviction: 40\n"
                f"bear-last-verified: {self.today.isoformat()}\n"
                "symbol: NVDA\n"
                "thesis_score: 73\n"
                "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
                "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
                "---\n"
            )
            # Rig wiki/tickers as a symlink to the external dir.
            wiki_dir = self.tmp_vault / "wiki"
            wiki_dir.mkdir(parents=True, exist_ok=True)
            symlinked = wiki_dir / "tickers"
            symlinked.symlink_to(external, target_is_directory=True)
            result = iss.scan_bear_case_for_ticker(
                "NVDA", vault_root=self.tmp_vault, now=self.today,
            )
            self.assertEqual(result.verdict, "REFUSE")
            self.assertIn("outside", result.reason.lower())
        finally:
            shutil.rmtree(external, ignore_errors=True)


class NonThesisFileTests(BearCaseGateBase):
    """Codex cycle-2 round-2 R2 HIGH: a hand-crafted bear-case blob
    with no thesis_score must not satisfy approval. Scan must refuse
    unless the file is recognisably a thesis."""

    def test_bear_only_file_without_thesis_score_refuses(self) -> None:
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {fresh}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n\nNot a thesis.\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("thesis_score", result.reason)

    def test_symbol_mismatch_refuses(self) -> None:
        # File at wiki/tickers/NVDA.md claims symbol: AAPL -- scan refuses.
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "symbol: AAPL\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {fresh}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("AAPL", result.reason)
        self.assertIn("NVDA", result.reason)

    def test_missing_symbol_field_refuses(self) -> None:
        # Codex round-3 HIGH: `symbol:` must be REQUIRED, not optional.
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {fresh}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("symbol", result.reason.lower())

    def test_non_string_symbol_refuses(self) -> None:
        # `symbol: 123` parses as int -- must not satisfy the guard.
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "symbol: 123\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {fresh}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")

    def test_null_symbol_refuses(self) -> None:
        # `symbol:` (YAML null) -- must not satisfy the guard.
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        fresh = self.today.isoformat()
        (tickers_dir / "NVDA.md").write_text(
            "---\n"
            "symbol:\n"
            "thesis_score: 73\n"
            "bear_verdict: PROCEED\n"
            "bear_conviction: 40\n"
            f"bear-last-verified: {fresh}\n"
            "bear_top_counterpoints:\n  - c1\n  - c2\n  - c3\n"
            "bear_invalidation_scenarios:\n  - s1\n  - s2\n"
            "---\n"
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")


class NonRegularTickerPathTests(BearCaseGateBase):
    """Codex cycle-2 round-2 R3 MEDIUM: a directory at
    wiki/tickers/<TICKER>.md (rare but possible after bad git operations
    or manual mkdir) must refuse cleanly, not crash approval."""

    def test_directory_at_ticker_path_refuses(self) -> None:
        tickers_dir = self.tmp_vault / "wiki" / "tickers"
        tickers_dir.mkdir(parents=True, exist_ok=True)
        # Create a DIRECTORY where the .md file would live.
        (tickers_dir / "NVDA.md").mkdir()
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("not a regular file", result.reason.lower())


class MalformedBearCaseTests(BearCaseGateBase):
    """MiniMax review finding #6: scan must also refuse internally-
    inconsistent bear-case frontmatter. A `bear_verdict: PROCEED` with
    no `bear_conviction` field is a schema violation (spec §2.2
    mandates all 5 bear_* keys present together); the gate should not
    silently accept it just because the verdict string looks OK."""

    def test_proceed_verdict_without_conviction_refuses(self) -> None:
        fresh = (self.today - _dt.timedelta(days=10)).isoformat()
        # bear_verdict: PROCEED but bear_conviction is MISSING (hand-
        # edited frontmatter shape violation).
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="PROCEED",
            bear_conviction=None,
            bear_last_verified=fresh,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")

    def test_veto_verdict_without_conviction_refuses(self) -> None:
        fresh = (self.today - _dt.timedelta(days=10)).isoformat()
        _write_ticker(
            self.tmp_vault, "NVDA",
            bear_verdict="VETO",
            bear_conviction=None,
            bear_last_verified=fresh,
        )
        result = iss.scan_bear_case_for_ticker(
            "NVDA", vault_root=self.tmp_vault, now=self.today,
        )
        self.assertEqual(result.verdict, "REFUSE")


if __name__ == "__main__":
    unittest.main()
