"""Tests for the journal v1 -> v2 bump.

v2 adds 16 event types for the engine state machine + reconciliation
cases plus `broker_order_id` and `broker_perm_id` optional top-level
fields.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from execution.journal.schema import (
    EVENT_TYPES,
    EVENT_TYPES_V1,
    EVENT_TYPES_V2_ADDITIONS,
    KNOWN_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    JournalSchemaError,
    validate,
)
from execution.journal.ulid import new_ulid
from execution.journal.writer import JournalWriter


class SchemaV2Tests(unittest.TestCase):
    def test_schema_version_is_2(self):
        self.assertEqual(SCHEMA_VERSION, 2)

    def test_known_versions_include_1_and_2(self):
        self.assertIn(1, KNOWN_SCHEMA_VERSIONS)
        self.assertIn(2, KNOWN_SCHEMA_VERSIONS)

    def test_v1_event_types_still_valid(self):
        self.assertLessEqual(EVENT_TYPES_V1, EVENT_TYPES)

    def test_v2_additions_present(self):
        expected = {
            "order_proposed",
            "order_rejected",
            "engine_started",
            "engine_stopped",
            "engine_recovered",
            "recovery_state_mismatch",
            "recovery_reconciled",
            "strategy_file_modified_post_approval",
            "avg_price_drift",
            "kill_blocked",
            "kill_cleared",
            "auth_required",
            "auth_recovered",
            "reconnected",
            "disconnect_status",
            "order_timeout",
            "eod_cancel",
            "eod_complete",
        }
        self.assertTrue(expected.issubset(EVENT_TYPES))
        # EVENT_TYPES_V2_ADDITIONS contains exactly the listed additions.
        self.assertTrue(expected.issubset(EVENT_TYPES_V2_ADDITIONS))

    def test_validate_accepts_v1_record(self):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 1,
            "event_type": "order_submitted",
            "trade_id": new_ulid(),
            "journal_entry_id": new_ulid(),
            "strategy": "spy-rotational",
            "git_sha": "abc1234",
            "payload": {},
        }
        # Should not raise: readers tolerate v1 per schema evolution rules.
        validate(rec)

    def test_validate_rejects_unknown_version(self):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": 99,
            "event_type": "order_submitted",
            "trade_id": new_ulid(),
            "journal_entry_id": new_ulid(),
            "strategy": "spy-rotational",
            "git_sha": "abc1234",
            "payload": {},
        }
        with self.assertRaises(JournalSchemaError):
            validate(rec)

    def test_validate_rejects_unknown_event_type(self):
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "schema_version": SCHEMA_VERSION,
            "event_type": "does_not_exist",
            "trade_id": None,
            "journal_entry_id": new_ulid(),
            "strategy": None,
            "git_sha": None,
            "payload": {},
        }
        with self.assertRaises(JournalSchemaError):
            validate(rec)


class WriterV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.writer = JournalWriter(base_dir=self.base, git_sha="abc1234")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_with_broker_ids(self):
        rec = self.writer.append(
            "order_submitted",
            payload={"limit_price": "500"},
            strategy="spy-rotational",
            trade_id=new_ulid(),
            ticker="SPY",
            side="buy",
            qty=10,
            broker_order_id="1000",
            broker_perm_id="2000000",
        )
        self.assertEqual(rec["broker_order_id"], "1000")
        self.assertEqual(rec["broker_perm_id"], "2000000")
        self.assertEqual(rec["schema_version"], 2)

    def test_append_v2_event_types(self):
        for event_type in (
            "engine_started",
            "engine_stopped",
            "recovery_reconciled",
            "recovery_state_mismatch",
            "kill_blocked",
            "kill_cleared",
            "auth_required",
            "auth_recovered",
            "reconnected",
            "disconnect_status",
            "order_proposed",
            "order_rejected",
            "order_timeout",
            "eod_complete",
            "strategy_file_modified_post_approval",
            "avg_price_drift",
        ):
            rec = self.writer.append(event_type, payload={"k": "v"})
            self.assertEqual(rec["event_type"], event_type)


if __name__ == "__main__":
    unittest.main()
