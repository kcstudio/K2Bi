"""Tests for the stage 3 preparation presentation layer (invented previews only)."""
from __future__ import annotations

import json
import unittest
from datetime import timedelta, timezone

from scripts.stage3_paper.preparation import build_preparation
from tests.test_stage3_offline_recovery import context as recovery_context
from tests.test_stage3_offline_recovery import position as recovery_position
from tests.test_stage3_offline_risk import CAPTURED as RISK_CAPTURED
from tests.test_stage3_offline_risk import CONFIG_PATH
from tests.test_stage3_offline_risk import _blob as risk_blob
from tests.test_stage3_offline_risk import _record as risk_record
from tests.test_stage3_offline_risk import _stamp

AS_OF = RISK_CAPTURED.isoformat()

def _config_bytes() -> bytes:
    with open(CONFIG_PATH, "rb") as handle:
        return handle.read()

def _risk_raw(**overrides) -> bytes:
    return risk_blob(risk_record(**overrides))

def _recovery_doc() -> dict:
    doc = recovery_context()
    del doc["captured_at"], doc["expires_at"]
    doc["captured_at"] = _stamp(RISK_CAPTURED - timedelta(seconds=1))
    doc["expires_at"] = _stamp(RISK_CAPTURED + timedelta(minutes=9))
    return doc

def _recovery_raw() -> bytes:
    return json.dumps(_recovery_doc(), default=str).encode()

def _prep(risk_raw=None, recovery_raw=None, **kwargs):
    return build_preparation(risk_raw, recovery_raw, as_of=AS_OF, config_raw=_config_bytes(), **kwargs)

class PreparationTests(unittest.TestCase):
    def test_missing_inputs_wait_withheld(self):
        result = _prep()
        self.assertEqual(result["status"], "WAIT")
        for field in ("candidate", "quantity", "estimated_cost", "estimated_risk"):
            self.assertIsNone(result[field])
        for field in ("executable", "fill_eligible", "approved", "applied"):
            self.assertFalse(result[field])
        self.assertIn("not run or missing", result["reason"])
        self.assertIn("unverified", result["reason"])
        statuses = {row["label"]: row["status"] for row in result["checks"]}
        self.assertEqual(statuses["current_quote"], "unverified")
        self.assertEqual(statuses["approved_paper_rules"], "unverified")
        self.assertEqual(statuses["broker_reconciliation"], "not_run")

    def test_recorded_origin_remains_wait(self):
        risk_raw = _risk_raw(origin="recorded")
        recovery_doc = _recovery_doc()
        recovery_doc["origin"] = "recorded"
        result = _prep(risk_raw, json.dumps(recovery_doc, default=str).encode())
        self.assertEqual(result["status"], "WAIT")
        self.assertIsNone(result["candidate"])
        self.assertIsNone(result["quantity"])

    def test_fixture_clean_filled_preview_flags_false(self):
        result = _prep(_risk_raw(), _recovery_raw())
        self.assertEqual(result["status"], "INVENTED OFFLINE PREVIEW")
        self.assertEqual(result["candidate"], {"symbol": "SPY", "side": "buy"})
        self.assertEqual(result["quantity"], 1)
        self.assertEqual(result["estimated_cost"], "100")
        self.assertEqual(result["estimated_risk"], "1")
        for field in ("executable", "fill_eligible", "approved", "applied"):
            self.assertFalse(result[field])
        self.assertIn("invented", result["reason"])
        self.assertIn("not an order", result["reason"])

    def test_whitelist_rejection_then_not_run(self):
        record = risk_record()
        record["proposal"]["ticker"] = "CALX"
        record["context"]["current_marks"] = {"CALX": "100.25"}
        result = _prep(risk_blob(record), _recovery_raw())
        self.assertEqual(result["status"], "WAIT")
        self.assertEqual(result["risk"]["status"], "fixture_rejected")
        statuses = {row["label"]: row["status"] for row in result["checks"]}
        self.assertTrue(any(value == "rejected" for value in statuses.values()))
        self.assertTrue(any(value == "not_run" for value in statuses.values()))
        self.assertIsNone(result["candidate"])

    def test_recovery_phantom_position_mismatch_blocked(self):
        doc = _recovery_doc()
        doc["broker_positions"] = [recovery_position(qty=10)]
        result = _prep(_risk_raw(), json.dumps(doc, default=str).encode())
        self.assertEqual(result["status"], "WAIT")
        self.assertEqual(result["recovery"]["status"], "fixture_mismatch_refused")
        self.assertIsNone(result["candidate"])

    def test_stale_recovery_holds(self):
        doc = _recovery_doc()
        doc["captured_at"] = _stamp(RISK_CAPTURED - timedelta(minutes=20))
        doc["expires_at"] = _stamp(RISK_CAPTURED - timedelta(minutes=10))
        result = _prep(_risk_raw(), json.dumps(doc, default=str).encode())
        self.assertEqual(result["status"], "WAIT")
        self.assertEqual(result["recovery"]["status"], "not_run")
        self.assertIsNone(result["quantity"])

    def test_mixed_origin_and_account_mismatch_raise(self):
        doc = _recovery_doc()
        doc["origin"] = "recorded"
        with self.assertRaises(ValueError):
            _prep(_risk_raw(origin="fixture"), json.dumps(doc, default=str).encode())
        doc = _recovery_doc()
        doc["account_id"] = "OTHER-ACCOUNT"
        with self.assertRaises(ValueError):
            _prep(_risk_raw(), json.dumps(doc, default=str).encode())

    def test_malformed_raw_raises(self):
        with self.assertRaises(ValueError):
            _prep(b"{not-json", _recovery_raw())
        with self.assertRaises(ValueError):
            _prep(_risk_raw(), b"{not-json")

    def test_determinism_config_fingerprint_and_dates(self):
        risk_raw = _risk_raw()
        recovery_raw = _recovery_raw()
        first = _prep(risk_raw, recovery_raw)
        second = _prep(risk_raw, recovery_raw)
        self.assertEqual(first, second)
        self.assertEqual(first["risk_config"]["sha256"], first["risk"]["config_sha256"])
        risk_dates = first["evidence_dates"]["risk"]
        recovery_dates = first["evidence_dates"]["recovery"]
        self.assertEqual(risk_dates["captured"], RISK_CAPTURED.astimezone(timezone.utc).isoformat())
        self.assertNotEqual(risk_dates["captured"], recovery_dates["captured"])
        self.assertEqual(risk_dates["evaluated"], AS_OF)
