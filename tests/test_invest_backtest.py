"""Tests for scripts.lib.invest_backtest -- Bundle 4 cycle 3 (m2.15).

Covers the 20-row test matrix from the cycle-3 plan prompt + supporting
unit tests for the sanity gate, capture filename resolution, and
frontmatter schema. Fixtures avoid the real K2Bi-Vault: every test
builds a fresh tmp vault seeded with a minimal strategy spec matching
Bundle 3 cycle 5's REQUIRED_STRATEGY_FIELDS.

Price data is injected via the `price_fetcher` seam on `run_backtest`
so no yfinance calls fire during unit tests. Production yfinance path
is smoke-tested separately by the cycle 3 manual dry-run.

Test classes:

    * SanityGateTests            -- 5-row matrix on `_apply_sanity_gate`.
    * HappyPathTests             -- end-to-end happy path; file written,
                                    schema populated, look_ahead_check
                                    passed.
    * StrategyNotFoundTests      -- refuse with typed error.
    * EmptyPriceDataTests        -- fetcher returns empty -> error.
    * AtomicWriteTests           -- monkeypatch os.replace to raise;
                                    final file absent + no orphan temp.
    * HookIntegrationTests       -- pre-commit Check D regex never
                                    matches raw/backtests/ paths.
    * StrategyUntouchedTests     -- byte-for-byte hash of strategy
                                    file before/after run.
    * FilenameCollisionTests     -- same-day re-run gets _HHMMSS_
                                    inserted.
    * ScanTests                  -- 8-row scan matrix: empty, passed,
                                    suspicious-no-override, suspicious-
                                    with-override, empty-file-skip,
                                    malformed YAML, unknown enum,
                                    most-recent selection.
    * BodyFormatTests            -- verify Metrics table + Sanity Gate
                                    Result rendered correctly.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from scripts.lib import invest_backtest as ib
from scripts.lib import invest_ship_strategy as iss
from scripts.lib import strategy_frontmatter as sf


# ---------- fixture helpers ----------


def _seed_vault(root: Path) -> None:
    """Create the minimal vault tree invest-backtest writes into."""
    (root / "wiki" / "strategies").mkdir(parents=True)
    (root / "wiki" / "tickers").mkdir(parents=True)
    (root / "raw" / "backtests").mkdir(parents=True)


def _seed_strategy(
    root: Path,
    slug: str = "spy-rotational",
    *,
    ticker: str = "SPY",
    include_backtest_override: bool = False,
    override_reason: str = "total_return=620.0% > 500%",
    override_capture_filename: str | None = None,
) -> Path:
    """Write a strategy spec matching Bundle 3 cycle 5 REQUIRED fields."""
    strat_dir = root / "wiki" / "strategies"
    strat_dir.mkdir(parents=True, exist_ok=True)
    path = strat_dir / f"strategy_{slug}.md"
    lines = [
        "---",
        f"name: {slug}",
        "status: proposed",
        "strategy_type: hand_crafted",
        "risk_envelope_pct: 0.01",
        "regime_filter:",
        "  - risk_on",
        "order:",
        f"  ticker: {ticker}",
        "  side: buy",
        "  qty: 1",
        "  limit_price: 500.00",
        "  stop_loss: 490.00",
        "  time_in_force: DAY",
        "tags: [strategy, SPY]",
        "date: 2026-04-19",
        "type: strategy",
        "origin: keith",
        'up: "[[index]]"',
        "---",
        "",
        "## How This Works",
        "",
        "Buy SPY at 500 limit with 490 stop when risk_on regime holds.",
    ]
    if include_backtest_override:
        capture_name = override_capture_filename or (
            f"2026-04-19_{slug}_backtest.md"
        )
        lines += [
            "",
            "## Backtest Override",
            "",
            f"Backtest run: 2026-04-19 at raw/backtests/{capture_name}",
            f"Suspicious flag reason: {override_reason}",
            "Why this is acceptable: initial sanity baseline on SPY hits "
            "the 500% threshold because the 2-year window caught the "
            "post-2024 rally; strategy logic is conservative and not "
            "look-ahead dependent.",
        ]
    content = "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def _build_prices(close_values: list[float], start_date: _dt.date) -> Any:
    """Return a pandas DataFrame shaped like yfinance's output: a
    `Close` column indexed by trading-day DatetimeIndex. Business-day
    frequency skips weekends to match yfinance behaviour."""
    import pandas as pd

    idx = pd.bdate_range(start=start_date, periods=len(close_values))
    return pd.DataFrame({"Close": close_values}, index=idx)


def _linear_series(start: float, stop: float, n: int) -> list[float]:
    """Evenly-spaced list of floats from `start` to `stop` (inclusive)."""
    if n <= 1:
        return [start]
    step = (stop - start) / (n - 1)
    return [start + step * i for i in range(n)]


def _normal_series(n: int = 300, seed: int = 42) -> list[float]:
    """Deterministic pseudo-random walk with slight upward drift.

    Produces a series with multiple SMA crossovers so the happy-path
    backtest has a realistic number of trades. Seeded for repeatability;
    values are tuned so the resulting sim metrics land INSIDE the sanity
    thresholds (total_return < 500, max_dd < -2, win_rate < 85).
    """
    import numpy as np

    rng = np.random.default_rng(seed)
    # 0.08% daily mean, 1.1% daily std -> about 20% annualised return
    # with 17% volatility; produces maybe 3-5 crossovers over 300 days.
    daily = rng.normal(0.0008, 0.011, n)
    close = 100.0 * np.cumprod(1.0 + daily)
    return [float(x) for x in close]


def _mock_sha_resolver(
    sha: str = "deadbeefcafe1234567890abcdef0123456789ab",
) -> Any:
    # 40-char hex string matches real `git rev-parse HEAD` output width.
    return lambda: sha


def _fixed_fetcher(close_values: list[float], start: _dt.date) -> Any:
    prices = _build_prices(close_values, start)

    def fetcher(symbol: str, s: _dt.date, e: _dt.date) -> Any:
        return prices

    return fetcher


def _empty_fetcher() -> Any:
    import pandas as pd

    empty = pd.DataFrame({"Close": []})

    def fetcher(symbol: str, s: _dt.date, e: _dt.date) -> Any:
        return empty

    return fetcher


# ---------- SanityGateTests (rows 2-5) ----------


class SanityGateTests(unittest.TestCase):
    """Direct tests on `_apply_sanity_gate` -- deterministic, no sim."""

    def _metrics(self, **overrides: Any) -> ib.BacktestMetrics:
        base = dict(
            sharpe=1.0,
            sortino=1.5,
            max_dd_pct=-5.0,
            win_rate_pct=55.0,
            avg_winner_pct=2.0,
            avg_loser_pct=-1.5,
            total_return_pct=20.0,
            n_trades=30,
            avg_trade_holding_days=5.0,
        )
        base.update(overrides)
        return ib.BacktestMetrics(**base)

    def test_passed_when_all_within_bounds(self) -> None:
        ok = self._metrics()
        check, reason = ib._apply_sanity_gate(ok)
        self.assertEqual(check, "passed")
        self.assertEqual(reason, "")

    def test_total_return_trips(self) -> None:
        bad = self._metrics(total_return_pct=620.5)
        check, reason = ib._apply_sanity_gate(bad)
        self.assertEqual(check, "suspicious")
        self.assertIn("total_return", reason)
        self.assertIn("620.5", reason)
        self.assertIn("500", reason)

    def test_max_dd_trips(self) -> None:
        # -1.5 is shallower than -2 (i.e. -1.5 > -2), so suspicious.
        bad = self._metrics(max_dd_pct=-1.5)
        check, reason = ib._apply_sanity_gate(bad)
        self.assertEqual(check, "suspicious")
        self.assertIn("max_dd", reason)
        self.assertIn("-1.5", reason)
        self.assertIn("-2", reason)

    def test_win_rate_trips(self) -> None:
        bad = self._metrics(win_rate_pct=91.0)
        check, reason = ib._apply_sanity_gate(bad)
        self.assertEqual(check, "suspicious")
        self.assertIn("win_rate", reason)
        self.assertIn("91.0", reason)
        self.assertIn("85", reason)

    def test_multiple_thresholds_all_reported(self) -> None:
        bad = self._metrics(
            total_return_pct=700.0,
            max_dd_pct=-0.5,
            win_rate_pct=95.0,
        )
        check, reason = ib._apply_sanity_gate(bad)
        self.assertEqual(check, "suspicious")
        self.assertIn("total_return", reason)
        self.assertIn("max_dd", reason)
        self.assertIn("win_rate", reason)
        # Semicolon-joined so Keith sees all at once in output.
        self.assertIn(";", reason)

    def test_boundary_not_suspicious(self) -> None:
        # Exactly at threshold is NOT suspicious (strict > rule).
        ok = self._metrics(
            total_return_pct=500.0,
            max_dd_pct=-2.0,
            win_rate_pct=85.0,
        )
        check, _ = ib._apply_sanity_gate(ok)
        self.assertEqual(check, "passed")


# ---------- HappyPathTests (row 1 + row 10 strategy-untouched combined) ----------


class HappyPathTests(unittest.TestCase):
    """End-to-end happy path: seed strategy -> run_backtest -> verify
    capture file exists with schema populated AND strategy file is
    byte-identical before and after (Check D lockdown contract)."""

    def setUp(self) -> None:
        self.vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(self.vault)
        self.slug = "spy-rotational"
        self.strategy_path = _seed_strategy(self.vault, self.slug)
        self.now = _dt.datetime(
            2026, 4, 19, 10, 15, 0, tzinfo=_dt.timezone.utc
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.vault, ignore_errors=True)

    def test_happy_path_writes_capture_with_all_schema_fields(self) -> None:
        pre_hash = hashlib.sha256(
            self.strategy_path.read_bytes()
        ).hexdigest()

        close = _normal_series(300)
        result = ib.run_backtest(
            self.slug,
            vault_root=self.vault,
            window_start=_dt.date(2024, 4, 19),
            window_end=_dt.date(2026, 4, 19),
            now=self.now,
            price_fetcher=_fixed_fetcher(close, _dt.date(2024, 4, 19)),
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )

        # File written at expected location.
        self.assertTrue(result.path.exists())
        self.assertEqual(
            result.path.name,
            f"2026-04-19_{self.slug}_backtest.md",
        )
        self.assertEqual(
            result.path.parent,
            self.vault / "raw" / "backtests",
        )

        # Schema validation: parse frontmatter and assert spec §2.5 shape.
        fm = sf.parse(result.path.read_bytes())
        self.assertEqual(fm["type"], "backtest")
        self.assertEqual(fm["origin"], "k2bi-generate")
        self.assertEqual(fm["strategy_slug"], self.slug)
        self.assertEqual(
            fm["strategy_commit_sha"],
            "deadbeefcafe1234567890abcdef0123456789ab",
        )
        bt = fm["backtest"]
        self.assertEqual(bt["source"], "yfinance")
        self.assertEqual(bt["source_version"], "1.3.0")
        self.assertEqual(bt["symbol"], "SPY")
        # SPY strategy ticker == reference -> reference falls back to symbol.
        self.assertEqual(bt["reference_symbol"], "SPY")
        # Window fields present.
        self.assertEqual(bt["window"]["start"], "2024-04-19")
        self.assertEqual(bt["window"]["end"], "2026-04-19")
        # Metrics block has all 9 required keys.
        metrics = bt["metrics"]
        for k in (
            "sharpe",
            "sortino",
            "max_dd_pct",
            "win_rate_pct",
            "avg_winner_pct",
            "avg_loser_pct",
            "total_return_pct",
            "n_trades",
            "avg_trade_holding_days",
        ):
            self.assertIn(k, metrics, f"metrics missing key {k!r}")
        # Happy path should be `passed` (the normal-series fixture is
        # tuned to stay within thresholds).
        self.assertEqual(
            bt["look_ahead_check"],
            "passed",
            f"expected passed; metrics={metrics}; reason={bt['look_ahead_check_reason']!r}",
        )
        self.assertEqual(bt["look_ahead_check_reason"], "")
        # last_run is an ISO-8601 string with timezone.
        self.assertIsInstance(bt["last_run"], str)
        self.assertIn("2026-04-19", bt["last_run"])

        # Strategy file byte-identical (row 10).
        post_hash = hashlib.sha256(
            self.strategy_path.read_bytes()
        ).hexdigest()
        self.assertEqual(
            pre_hash,
            post_hash,
            "strategy file was modified during backtest run -- "
            "Check D lockdown would fail",
        )

        # BacktestResult also has the typed metrics + trades.
        self.assertEqual(result.look_ahead_check, "passed")
        self.assertEqual(result.slug, self.slug)
        self.assertEqual(result.symbol, "SPY")
        # Reference falls back to None on-result when symbol == reference.
        self.assertIsNone(result.reference_symbol)

    def test_reference_recorded_when_different_from_symbol(self) -> None:
        """When the strategy ticker differs from --reference-symbol, the
        capture records both; reference_symbol in frontmatter is the
        distinct benchmark ticker (spec §2.5)."""
        _seed_strategy(self.vault, slug="nvda", ticker="NVDA")
        close = _normal_series(300)
        result = ib.run_backtest(
            "nvda",
            vault_root=self.vault,
            now=self.now,
            price_fetcher=_fixed_fetcher(close, _dt.date(2024, 4, 19)),
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )
        fm = sf.parse(result.path.read_bytes())
        self.assertEqual(fm["backtest"]["symbol"], "NVDA")
        self.assertEqual(fm["backtest"]["reference_symbol"], "SPY")
        self.assertEqual(result.reference_symbol, "SPY")


# ---------- StrategyNotFoundTests (row 7) ----------


class StrategyNotFoundTests(unittest.TestCase):
    def test_unknown_slug_refuses_with_typed_error(self) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        try:
            with self.assertRaises(ib.BacktestError) as cm:
                ib.run_backtest(
                    "ghost-strategy",
                    vault_root=vault,
                    price_fetcher=_fixed_fetcher(
                        _normal_series(300), _dt.date(2024, 1, 1)
                    ),
                    sha_resolver=_mock_sha_resolver(),
                    source_version="1.3.0",
                )
            self.assertIn("strategy_ghost-strategy.md not found", str(cm.exception))
        finally:
            shutil.rmtree(vault, ignore_errors=True)


# ---------- EmptyPriceDataTests (row 6) ----------


class EmptyPriceDataTests(unittest.TestCase):
    def test_empty_dataframe_from_fetcher_refuses(self) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        _seed_strategy(vault)
        try:
            with self.assertRaises(ib.BacktestError) as cm:
                ib.run_backtest(
                    "spy-rotational",
                    vault_root=vault,
                    price_fetcher=_empty_fetcher(),
                    sha_resolver=_mock_sha_resolver(),
                    source_version="1.3.0",
                )
            self.assertIn("insufficient price history", str(cm.exception))
        finally:
            shutil.rmtree(vault, ignore_errors=True)

    def test_fetcher_raising_wraps_as_backtest_error(self) -> None:
        """yfinance can raise JSONDecodeError / RequestException / etc.
        Wrap everything so the SKILL.md body gets one failure shape."""
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        _seed_strategy(vault)

        def broken_fetcher(symbol: str, s: _dt.date, e: _dt.date) -> Any:
            raise ConnectionError("rate-limited")

        try:
            with self.assertRaises(ib.BacktestError) as cm:
                ib.run_backtest(
                    "spy-rotational",
                    vault_root=vault,
                    price_fetcher=broken_fetcher,
                    sha_resolver=_mock_sha_resolver(),
                    source_version="1.3.0",
                )
            self.assertIn("price fetch", str(cm.exception))
            self.assertIn("rate-limited", str(cm.exception))
        finally:
            shutil.rmtree(vault, ignore_errors=True)


# ---------- AtomicWriteTests (row 8) ----------


class AtomicWriteTests(unittest.TestCase):
    def test_failure_between_fsync_and_replace_leaves_no_artefacts(
        self,
    ) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        _seed_strategy(vault)
        try:
            close = _normal_series(300)
            original_replace = os.replace
            call_count = {"n": 0}

            def flaky_replace(src: Any, dst: Any) -> None:
                # Only fail for files that look like our backtest capture;
                # the raw/backtests/index.md stub write + any other
                # atomic writes must still succeed so the test isolates
                # the failure to the capture write itself.
                dst_str = os.fspath(dst)
                if "_spy-rotational_backtest.md" in dst_str:
                    call_count["n"] += 1
                    raise OSError("simulated disk failure")
                original_replace(src, dst)

            with mock.patch(
                "scripts.lib.strategy_frontmatter.os.replace",
                side_effect=flaky_replace,
            ):
                with self.assertRaises(OSError):
                    ib.run_backtest(
                        "spy-rotational",
                        vault_root=vault,
                        price_fetcher=_fixed_fetcher(
                            close, _dt.date(2024, 4, 19)
                        ),
                        sha_resolver=_mock_sha_resolver(),
                        source_version="1.3.0",
                    )

            self.assertEqual(call_count["n"], 1)
            # Final capture file should not exist.
            backtests_dir = vault / "raw" / "backtests"
            captures = list(backtests_dir.glob("*_spy-rotational_backtest.md"))
            self.assertEqual(
                captures,
                [],
                f"expected no capture file; found {captures}",
            )
            # No orphan tempfile in the target directory.
            orphans = [
                p
                for p in backtests_dir.iterdir()
                if p.name.startswith(".") and "tmp" in p.name
            ]
            self.assertEqual(
                orphans,
                [],
                f"expected no orphan tempfile; found {orphans}",
            )
        finally:
            shutil.rmtree(vault, ignore_errors=True)


# ---------- HookIntegrationTests (row 9) ----------


class HookIntegrationTests(unittest.TestCase):
    """Pre-commit Check D scope: `^wiki/strategies/strategy_[^/]+\\.md$`.

    A commit adding a raw/backtests/ file must not trigger Check D. The
    hook helper's canonical path regex is the authority; spot-check it
    here so a future loosening that would catch raw/ paths fails loud.
    """

    CHECK_D_GLOB_RE = re.compile(r"^wiki/strategies/strategy_[^/]+\.md$")

    def test_raw_backtests_path_does_not_match_check_d(self) -> None:
        self.assertIsNone(
            self.CHECK_D_GLOB_RE.match(
                "raw/backtests/2026-04-19_spy-rotational_backtest.md"
            )
        )
        self.assertIsNone(
            self.CHECK_D_GLOB_RE.match(
                "raw/backtests/2026-04-19_143022_spy_backtest.md"
            )
        )
        self.assertIsNone(
            self.CHECK_D_GLOB_RE.match("raw/backtests/index.md")
        )

    def test_wiki_strategies_path_still_matches_check_d(self) -> None:
        # Defence-in-depth: ensure the regex wasn't accidentally changed.
        self.assertIsNotNone(
            self.CHECK_D_GLOB_RE.match(
                "wiki/strategies/strategy_spy-rotational.md"
            )
        )

    def test_canonical_strategy_path_re_from_invest_ship_module(
        self,
    ) -> None:
        """The helper module defines its own CANONICAL regex; confirm
        parity with the bash hook's glob so gate + hook agree."""
        self.assertIsNotNone(
            iss.CANONICAL_STRATEGY_PATH_RE.match(
                "wiki/strategies/strategy_spy-rotational.md"
            )
        )
        self.assertIsNone(
            iss.CANONICAL_STRATEGY_PATH_RE.match(
                "raw/backtests/2026-04-19_spy-rotational_backtest.md"
            )
        )


