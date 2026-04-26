"""Tests for invest-screen Stage-2 enricher (m2.13)."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.lib.invest_screen import (
    _build_manual_stub,
    _derive_rating_band,
    _enrich_frontmatter,
    _load_bands,
    _score_symbol,
    _validate_llm_output,
    _validate_stage1_presence,
    _validate_stage1_status,
    enrich,
    main,
    manual_promote,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_llm_response(
    *,
    trend_alignment: int = 8,
    momentum: int = 6,
    volume_pattern: int = 6,
    pattern_quality: int = 7,
    key_level_proximity: int = 6,
    valuation: int = 6,
    growth: int = 7,
    profitability: int = 6,
    balance_sheet: int = 5,
    analyst: int = 5,
    catalyst_clarity: int = 6,
    timeline: int = 5,
    sentiment: int = 4,
    rr_setup: int = 5,
) -> dict:
    """Build a valid LLM response dict with defaults that sum correctly."""
    technical = trend_alignment + momentum + volume_pattern + pattern_quality + key_level_proximity
    fundamentals = valuation + growth + profitability + balance_sheet + analyst
    catalyst = catalyst_clarity + timeline + sentiment + rr_setup
    quick_score = technical + fundamentals + catalyst

    band = "A"
    if quick_score < 35:
        band = "F"
    elif quick_score < 50:
        band = "D"
    elif quick_score < 65:
        band = "C"
    elif quick_score < 80:
        band = "B"

    return {
        "sub_factors": {
            "trend_alignment": trend_alignment,
            "momentum": momentum,
            "volume_pattern": volume_pattern,
            "pattern_quality": pattern_quality,
            "key_level_proximity": key_level_proximity,
            "valuation": valuation,
            "growth": growth,
            "profitability": profitability,
            "balance_sheet": balance_sheet,
            "analyst": analyst,
            "catalyst_clarity": catalyst_clarity,
            "timeline": timeline,
            "sentiment": sentiment,
            "rr_setup": rr_setup,
        },
        "quick_score_breakdown": {
            "technical": technical,
            "fundamentals": fundamentals,
            "catalyst": catalyst,
        },
        "quick_score": quick_score,
        "rating_band": band,
        "scoring_notes": "Test scoring notes.",
    }


def _make_stage1_frontmatter(symbol: str = "LRCX") -> str:
    return f"""---
tags: [watchlist, k2bi]
date: 2026-04-25
type: watchlist
origin: k2bi-extract
up: "[[index]]"
symbol: {symbol}
status: promoted
schema_version: 1
narrative_provenance: "[[macro-themes/theme_ai-compute-demand]]"
reasoning_chain: "AI -> chips -> LRCX"
citation_url: "https://example.com"
order_of_beneficiary: 2
ark_6_metric_initial_scores:
  people_culture: 8
  rd_execution: 9
  moat: 9
  product_leadership: 8
  thesis_risk: 7
  valuation: 6
---

# Watchlist: {symbol}

Promoted from [[macro-themes/theme_ai-compute-demand]] on 2026-04-25.

**Reasoning chain:** AI -> chips -> LRCX

## Linked notes

