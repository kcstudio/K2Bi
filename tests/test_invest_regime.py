"""Tests for invest-regime manual classification MVP."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from scripts.lib import invest_regime
from scripts.lib.invest_regime import VALID_BANDS, classify
from scripts.lib.strategy_frontmatter import parse as parse_frontmatter


class BandValidationTests(unittest.TestCase):
    def test_rejects_unknown_band(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            classify("panic", "markets are crashing", vault_root=Path("/tmp"))
        msg = str(ctx.exception)
        self.assertIn("panic", msg)
        for band in VALID_BANDS:
            self.assertIn(band, msg)

    def test_rejects_empty_reason(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            classify("bear", "", vault_root=Path("/tmp"))
        self.assertIn("reason", str(ctx.exception).lower())

    def test_rejects_whitespace_only_reason(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            classify("bear", "   ", vault_root=Path("/tmp"))
        self.assertIn("reason", str(ctx.exception).lower())

    def test_accepts_all_valid_bands(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            for band in VALID_BANDS:
                path = classify(band, f"reason for {band}", vault_root=vault)
                self.assertTrue(path.exists())
                fm = parse_frontmatter(path.read_bytes())
                self.assertEqual(fm.get("regime"), band)


class AtomicWriteTests(unittest.TestCase):
    def test_produces_correct_frontmatter_and_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            path = classify(
                "bear",
                "VIX spiked and credit spreads widened. Risk-off posture warranted.",
                vault_root=vault,
            )
            self.assertTrue(path.exists())
            content = path.read_text(encoding="utf-8")
            self.assertIn("regime: bear", content)
            self.assertIn("type: regime", content)
            self.assertIn("origin: keith", content)
            self.assertIn("VIX spiked and credit spreads widened", content)
            self.assertIn("## Indicator Readings", content)
            self.assertIn("| Fear & Greed Index | n/a |", content)

    def test_atomic_write_rollback_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            # Seed an existing current.md
            target = vault / "wiki" / "regimes" / "current.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            original = "---\ntags: [regime, k2bi]\nregime: neutral\n---\n\nOld body\n"
            target.write_text(original, encoding="utf-8")

            def boom(*args, **kwargs):
                raise OSError("simulated write failure")

            with patch(
                "scripts.lib.invest_regime.atomic_write_bytes",
                side_effect=boom,
            ):
                with self.assertRaises(OSError):
                    classify("bear", "reason", vault_root=vault)

            self.assertEqual(target.read_text(encoding="utf-8"), original)


class FrontmatterShapeTests(unittest.TestCase):
    def test_all_required_keys_present(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            path = classify("bull", "Earnings strong. Risk-on.", vault_root=vault)
            fm = parse_frontmatter(path.read_bytes())
            required = {
                "tags",
                "date",
                "type",
                "origin",
                "up",
                "regime",
                "classified_date",
                "reasoning_summary",
            }
            self.assertTrue(required.issubset(set(fm.keys())))
            self.assertEqual(fm["type"], "regime")
            self.assertEqual(fm["origin"], "keith")
            self.assertEqual(fm["up"], "[[index]]")


class BodyShapeTests(unittest.TestCase):
    def test_body_without_indicators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            path = classify("neutral", "No strong directional edge.", vault_root=vault)
            text = path.read_text(encoding="utf-8")
            self.assertIn("# Current Regime: neutral", text)
            self.assertIn("No strong directional edge.", text)
            self.assertIn("| Fear & Greed Index | n/a |", text)
            self.assertIn("| VIX | n/a |", text)
            self.assertIn("| VVIX | n/a |", text)
            self.assertIn("| Sector Breadth | n/a |", text)

    def test_body_with_indicators(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            indicators = {"fear_greed": 32, "vix": 18.4, "vvix": 95.2}
            path = classify(
                "bull",
                "Sentiment elevated but not euphoric.",
                indicators=indicators,
                vault_root=vault,
            )
            text = path.read_text(encoding="utf-8")
            self.assertIn("| Fear & Greed Index | 32 |", text)
            self.assertIn("| VIX | 18.4 |", text)
            self.assertIn("| VVIX | 95.2 |", text)
            self.assertIn("| Sector Breadth | n/a |", text)


class ReclassificationTests(unittest.TestCase):
    def test_overwrite_replaces_does_not_append(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            classify("bear", "First call: bear market.", vault_root=vault)
            classify("bull", "Second call: bull market.", vault_root=vault)
            text = vault.joinpath("wiki", "regimes", "current.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("bull", text)
            self.assertNotIn("bear market", text)
            # Verify only one frontmatter block.
            self.assertEqual(text.count("---\n"), 2)


class IndicatorJsonParsingTests(unittest.TestCase):
    def test_cli_rejects_invalid_json(self) -> None:
        rc = invest_regime.main(
            ["classify", "bull", "--reason", "test", "--indicators", "not-json"]
        )
        self.assertEqual(rc, 1)

    def test_cli_rejects_non_dict_json(self) -> None:
        rc = invest_regime.main(
            ["classify", "bull", "--reason", "test", "--indicators", "[1, 2, 3]"]
        )
        self.assertEqual(rc, 1)

    def test_cli_valid_json_populates_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            with patch.dict(
                "os.environ", {"K2BI_VAULT_ROOT": str(vault)}
            ):
                rc = invest_regime.main(
                    [
                        "classify",
                        "bull",
                        "--reason",
                        "Risk-on.",
                        "--indicators",
                        '{"vix": 18.4, "sector_breadth": 72}',
                    ]
                )
            self.assertEqual(rc, 0)
            path = vault / "wiki" / "regimes" / "current.md"
            text = path.read_text(encoding="utf-8")
            self.assertIn("| VIX | 18.4 |", text)
            self.assertIn("| Sector Breadth | 72 |", text)
            self.assertIn("| Fear & Greed Index | n/a |", text)


class ReasoningSummaryTests(unittest.TestCase):
    def test_first_sentence_truncation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            reason = "First sentence here. Second sentence ignored."
            path = classify("neutral", reason, vault_root=vault)
            fm = parse_frontmatter(path.read_bytes())
            self.assertEqual(fm["reasoning_summary"], "First sentence here.")

    def test_first_sentence_earliest_terminator(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            reason = "Risk-off! Credit spreads are widening."
            path = classify("bear", reason, vault_root=vault)
            fm = parse_frontmatter(path.read_bytes())
            self.assertEqual(fm["reasoning_summary"], "Risk-off!")

    def test_long_sentence_truncated_to_120(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            vault = Path(tmp) / "K2Bi-Vault"
            reason = "A" * 200
            path = classify("neutral", reason, vault_root=vault)
            fm = parse_frontmatter(path.read_bytes())
            self.assertTrue(fm["reasoning_summary"].endswith("..."))
            self.assertLessEqual(len(fm["reasoning_summary"]), 123)


if __name__ == "__main__":
    unittest.main()