# ---------- FilenameCollisionTests (row 11) ----------


class FilenameCollisionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(self.vault)
        _seed_strategy(self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.vault, ignore_errors=True)

    def test_same_second_third_run_does_not_overwrite(self) -> None:
        """Codex R9 #2 HIGH regression: a third run at the same
        HHMMSS as the second (i.e. two reruns within the same second)
        must NOT overwrite the second capture. Each run produces a
        distinct file so the immutable audit trail is preserved."""
        close = _normal_series(300)
        fetcher = _fixed_fetcher(close, _dt.date(2024, 4, 19))

        # Three runs with literally-identical `now` values. The third
        # can't share a filename with the second; microsecond-suffix
        # fallback kicks in.
        fixed_now = _dt.datetime(
            2026, 4, 19, 10, 0, 0, 500000, tzinfo=_dt.timezone.utc
        )
        first = ib.run_backtest(
            "spy-rotational",
            vault_root=self.vault,
            now=fixed_now,
            price_fetcher=fetcher,
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )
        second = ib.run_backtest(
            "spy-rotational",
            vault_root=self.vault,
            now=fixed_now,
            price_fetcher=fetcher,
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )
        third = ib.run_backtest(
            "spy-rotational",
            vault_root=self.vault,
            now=fixed_now,
            price_fetcher=fetcher,
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )
        # All three paths must be distinct.
        self.assertNotEqual(first.path, second.path)
        self.assertNotEqual(second.path, third.path)
        self.assertNotEqual(first.path, third.path)
        # All three files must exist (none overwritten).
        self.assertTrue(first.path.exists())
        self.assertTrue(second.path.exists())
        self.assertTrue(third.path.exists())

    def test_same_day_second_run_gets_hhmmss_suffix(self) -> None:
        close = _normal_series(300)
        fetcher = _fixed_fetcher(close, _dt.date(2024, 4, 19))

        first = ib.run_backtest(
            "spy-rotational",
            vault_root=self.vault,
            now=_dt.datetime(2026, 4, 19, 10, 0, 0, tzinfo=_dt.timezone.utc),
            price_fetcher=fetcher,
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )
        self.assertEqual(first.path.name, "2026-04-19_spy-rotational_backtest.md")

        second = ib.run_backtest(
            "spy-rotational",
            vault_root=self.vault,
            now=_dt.datetime(2026, 4, 19, 14, 30, 22, tzinfo=_dt.timezone.utc),
            price_fetcher=fetcher,
            sha_resolver=_mock_sha_resolver(),
            source_version="1.3.0",
        )
        self.assertEqual(
            second.path.name,
            "2026-04-19_143022_spy-rotational_backtest.md",
        )
        # Both files exist after second run -- first NOT overwritten.
        self.assertTrue(first.path.exists())
        self.assertTrue(second.path.exists())
        self.assertNotEqual(first.path, second.path)