- [[macro-themes/theme_ai-compute-demand]]
- [[index]]
"""


# ---------------------------------------------------------------------------
# Band loading + rating band derivation
# ---------------------------------------------------------------------------


class LoadBandsTests(unittest.TestCase):
    def test_load_bands_structure(self):
        bands = _load_bands()
        self.assertEqual(bands["band_definition_version"], 1)
        self.assertIn("technical", bands["component_max"])
        self.assertEqual(len(bands["sub_factors"]), 14)
        self.assertIn("A", bands["rating_bands"])


class DeriveRatingBandTests(unittest.TestCase):
    def test_band_a(self):
        bands = _load_bands()
        self.assertEqual(_derive_rating_band(85, bands), "A")
        self.assertEqual(_derive_rating_band(100, bands), "A")
        self.assertEqual(_derive_rating_band(80, bands), "A")

    def test_band_b(self):
        bands = _load_bands()
        self.assertEqual(_derive_rating_band(65, bands), "B")
        self.assertEqual(_derive_rating_band(79, bands), "B")

    def test_band_c(self):
        bands = _load_bands()
        self.assertEqual(_derive_rating_band(50, bands), "C")
        self.assertEqual(_derive_rating_band(64, bands), "C")

    def test_band_d(self):
        bands = _load_bands()
        self.assertEqual(_derive_rating_band(35, bands), "D")
        self.assertEqual(_derive_rating_band(49, bands), "D")

    def test_band_f(self):
        bands = _load_bands()
        self.assertEqual(_derive_rating_band(0, bands), "F")
        self.assertEqual(_derive_rating_band(34, bands), "F")

    def test_out_of_range_raises(self):
        bands = _load_bands()
        with self.assertRaises(ValueError):
            _derive_rating_band(101, bands)
        with self.assertRaises(ValueError):
            _derive_rating_band(-1, bands)


# ---------------------------------------------------------------------------
# LLM output validation
# ---------------------------------------------------------------------------


class ValidateLlmOutputTests(unittest.TestCase):
    def test_valid_response(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        result = _validate_llm_output(resp, bands)
        self.assertEqual(result["quick_score"], resp["quick_score"])
        self.assertEqual(result["rating_band"], resp["rating_band"])

    def test_missing_sub_factor(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        del resp["sub_factors"]["trend_alignment"]
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("trend_alignment", str(ctx.exception))

    def test_extra_sub_factor(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        resp["sub_factors"]["extra"] = 5
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("extra", str(ctx.exception))

    def test_out_of_range_sub_factor(self):
        bands = _load_bands()
        resp = _make_valid_llm_response(trend_alignment=11)
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("trend_alignment=11", str(ctx.exception))

    def test_technical_sum_mismatch(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        resp["quick_score_breakdown"]["technical"] = 99
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("Technical sum mismatch", str(ctx.exception))

    def test_fundamental_sum_mismatch(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        resp["quick_score_breakdown"]["fundamentals"] = 99
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("Fundamental sum mismatch", str(ctx.exception))

    def test_catalyst_sum_mismatch(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        resp["quick_score_breakdown"]["catalyst"] = 99
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("Catalyst sum mismatch", str(ctx.exception))

    def test_quick_score_mismatch(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        resp["quick_score"] = resp["quick_score"] + 1
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("Quick score mismatch", str(ctx.exception))

    def test_rating_band_mismatch(self):
        bands = _load_bands()
        resp = _make_valid_llm_response()
        # Force a B-band score but claim A
        resp["sub_factors"] = {
            "trend_alignment": 5,
            "momentum": 4,
            "volume_pattern": 3,
            "pattern_quality": 4,
            "key_level_proximity": 3,
            "valuation": 4,
            "growth": 4,
            "profitability": 3,
            "balance_sheet": 3,
            "analyst": 3,
            "catalyst_clarity": 4,
            "timeline": 3,
            "sentiment": 2,
            "rr_setup": 3,
        }
        resp["quick_score_breakdown"] = {
            "technical": 19,
            "fundamentals": 17,
            "catalyst": 12,
        }
        resp["quick_score"] = 48
        resp["rating_band"] = "A"
        with self.assertRaises(ValueError) as ctx:
            _validate_llm_output(resp, bands)
        self.assertIn("Rating band mismatch", str(ctx.exception))


# ---------------------------------------------------------------------------
# Stage-1 validation
# ---------------------------------------------------------------------------


class ValidateStage1Tests(unittest.TestCase):
    def test_valid_stage1(self):
        fm = {
            "symbol": "LRCX",
            "status": "promoted",
            "narrative_provenance": "...",
            "reasoning_chain": "...",
            "citation_url": "...",
            "order_of_beneficiary": 2,
            "ark_6_metric_initial_scores": {},
        }
        # Should not raise
        _validate_stage1_presence(fm, Path("/vault/wiki/watchlist/LRCX.md"))
        _validate_stage1_status(fm, Path("/vault/wiki/watchlist/LRCX.md"))

    def test_missing_field_raises(self):
        fm = {
            "symbol": "LRCX",
            "status": "promoted",
            "narrative_provenance": "...",
            "reasoning_chain": "...",
            "citation_url": "...",
            "order_of_beneficiary": 2,
            # missing ark_6_metric_initial_scores
        }
        with self.assertRaises(ValueError) as ctx:
            _validate_stage1_presence(fm, Path("/vault/wiki/watchlist/LRCX.md"))
        self.assertIn("ark_6_metric_initial_scores", str(ctx.exception))

    def test_wrong_status_raises(self):
        fm = {
            "symbol": "LRCX",
            "status": "screened",
            "narrative_provenance": "...",
            "reasoning_chain": "...",
            "citation_url": "...",
            "order_of_beneficiary": 2,
            "ark_6_metric_initial_scores": {},
        }
        with self.assertRaises(ValueError) as ctx:
            _validate_stage1_status(fm, Path("/vault/wiki/watchlist/LRCX.md"))
        self.assertIn("expected status 'promoted'", str(ctx.exception))


# ---------------------------------------------------------------------------
# Frontmatter enrichment
# ---------------------------------------------------------------------------


class EnrichFrontmatterTests(unittest.TestCase):
    def test_preserves_stage1_and_injects_stage2(self):
        original = _make_stage1_frontmatter("LRCX").encode("utf-8")
        stage2 = {
            "quick_score": 75,
            "quick_score_breakdown": {"technical": 30, "fundamentals": 25, "catalyst": 20},
            "sub_factors": {"trend_alignment": 8},
            "rating_band": "B",
            "band_definition_version": 1,
        }
        result = _enrich_frontmatter(original, stage2)
        text = result.decode("utf-8")
        self.assertIn("status: screened", text)
        self.assertIn("quick_score: 75", text)
        self.assertIn("rating_band: B", text)
        # Stage-1 fields preserved
        self.assertIn("symbol: LRCX", text)
        self.assertIn("narrative_provenance:", text)
        self.assertIn("reasoning_chain:", text)
        # Body preserved
        self.assertIn("# Watchlist: LRCX", text)
        self.assertIn("Promoted from", text)

    def test_missing_opening_fence_raises(self):
        with self.assertRaises(ValueError):
            _enrich_frontmatter(b"no frontmatter\n", {})

    def test_missing_closing_fence_raises(self):
        with self.assertRaises(ValueError):
            _enrich_frontmatter(b"---\nfoo: bar\n", {})


# ---------------------------------------------------------------------------
# Manual stub builder
# ---------------------------------------------------------------------------


class BuildManualStubTests(unittest.TestCase):
    def test_structure(self):
        stage2 = {
            "quick_score": 60,
            "quick_score_breakdown": {"technical": 20, "fundamentals": 20, "catalyst": 20},
            "sub_factors": {"trend_alignment": 5},
            "rating_band": "C",
            "band_definition_version": 1,
        }
        content = _build_manual_stub("AAPL", stage2, "2026-04-26")
        text = content.decode("utf-8")
        self.assertIn("status: screened", text)
        self.assertIn("symbol: AAPL", text)
        self.assertIn("narrative_provenance:", text)
        self.assertIn("quick_score: 60", text)
        self.assertIn("# Watchlist: AAPL", text)
        self.assertIn("Manually promoted on 2026-04-26.", text)


# ---------------------------------------------------------------------------
# Core enrich flow
# ---------------------------------------------------------------------------


class EnrichFlowTests(unittest.TestCase):
    def _mock_call_fn(self, symbol: str, ctx: str, reason: str) -> dict:
        return _make_valid_llm_response()

    def test_enrich_success(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            watchlist_path.write_text(_make_stage1_frontmatter("LRCX"))

            result = enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)
            self.assertEqual(result, watchlist_path)

            content = watchlist_path.read_text()
            self.assertIn("status: screened", content)
            self.assertIn("quick_score:", content)
            self.assertIn("rating_band:", content)
            self.assertIn("band_definition_version: 1", content)

    def test_enrich_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            watchlist_path.write_text(_make_stage1_frontmatter("LRCX"))

            enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)
            result = enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)
            self.assertEqual(result, watchlist_path)

    def test_enrich_re_enrich(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            watchlist_path.write_text(_make_stage1_frontmatter("LRCX"))

            enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)
            result = enrich("LRCX", vault_root=td_path, re_enrich=True, call_fn=self._mock_call_fn)
            self.assertEqual(result, watchlist_path)
            # Should still be screened and contain stage2
            content = watchlist_path.read_text()
            self.assertIn("status: screened", content)

    def test_enrich_re_enrich_on_non_screened_raises(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            watchlist_path.write_text(_make_stage1_frontmatter("LRCX"))

            with self.assertRaises(ValueError) as ctx:
                enrich("LRCX", vault_root=td_path, re_enrich=True, call_fn=self._mock_call_fn)
            self.assertIn("--re-enrich requires status 'screened'", str(ctx.exception))

    def test_enrich_missing_stage1_field_raises(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            bad_fm = _make_stage1_frontmatter("LRCX").replace("reasoning_chain:", "x_reasoning_chain:")
            watchlist_path.write_text(bad_fm)

            with self.assertRaises(ValueError) as ctx:
                enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)
            self.assertIn("reasoning_chain", str(ctx.exception))

    def test_enrich_wrong_status_raises(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            bad_fm = _make_stage1_frontmatter("LRCX").replace("status: promoted", "status: draft")
            watchlist_path.write_text(bad_fm)

            with self.assertRaises(ValueError) as ctx:
                enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)
            self.assertIn("expected status 'promoted'", str(ctx.exception))

    def test_enrich_file_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            with self.assertRaises(FileNotFoundError):
                enrich("FAKE", vault_root=td_path, call_fn=self._mock_call_fn)

    def test_enrich_index_failure_rolls_back_watchlist(self):
        """If index update fails after watchlist write, rollback restores original."""
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "LRCX.md"
            watchlist_path.parent.mkdir(parents=True)
            original_text = _make_stage1_frontmatter("LRCX")
            watchlist_path.write_text(original_text)

            with patch(
                "scripts.lib.invest_screen._update_watchlist_index",
                side_effect=OSError("index failure"),
            ):
                with self.assertRaises(OSError):
                    enrich("LRCX", vault_root=td_path, call_fn=self._mock_call_fn)

            # Original should be restored
            self.assertEqual(watchlist_path.read_text(), original_text)


# ---------------------------------------------------------------------------
# Manual promote flow
# ---------------------------------------------------------------------------


class ManualPromoteFlowTests(unittest.TestCase):
    def _mock_call_fn(self, symbol: str, ctx: str, reason: str) -> dict:
        return _make_valid_llm_response()

    def test_manual_promote_success(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            result = manual_promote(
                "AAPL", reason="Free cash flow machine", vault_root=td_path, call_fn=self._mock_call_fn
            )
            self.assertTrue(result.exists())
            content = result.read_text()
            self.assertIn("status: screened", content)
            self.assertIn("symbol: AAPL", content)
            self.assertIn("quick_score:", content)
            self.assertIn("narrative_provenance:", content)

    def test_manual_promote_without_reason(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            result = manual_promote("AAPL", vault_root=td_path, call_fn=self._mock_call_fn)
            self.assertTrue(result.exists())
            content = result.read_text()
            self.assertIn("status: screened", content)

    def test_manual_promote_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            watchlist_path = td_path / "wiki" / "watchlist" / "AAPL.md"
            watchlist_path.parent.mkdir(parents=True)
            watchlist_path.write_text("---\nstatus: promoted\n---\n")

            with self.assertRaises(FileExistsError):
                manual_promote("AAPL", vault_root=td_path, call_fn=self._mock_call_fn)


# ---------------------------------------------------------------------------
# LLM retry logic
# ---------------------------------------------------------------------------


class LlmRetryTests(unittest.TestCase):
    def test_retry_on_invalid_then_success(self):
        call_count = 0

        def flaky_call(symbol: str, ctx: str, reason: str) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Invalid: out of range
                return _make_valid_llm_response(trend_alignment=99)
            return _make_valid_llm_response()

        result = _score_symbol("LRCX", "ctx", "none", flaky_call)
        self.assertEqual(call_count, 2)
        self.assertEqual(result["rating_band"], "A")

    def test_raises_after_max_retries(self):
        def always_bad(symbol: str, ctx: str, reason: str) -> dict:
            return _make_valid_llm_response(trend_alignment=99)

        with self.assertRaises(ValueError) as ctx:
            _score_symbol("LRCX", "ctx", "none", always_bad)
        self.assertIn("LLM scoring failed after 3 attempts", str(ctx.exception))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class CliTests(unittest.TestCase):
    @patch("scripts.lib.invest_screen.enrich")
    def test_cli_enrich(self, mock_enrich):
        mock_enrich.return_value = Path("/vault/wiki/watchlist/LRCX.md")
        code = main(["--enrich", "LRCX"])
        self.assertEqual(code, 0)
        mock_enrich.assert_called_once_with("LRCX", re_enrich=False)

    @patch("scripts.lib.invest_screen.manual_promote")
    def test_cli_manual_promote(self, mock_manual):
        mock_manual.return_value = Path("/vault/wiki/watchlist/AAPL.md")
        code = main(["--manual-promote", "AAPL", "--reason", "test"])
        self.assertEqual(code, 0)
        mock_manual.assert_called_once_with("AAPL", reason="test")

    def test_cli_mutual_exclusion(self):
        with self.assertRaises(SystemExit):
            main(["--enrich", "LRCX", "--manual-promote", "AAPL"])

    def test_cli_no_args(self):
        code = main([])
        self.assertEqual(code, 1)

    @patch("scripts.lib.invest_screen.enrich")
    def test_cli_re_enrich(self, mock_enrich):
        mock_enrich.return_value = Path("/vault/wiki/watchlist/LRCX.md")
        code = main(["--enrich", "LRCX", "--re-enrich"])
        self.assertEqual(code, 0)
        mock_enrich.assert_called_once_with("LRCX", re_enrich=True)


if __name__ == "__main__":
    unittest.main()
