"""Tests for deterministic strategy spec integrity preflight."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.strategies import loader
from scripts.lib import strategy_frontmatter as sf
from scripts.lib.strategy_spec_integrity import strategy_spec_integrity
from tests.test_invest_ship_strategy import _write_strategy


FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _result_bytes(result) -> bytes:
    return json.dumps(
        result.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class StrategySpecIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp(prefix="ssi_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_missing_required_loader_field_refuses_fail_closed(self):
        path = _write_strategy(self.repo, missing_fields=["risk_envelope_pct"])

        result = strategy_spec_integrity(path)

        self.assertFalse(result.ok)
        self.assertTrue(
            any(f["code"] == "loader_validation_error" for f in result.findings)
        )
        self.assertIn("risk_envelope_pct", json.dumps(result.to_dict()))

    def test_present_non_numeric_stop_loss_refuses(self):
        path = _write_strategy(self.repo, order={"stop_loss": "not-a-number"})

        result = strategy_spec_integrity(path)

        self.assertFalse(result.ok)
        self.assertTrue(
            any(f["code"] == "invalid_stop_loss" for f in result.findings)
        )

    def test_real_cdns_fixture_passes_existing_contracts(self):
        source = FIXTURES / "strategy_cdns.md"
        target = self.repo / "wiki" / "strategies" / "strategy_cdns.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        frontmatter = sf.parse(target.read_bytes())

        self.assertIn("forward_guidance_check", frontmatter)
        self.assertIn("stop_loss", frontmatter["order"])

        result = strategy_spec_integrity(target)

        self.assertTrue(result.ok, result.to_dict())
        self.assertEqual(result.findings, ())

    def test_unexpected_exception_returns_deterministic_refusal(self):
        path = _write_strategy(self.repo)

        with patch.object(loader, "load_document", side_effect=TypeError("unstable 0xabc")):
            result = strategy_spec_integrity(path)

        self.assertFalse(result.ok)
        self.assertEqual(len(result.findings), 1)
        self.assertEqual(result.findings[0]["code"], "integrity_exception")
        self.assertEqual(
            result.findings[0]["message"],
            "unexpected TypeError during integrity check",
        )

    def test_output_is_byte_identical_across_runs(self):
        path = _write_strategy(self.repo)

        outputs = [_result_bytes(strategy_spec_integrity(path)) for _ in range(5)]

        self.assertEqual(outputs, [outputs[0]] * 5)

    def test_exception_path_output_is_byte_identical_across_runs(self):
        # The fail-closed guard must be deterministic on the EXCEPTION path too,
        # not just the happy path: an unstable exception payload must never leak
        # into the refusal, so the serialized result is byte-identical run to run.
        path = _write_strategy(self.repo)

        with patch.object(loader, "load_document", side_effect=TypeError("unstable 0xabc")):
            outputs = [_result_bytes(strategy_spec_integrity(path)) for _ in range(5)]

        self.assertEqual(outputs, [outputs[0]] * 5)