# ---------- ScanTests (rows 12-19) ----------


def _write_backtest_capture(
    vault: Path,
    slug: str,
    *,
    filename: str,
    look_ahead_check: str = "passed",
    look_ahead_check_reason: str = "",
    backtest_block_override: dict | None = None,
    raw_text: str | None = None,
) -> Path:
    """Write a stub backtest capture to raw/backtests/<filename> for
    scan tests. `raw_text` short-circuits the frontmatter build for
    malformed-YAML fixtures; otherwise we render a valid §2.5 shape."""
    backtests_dir = vault / "raw" / "backtests"
    backtests_dir.mkdir(parents=True, exist_ok=True)
    path = backtests_dir / filename
    if raw_text is not None:
        path.write_text(raw_text, encoding="utf-8")
        return path
    if backtest_block_override is not None:
        bt_block = backtest_block_override
    else:
        bt_block = {
            "window": {"start": "2024-04-19", "end": "2026-04-19"},
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
            "look_ahead_check": look_ahead_check,
            "look_ahead_check_reason": look_ahead_check_reason,
            "last_run": "2026-04-19T10:00:00+00:00",
        }
    import yaml

    fm = {
        "tags": ["backtest", slug, "raw"],
        "date": "2026-04-19",
        "type": "backtest",
        "origin": "k2bi-generate",
        "up": "[[backtests/index]]",
        "strategy_slug": slug,
        "strategy_commit_sha": "abc123def456",
        "backtest": bt_block,
    }
    content = (
        "---\n"
        + yaml.safe_dump(fm, sort_keys=False, default_flow_style=False)
        + "---\n\nbody\n"
    )
    path.write_text(content, encoding="utf-8")
    return path


class ScanTests(unittest.TestCase):
    """Spec §3.5 LOCKED algorithm coverage."""

    def setUp(self) -> None:
        self.vault = Path(tempfile.mkdtemp(prefix="ib_scan_"))
        _seed_vault(self.vault)

    def tearDown(self) -> None:
        shutil.rmtree(self.vault, ignore_errors=True)

    def test_empty_raw_backtests_refuses(self) -> None:
        result = iss.scan_backtests_for_slug(
            "missing", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("no backtest found", result.reason)
        self.assertIn("missing", result.reason)

    def test_passed_backtest_proceeds(self) -> None:
        _seed_strategy(self.vault, slug="spy-rotational")
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="passed",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_suspicious_without_override_refuses(self) -> None:
        _seed_strategy(self.vault, slug="spy-rotational")
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620.0% > 500%",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("suspicious", result.reason)
        self.assertIn("total_return", result.reason)
        self.assertIn("Backtest Override", result.reason)

    def test_suspicious_with_override_proceeds(self) -> None:
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=True,
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620.0% > 500%",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_empty_file_skipped_and_next_used(self) -> None:
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        # Empty 0-byte file sorts FIRST in descending order of our
        # filenames (later date), valid file sorts SECOND. Scanner
        # should skip the empty one.
        empty = backtests / "2026-04-20_spy-rotational_backtest.md"
        empty.write_bytes(b"")
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="passed",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_all_empty_refuses(self) -> None:
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_bytes(b"")
        (backtests / "2026-04-18_spy-rotational_backtest.md").write_bytes(b"")
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("all are empty", result.reason)

    def test_malformed_yaml_refuses(self) -> None:
        _seed_strategy(self.vault, slug="spy-rotational")
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            raw_text="---\nthis: is: not: valid: yaml\n---\nbody\n",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("unparseable", result.reason)

    def test_unknown_look_ahead_check_refuses(self) -> None:
        _seed_strategy(self.vault, slug="spy-rotational")
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="maybe",
            look_ahead_check_reason="",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("unknown look_ahead_check", result.reason)

    def test_missing_metrics_block_refuses(self) -> None:
        """MiniMax R1 #1 HIGH: a hand-crafted capture with
        `look_ahead_check: passed` but no `metrics:` subfield must NOT
        clear approval. Mirrors cycle-2 bear-case schema enforcement."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            "tags: [backtest, spy-rotational, raw]\n"
            "date: 2026-04-19\n"
            "type: backtest\n"
            "origin: k2bi-generate\n"
            'up: "[[backtests/index]]"\n'
            "strategy_slug: spy-rotational\n"
            "backtest:\n"
            "  look_ahead_check: passed\n"
            "  last_run: 2026-04-19T10:00:00Z\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("`metrics:` mapping", result.reason)

    def test_missing_last_run_refuses(self) -> None:
        """MiniMax R1 #1 follow-on: `last_run` is a required provenance
        field; a capture without it is malformed audit-trail-wise."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            "tags: [backtest, spy-rotational, raw]\n"
            "date: 2026-04-19\n"
            "type: backtest\n"
            "origin: k2bi-generate\n"
            'up: "[[backtests/index]]"\n'
            "strategy_slug: spy-rotational\n"
            "backtest:\n"
            "  look_ahead_check: passed\n"
            "  metrics:\n"
            "    sharpe: 1.0\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("`last_run`", result.reason)

    def test_vault_missing_disambiguated_from_no_backtest(self) -> None:
        """MiniMax R1 #3 MEDIUM: a non-existent vault_root must surface
        as a clearly-different error than "no backtest yet"."""
        import tempfile

        # Create a tmp path then delete the dir so resolve() still works
        # but exists() is False. Using a stable parent to avoid cleanup
        # races with setUp.tmpdir fixtures.
        missing = Path(tempfile.mkdtemp(prefix="ib_missing_"))
        shutil.rmtree(missing)
        self.assertFalse(missing.exists())
        result = iss.scan_backtests_for_slug("spy", vault_root=missing)
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("vault_root", result.reason)
        self.assertIn("does not exist", result.reason)

    def test_missing_backtest_block_refuses(self) -> None:
        """Malformed schema: frontmatter parses but `backtest:` mapping
        is missing. Scanner refuses rather than AttributeError-ing on
        `.get("look_ahead_check")` against a non-dict."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            "tags: [backtest, spy-rotational, raw]\n"
            "date: 2026-04-19\n"
            "type: backtest\n"
            "origin: k2bi-generate\n"
            'up: "[[backtests/index]]"\n'
            "strategy_slug: spy-rotational\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("`backtest:` mapping", result.reason)

    def test_most_recent_by_filename_descending(self) -> None:
        """Three files across three dates; scanner picks the latest date."""
        _seed_strategy(self.vault, slug="spy-rotational")
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-10_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="win_rate=95% > 85%",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-15_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="max_dd=-1% > -2%",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-18_spy-rotational_backtest.md",
            look_ahead_check="passed",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        # Latest file is passed -> PROCEED (despite earlier suspicious files).
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_strategy_slug_mismatch_refuses(self) -> None:
        """Codex R1 #3 MEDIUM: a capture file renamed to satisfy the
        glob for a different slug must still refuse if its internal
        `strategy_slug` does not match the requested slug. Provenance
        check closes the hand-crafted-file-renamed attack."""
        _seed_strategy(self.vault, slug="spy-rotational")
        # Deliberately lie about the slug in frontmatter.
        _write_backtest_capture(
            self.vault,
            "spy-rotational",  # filename matches the target glob
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="passed",
            backtest_block_override=None,
        )
        # Now overwrite strategy_slug in the frontmatter to lie about
        # provenance (simulating a copied capture).
        target = (
            self.vault
            / "raw"
            / "backtests"
            / "2026-04-19_spy-rotational_backtest.md"
        )
        content = target.read_text(encoding="utf-8")
        target.write_text(
            content.replace(
                "strategy_slug: spy-rotational",
                "strategy_slug: some-other-strategy",
            ),
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("strategy_slug=", result.reason)
        self.assertIn("some-other-strategy", result.reason)

    def test_last_run_selects_later_timestamp_same_day(self) -> None:
        """Codex R1 #1 HIGH: when two same-day captures exist, the
        scanner must select by `backtest.last_run` timestamp -- NOT
        filename-descending -- so the later run always wins even when
        lex order disagrees.

        Setup: first run (bare filename) has `last_run: 10:00:00` +
        suspicious. Second run (HHMMSS form) has `last_run: 14:30:22`
        + passed. Filename-descending picks the bare form (suspicious,
        earlier). last_run-based selection picks the HHMMSS form
        (passed, later) -> PROCEED.
        """
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Earlier run, bare filename, suspicious.
        earlier = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "suspicious",
                "look_ahead_check_reason": "max_dd=-1% > -2%",
                "last_run": "2026-04-19T10:00:00+00:00",
            },
        }
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(earlier, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        # Later run, HHMMSS filename, passed.
        later = dict(earlier)
        later["backtest"] = dict(earlier["backtest"])
        later["backtest"]["look_ahead_check"] = "passed"
        later["backtest"]["look_ahead_check_reason"] = ""
        later["backtest"]["last_run"] = "2026-04-19T14:30:22+00:00"
        (
            backtests
            / "2026-04-19_143022_spy-rotational_backtest.md"
        ).write_text(
            "---\n"
            + yaml.safe_dump(later, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        # With last_run-based selection, the later (passed) run is
        # picked -> PROCEED. With filename-descending-only, the earlier
        # (suspicious) run would be picked -> REFUSE. This test
        # distinguishes the two paths.
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_override_without_justification_label_refuses(self) -> None:
        """Codex R1 #2 HIGH: an override section with no
        `Why this is acceptable:` label must NOT clear approval. A bare
        heading bypass would defeat the entire gate."""
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=False,
        )
        # Append a bare heading-only override section (no justification).
        strategy_path = (
            self.vault / "wiki" / "strategies" / "strategy_spy-rotational.md"
        )
        strategy_path.write_text(
            strategy_path.read_text(encoding="utf-8")
            + "\n## Backtest Override\n\n(heading only)\n",
            encoding="utf-8",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620% > 500%",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("Why this is acceptable:", result.reason)

    def test_override_with_mismatched_backtest_run_refuses(self) -> None:
        """Codex R2 #1 HIGH: a stale override pointing at a PRIOR
        capture must not clear a NEW suspicious run. Override's
        `Backtest run:` line must name the selected capture."""
        # Strategy seeded with override pointing at a 2026-04-10 capture
        # (stale) while we seed a 2026-04-19 capture as the target.
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=True,
            override_capture_filename=(
                "2026-04-10_spy-rotational_backtest.md"
            ),
            override_reason="total_return=620.0% > 500%",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620.0% > 500%",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("does not reference", result.reason)
        self.assertIn("2026-04-19_spy-rotational_backtest.md", result.reason)

    def test_override_with_mismatched_suspicious_reason_refuses(
        self,
    ) -> None:
        """Codex R2 #1 HIGH: override's `Suspicious flag reason:` must
        contain the CURRENT look_ahead_check_reason as substring. A
        stale reason pointing at a different suspicious trigger must
        not clear a new run with a different reason."""
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=True,
            # Override reason was for a PRIOR run that tripped win_rate.
            override_reason="win_rate=92.5% > 85%",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            # But CURRENT run trips total_return instead.
            look_ahead_check_reason="total_return=620.0% > 500%",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("does not contain the current reason", result.reason)

    def test_off_scheme_filename_ignored(self) -> None:
        """Codex R8 #1 HIGH regression: handcrafted files with names
        that glob-match `*_<slug>_backtest.md` but don't match the
        writer's strict contract (wrong date format, invalid HHMMSS,
        junk prefix) must be ignored during selection. Forged passed
        evidence in an off-scheme file cannot clear the gate."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Hand-crafted off-scheme file with "passed" content. If the
        # scanner admits it, approval would proceed.
        forged = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                "last_run": "2026-04-19T10:00:00+00:00",
            },
        }
        forged_content = (
            "---\n"
            + yaml.safe_dump(forged, sort_keys=False)
            + "---\nbody\n"
        )
        # Off-scheme: junk prefix.
        (
            backtests / "junk_2026-04-19_spy-rotational_backtest.md"
        ).write_text(forged_content, encoding="utf-8")
        # Off-scheme: invalid HHMMSS (99:99:99).
        (
            backtests / "2026-04-19_999999_spy-rotational_backtest.md"
        ).write_text(forged_content, encoding="utf-8")
        # No writer-produced capture at all.
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        # Off-scheme files are filtered out, so the scan sees no
        # valid captures and emits the standard "no backtest found".
        self.assertIn("no backtest found", result.reason)

    def test_malformed_next_day_bare_blocks_approval(self) -> None:
        """Codex R7 #1 HIGH regression: a VALID bare capture on day N
        with negative-offset last_run (whose UTC instant spills into
        day N+1) must not cause a malformed bare capture filenamed
        for day N+1 to be ignored. Filename-domain comparison (date +
        HHMMSS) keeps ordering correct regardless of UTC spillover."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Valid bare capture on 2026-04-19 with Pacific-evening
        # last_run. UTC instant: 2026-04-20T01:00:00Z (next-day UTC).
        valid = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                "last_run": "2026-04-19T18:00:00-07:00",
            },
        }
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(valid, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        # Malformed bare capture on 2026-04-20 (next day).
        (backtests / "2026-04-20_spy-rotational_backtest.md").write_text(
            "---\ninvalid: : yaml\n---\n", encoding="utf-8"
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("newer backtest capture", result.reason)
        self.assertIn("2026-04-20", result.reason)

    def test_stale_bare_cant_outrank_newer_via_future_last_run(
        self,
    ) -> None:
        """Codex R6 #1 HIGH regression: an older bare-form capture
        cannot forge a future `last_run` within the 48h window to
        outrank a newer real bare capture. The calendar-date-in-
        timezone check requires last_run's local date to match the
        filename date, which naturally catches the forgery."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Older bare capture dated 2026-04-19 with FORGED last_run
        # 47 hours later (2026-04-21 early UTC). Local date =
        # 2026-04-21 != filename 2026-04-19 => REJECTED by helper.
        forged = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                # ~47h in the future from filename date -- previously
                # within the 48h tolerance, but the calendar-date
                # check sees a date mismatch.
                "last_run": "2026-04-20T23:00:00+00:00",
            },
        }
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(forged, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        # Real newer capture on 2026-04-20 with suspicious verdict.
        real_newer = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-20",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc456",
            "backtest": {
                "window": {"start": "2024-04-20", "end": "2026-04-20"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 700.0},
                "look_ahead_check": "suspicious",
                "look_ahead_check_reason": "total_return=700.0% > 500%",
                "last_run": "2026-04-20T10:00:00+00:00",
            },
        }
        (backtests / "2026-04-20_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(real_newer, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        # Expected: the forged older capture is rejected by consistency
        # check; the real newer (suspicious) capture is selected; no
        # override in strategy body -> REFUSE.
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        # The REFUSE reason should point at the REAL 2026-04-20
        # capture (the one actually selected as most recent), not at
        # the forged 2026-04-19 one.
        self.assertIn("2026-04-20", result.reason)

    def test_empty_suspicious_reason_refuses(self) -> None:
        """Codex R7 #2 MEDIUM regression: `look_ahead_check: suspicious`
        with an empty `look_ahead_check_reason` must REFUSE even when
        a non-empty override section is present. An empty reason
        silently bypasses the override-binding check, defeating the
        accountability invariant."""
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=True,
            # Override references an empty reason so it would otherwise
            # pass the reason-binding substring match (empty string IS
            # always a substring). That's the bypass this test locks.
            override_reason="",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("empty", result.reason)

    def test_non_string_suspicious_reason_refuses(self) -> None:
        """Codex R6 #2 MEDIUM regression: a malformed capture with
        `look_ahead_check_reason: [list, value]` must REFUSE cleanly,
        not TypeError on the substring-match override check."""
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=True,
        )
        backtests = self.vault / "raw" / "backtests"
        import yaml

        fm = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 620.0},
                "look_ahead_check": "suspicious",
                "look_ahead_check_reason": ["bad", "value"],  # non-string
                "last_run": "2026-04-19T10:00:00+00:00",
            },
        }
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(fm, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("look_ahead_check_reason", result.reason)
        self.assertIn("expected non-empty string", result.reason)

    def test_bare_filename_negative_timezone_last_run_accepted(
        self,
    ) -> None:
        """Codex R5 #1 HIGH regression: a bare-form capture whose
        last_run is in a negative-UTC-offset timezone (evening local
        time = early-morning-next-day UTC) must still be accepted.
        Writer uses UTC but a third-party-produced capture (future
        use case) could use local time. Bare form allows ±24h for
        timezone flexibility."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Bare filename dated 2026-04-19, last_run in -07:00 timezone
        # at 18:00 local = 2026-04-20T01:00:00Z next-day UTC. Diff
        # from 2026-04-19T00:00:00Z UTC = 25h. Previously would be
        # flagged as tampered; the 48h bare-filename tolerance fix
        # lets it through.
        fm = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                "last_run": "2026-04-19T18:00:00-07:00",
            },
        }
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(fm, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_unparseable_last_run_string_refuses_in_fallback(
        self,
    ) -> None:
        """Codex R5 #2 HIGH regression: a single capture with
        `last_run: not-a-timestamp` (present but not parseable as
        ISO-8601) must REFUSE. Previously the `last_run is None`
        check would pass the string value through, letting garbage
        evidence clear the gate."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            "tags: [backtest, spy-rotational, raw]\n"
            "date: 2026-04-19\n"
            "type: backtest\n"
            "origin: k2bi-generate\n"
            'up: "[[backtests/index]]"\n'
            "strategy_slug: spy-rotational\n"
            "strategy_commit_sha: abc123\n"
            "backtest:\n"
            "  look_ahead_check: passed\n"
            "  last_run: not-a-timestamp\n"
            "  metrics:\n"
            "    sharpe: 1.0\n"
            "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("unparseable `last_run`", result.reason)

    def test_forged_future_last_run_rejected(self) -> None:
        """Codex R4 #1 HIGH: a tampered capture with a future
        `last_run` (far beyond its filename-encoded timestamp) must
        be rejected, not selected as 'most recent'. Consistency check
        between last_run and filename-embedded timestamp closes the
        forged-evidence attack."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Older capture dated 2026-04-15 but carrying a FORGED
        # last_run way in the future (2099-12-31).
        forged_fm = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-15",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-15", "end": "2026-04-15"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                "last_run": "2099-12-31T23:59:59+00:00",  # FORGED
            },
        }
        (backtests / "2026-04-15_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(forged_fm, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        # Newer valid capture -- the one approval should actually use.
        valid_fm = dict(forged_fm)
        valid_fm["backtest"] = dict(forged_fm["backtest"])
        valid_fm["backtest"]["last_run"] = "2026-04-19T10:00:00+00:00"
        valid_fm["date"] = "2026-04-19"
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(valid_fm, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        # The forged capture would outrank the valid one by last_run,
        # but the consistency check filters it out. Approval proceeds
        # on the valid 2026-04-19 capture.
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_malformed_bare_plus_later_valid_same_day_proceeds(self) -> None:
        """Codex R3 HIGH regression: the MALFORMED first run of the day
        uses bare filename form (no HHMMSS); the LATER valid rerun
        uses HHMMSS form. Filename lex-descending puts the bare form
        first (wrongly claiming it's 'newer'), but chronologically the
        HHMMSS form is later. The malformed-newer check must use
        chronology (filename-embedded timestamp), not lex-sort, or the
        gate would permanently block approval after a same-day
        recovery rerun.
        """
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Malformed BARE-form capture (chronologically the first run,
        # but lex-descending sorts it FIRST -- after HHMMSS form).
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\ninvalid: : broken\n---\n", encoding="utf-8"
        )
        # Valid HHMMSS-form capture (chronologically the second run).
        later_fm = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-19",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-19"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                "last_run": "2026-04-19T14:30:22+00:00",
            },
        }
        (
            backtests
            / "2026-04-19_143022_spy-rotational_backtest.md"
        ).write_text(
            "---\n"
            + yaml.safe_dump(later_fm, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        # Expected: PROCEED on the later valid run. The malformed
        # bare-form capture represents the EARLIER (failed) first run
        # and must not block approval of the later successful rerun.
        self.assertEqual(result.verdict, "PROCEED", result.reason)

    def test_newer_malformed_capture_refuses_not_fallback(self) -> None:
        """Codex R2 #3 HIGH: when the filename-lex-newest capture is
        malformed AND an older valid capture exists, the scanner MUST
        refuse -- NOT silently fall back to the older passed capture.
        A failed / tampered latest run must block approval."""
        _seed_strategy(self.vault, slug="spy-rotational")
        backtests = self.vault / "raw" / "backtests"
        import yaml

        # Older valid passed capture.
        older_fm = {
            "tags": ["backtest", "spy-rotational", "raw"],
            "date": "2026-04-15",
            "type": "backtest",
            "origin": "k2bi-generate",
            "up": "[[backtests/index]]",
            "strategy_slug": "spy-rotational",
            "strategy_commit_sha": "abc123",
            "backtest": {
                "window": {"start": "2024-04-19", "end": "2026-04-15"},
                "source": "yfinance",
                "source_version": "1.3.0",
                "symbol": "SPY",
                "reference_symbol": "SPY",
                "metrics": {"sharpe": 1.0, "total_return_pct": 20.0},
                "look_ahead_check": "passed",
                "look_ahead_check_reason": "",
                "last_run": "2026-04-15T10:00:00+00:00",
            },
        }
        (backtests / "2026-04-15_spy-rotational_backtest.md").write_text(
            "---\n"
            + yaml.safe_dump(older_fm, sort_keys=False)
            + "---\nbody\n",
            encoding="utf-8",
        )
        # Newer but MALFORMED capture.
        (backtests / "2026-04-19_spy-rotational_backtest.md").write_text(
            "---\ninvalid: : yaml\n---\n", encoding="utf-8"
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn(
            "newer backtest capture", result.reason,
            f"expected REFUSE on newer malformed; got {result.reason!r}",
        )
        self.assertIn("2026-04-19", result.reason)

    def test_override_with_empty_justification_refuses(self) -> None:
        """Codex R1 #2 HIGH: override section with the label but empty
        after-label text must refuse. Stale/unfilled override cannot
        clear approval."""
        _seed_strategy(
            self.vault,
            slug="spy-rotational",
            include_backtest_override=False,
        )
        strategy_path = (
            self.vault / "wiki" / "strategies" / "strategy_spy-rotational.md"
        )
        strategy_path.write_text(
            strategy_path.read_text(encoding="utf-8")
            + "\n## Backtest Override\n\n"
            + "Backtest run: 2026-04-19 at raw/backtests/...\n"
            + "Suspicious flag reason: total_return=620%\n"
            + "Why this is acceptable:\n",
            encoding="utf-8",
        )
        _write_backtest_capture(
            self.vault,
            "spy-rotational",
            filename="2026-04-19_spy-rotational_backtest.md",
            look_ahead_check="suspicious",
            look_ahead_check_reason="total_return=620% > 500%",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn(
            "empty `Why this is acceptable:` justification",
            result.reason,
        )

    def test_scan_does_not_match_sibling_slug(self) -> None:
        """Glob `*_{slug}_backtest.md` must be slug-specific so a
        backtest for `spy-rotational-v2` doesn't satisfy the gate for
        `spy-rotational`. ASCII-agnostic suffix match: trailing
        `_backtest.md` anchors this."""
        _seed_strategy(self.vault, slug="spy-rotational")
        # Only a v2 backtest exists; no spy-rotational capture.
        _write_backtest_capture(
            self.vault,
            "spy-rotational-v2",
            filename="2026-04-19_spy-rotational-v2_backtest.md",
            look_ahead_check="passed",
        )
        result = iss.scan_backtests_for_slug(
            "spy-rotational", vault_root=self.vault
        )
        self.assertEqual(result.verdict, "REFUSE")
        self.assertIn("no backtest found", result.reason)


# ---------- BodyFormatTests ----------


class BodyFormatTests(unittest.TestCase):
    """Render-level assertions on the capture body. Covers the [!robot]
    callout + Metrics table + Sanity Gate Result section shapes per
    spec §2.5 example."""

    def test_passed_body_contains_expected_sections(self) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        _seed_strategy(vault)
        try:
            result = ib.run_backtest(
                "spy-rotational",
                vault_root=vault,
                now=_dt.datetime(
                    2026, 4, 19, 10, 0, 0, tzinfo=_dt.timezone.utc
                ),
                price_fetcher=_fixed_fetcher(
                    _normal_series(300), _dt.date(2024, 4, 19)
                ),
                sha_resolver=_mock_sha_resolver(),
                source_version="1.3.0",
            )
            body = result.path.read_text(encoding="utf-8")
            self.assertIn(
                "> [!robot] K2Bi analysis -- yfinance sanity-check backtest",
                body,
            )
            self.assertIn("## Strategy Reference", body)
            self.assertIn("## Sanity Gate Result", body)
            self.assertIn("## Metrics", body)
            self.assertIn("## Limitations", body)
            self.assertIn(
                "| Metric | Value |",
                body,
                "metrics table header missing",
            )
            self.assertIn("| Sharpe (annualised) |", body)
            self.assertIn("| Trades |", body)
            self.assertIn(
                "**Result:** passed",
                body,
                "sanity gate passed line missing",
            )
        finally:
            shutil.rmtree(vault, ignore_errors=True)


# ---------- IndexStubTests ----------


class IndexStubTests(unittest.TestCase):
    def test_index_md_created_on_first_run_if_absent(self) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        # NB: don't call _seed_vault -- we want to verify the skill
        # creates the raw/backtests/ dir + index.md itself.
        (vault / "wiki" / "strategies").mkdir(parents=True)
        _seed_strategy(vault)
        try:
            result = ib.run_backtest(
                "spy-rotational",
                vault_root=vault,
                now=_dt.datetime(
                    2026, 4, 19, 10, 0, 0, tzinfo=_dt.timezone.utc
                ),
                price_fetcher=_fixed_fetcher(
                    _normal_series(300), _dt.date(2024, 4, 19)
                ),
                sha_resolver=_mock_sha_resolver(),
                source_version="1.3.0",
            )
            index = vault / "raw" / "backtests" / "index.md"
            self.assertTrue(index.exists())
            content = index.read_text(encoding="utf-8")
            self.assertIn("raw/backtests", content)
            self.assertIn("YYYY-MM-DD_<slug>_backtest.md", content)
            # Capture file also exists.
            self.assertTrue(result.path.exists())
        finally:
            shutil.rmtree(vault, ignore_errors=True)

    def test_index_md_not_overwritten_on_rerun(self) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        _seed_strategy(vault)
        try:
            custom_index = vault / "raw" / "backtests" / "index.md"
            custom_index.write_text("custom content\n", encoding="utf-8")
            ib.run_backtest(
                "spy-rotational",
                vault_root=vault,
                now=_dt.datetime(
                    2026, 4, 19, 10, 0, 0, tzinfo=_dt.timezone.utc
                ),
                price_fetcher=_fixed_fetcher(
                    _normal_series(300), _dt.date(2024, 4, 19)
                ),
                sha_resolver=_mock_sha_resolver(),
                source_version="1.3.0",
            )
            self.assertEqual(
                custom_index.read_text(encoding="utf-8"),
                "custom content\n",
            )
        finally:
            shutil.rmtree(vault, ignore_errors=True)


# ---------- DeclineSeriesTests (row 3 supplement: exercises 0-trade path) ----------


class DeclineSeriesTests(unittest.TestCase):
    """Monotonically declining prices should produce zero trades and a
    flat equity curve (max_dd_pct == 0.0). The sanity gate treats that
    as suspicious (max_dd > -2). Matches plan-prompt row 3 shape via a
    real end-to-end run."""

    def test_declining_series_trips_max_dd_only(self) -> None:
        vault = Path(tempfile.mkdtemp(prefix="ib_"))
        _seed_vault(vault)
        _seed_strategy(vault)
        try:
            close = _linear_series(300.0, 100.0, 300)
            result = ib.run_backtest(
                "spy-rotational",
                vault_root=vault,
                now=_dt.datetime(
                    2026, 4, 19, 10, 0, 0, tzinfo=_dt.timezone.utc
                ),
                price_fetcher=_fixed_fetcher(close, _dt.date(2024, 4, 19)),
                sha_resolver=_mock_sha_resolver(),
                source_version="1.3.0",
            )
            self.assertEqual(result.metrics.n_trades, 0)
            self.assertEqual(result.metrics.total_return_pct, 0.0)
            self.assertEqual(result.metrics.win_rate_pct, 0.0)
            # max_dd is 0% because the equity curve is flat at 1.0.
            self.assertEqual(result.metrics.max_dd_pct, 0.0)
            # Sanity gate trips on max_dd only.
            self.assertEqual(result.look_ahead_check, "suspicious")
            self.assertIn("max_dd", result.look_ahead_check_reason)
            self.assertNotIn("total_return", result.look_ahead_check_reason)
            self.assertNotIn("win_rate", result.look_ahead_check_reason)
        finally:
            shutil.rmtree(vault, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
