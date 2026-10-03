"""Focused tests for the strict offline account readiness parser."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.stage3_paper import account_readiness as mod  # noqa: E402
from scripts.stage3_paper.account_readiness import (  # noqa: E402
    CAPTURE_SOURCE_PATH,
    KIND,
    PROOF_KIND,
    REQUESTED_ACCOUNT,
    parse_account_readiness,
)

_BASE = datetime(2024, 5, 1, 12, 0, 0, tzinfo=timezone.utc)


def iso(offset=0):
    return (_BASE + timedelta(seconds=offset)).isoformat()


def canon(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def encode(obj):
    return json.dumps(obj, separators=(",", ":")).encode()


def request(client_id=90):
    return {
        "account_updates": "reqAccountUpdates",
        "open_orders": "reqAllOpenOrders",
        "startup_fetch": 0,
        "timeout_seconds": 15,
        "client_id": client_id,
        "bind_orders": False,
        "market_data": False,
    }


def account(end=True, ready=True, values=None):
    if values is None:
        values = [
            {"tag": "CashBalance", "currency": "USD", "value": "100.0"},
            {"tag": "SettledCash", "currency": "USD", "value": "50.0"},
            {"tag": "$LEDGER-TotalCashBalance", "currency": "BASE", "value": "100.0"},
        ]
    return {
        "requested_at": iso(1),
        "ended_at": iso(3) if end else None,
        "end_received": end,
        "account_ready": ready if end else None,
        "values": values if end else None,
    }


def order_row(**over):
    row = {
        "account_id": REQUESTED_ACCOUNT,
        "perm_id": 1,
        "client_id": 0,
        "order_id": 5,
        "symbol": "AAPL",
        "sec_type": "STK",
        "currency": "USD",
        "side": "BUY",
        "order_type": "LMT",
        "quantity": "10",
        "limit_price": "12.5",
        "status": "Submitted",
    }
    row.update(over)
    return row


def orders(end=True, rows=None):
    if rows is None:
        rows = [order_row()]
    return {
        "requested_at": iso(2),
        "ended_at": iso(4) if end else None,
        "end_received": end,
        "rows": rows if end else None,
        "unattributed_count": 0,
        "other_account_count": 0,
    }


def snapshot(
    origin="recorded",
    status="complete",
    reason="",
    account_obj=None,
    orders_obj=None,
    client_id=90,
    account_id=REQUESTED_ACCOUNT,
):
    if account_obj is None and status != "unavailable":
        account_obj = account(end=(status == "complete"))
    if orders_obj is None and status != "unavailable":
        orders_obj = orders(end=(status == "complete"))
    return {
        "schema": 1,
        "kind": KIND,
        "origin": origin,
        "source": "IBKR paper Gateway",
        "connection_mode": "readonly",
        "gateway_port": 4002,
        "requested_account_id": REQUESTED_ACCOUNT,
        "account_id": None if status == "unavailable" else account_id,
        "started_at": iso(0),
        "completed_at": iso(5),
        "status": status,
        "reason": reason,
        "request": request(client_id=client_id),
        "account": None if status == "unavailable" else account_obj,
        "orders": None if status == "unavailable" else orders_obj,
        "restrictions": {"state": "unknown", "reason": "not captured"},
    }


class _SourceHarness(unittest.TestCase):
    """Install a sibling capture source, then exercise proofs against it."""

    def setUp(self):
        self._saved = mod.CAPTURE_SOURCE_PATH
        self._dir = tempfile.TemporaryDirectory()
        fake = Path(self._dir.name) / "account_capture.py"
        fake.write_bytes(b"# fixture capture source\ndef main(ib=None, fetch_fields=None, env=None):\n    return 0\n")
        self.source_bytes = fake.read_bytes()
        mod.CAPTURE_SOURCE_PATH = fake

    def tearDown(self):
        mod.CAPTURE_SOURCE_PATH = self._saved
        self._dir.cleanup()

    def proof(self, raw_obj, captured_at=None):
        raw = encode(raw_obj)
        blob = {
            "schema": 1,
            "kind": PROOF_KIND,
            "raw_sha256": sha(raw),
            "source_sha256": sha(self.source_bytes),
            "request_sha256": canon(raw_obj["request"]),
            "returncode": 0,
            "captured_at": captured_at or raw_obj["completed_at"],
        }
        return raw, encode(blob)


class ParseFamilyTests(_SourceHarness):

    def test_canonical_complete_with_proof(self):
        obj = snapshot()
        raw, proof = self.proof(obj)
        out = parse_account_readiness(raw, as_of=iso(30), proof_raw=proof)
        self.assertEqual(out["status"], "complete")
        self.assertEqual(out["provenance"], "recorded_request_verified")
        self.assertEqual(out["freshness"], "fresh")
        self.assertEqual(out["orders_state"], "visible_api_orders")
        self.assertEqual(out["order_count"], 1)
        self.assertIsNone(out["settled_usd"])
        self.assertEqual(out["settled_usd_state"], "unknown")
        self.assertFalse(out["executable"])
        self.assertFalse(out["fill_eligible"])
        self.assertEqual(out["reserved_cash"], None)
        self.assertEqual(out["restrictions_state"], "unknown")
        self.assertIsInstance(out["raw_sha256"], str)
        self.assertIsInstance(out["proof_sha256"], str)

    def test_complete_with_proof_empty_rows_no_visible(self):
        obj = snapshot()
        obj["orders"]["rows"] = []
        raw, proof = self.proof(obj)
        out = parse_account_readiness(raw, as_of=iso(30), proof_raw=proof)
        self.assertEqual(out["orders_state"], "no_visible_api_orders")
        self.assertEqual(out["order_count"], 0)

    def test_visible_only_when_coverage_conditions(self):
        base = snapshot()
        raw, proof = self.proof(base)
        self.assertEqual(
            parse_account_readiness(raw, as_of=iso(30), proof_raw=proof)["orders_state"],
            "visible_api_orders",
        )
        # unverified proof -> unknown
        out = parse_account_readiness(raw, as_of=iso(30))
        self.assertEqual(out["orders_state"], "unknown")
        self.assertEqual(out["provenance"], "unverified")

    def test_stale_and_false_ready_and_missing_end_never_no_visible(self):
        cases = []
        stale = snapshot()
        cases.append((stale, iso(5000)))
        partial = snapshot(
            status="partial",
            reason="orders",
            account_obj=account(end=True),
            orders_obj=orders(end=False),
        )
        cases.append((partial, iso(30)))
        not_ready = snapshot()
        not_ready["account"]["account_ready"] = False
        cases.append((not_ready, iso(30)))
        for obj, age in cases:
            raw, proof = self.proof(obj)
            out = parse_account_readiness(raw, as_of=age, proof_raw=proof)
            self.assertNotEqual(out["orders_state"], "no_visible_api_orders")

    def test_empty_complete_but_unverified_never_no_visible(self):
        obj = snapshot()
        obj["orders"]["rows"] = []
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["orders_state"], "unknown")

    def test_fixture_origin_with_proof_rejected(self):
        obj = snapshot(origin="fixture")
        raw, proof = self.proof(obj)
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30), proof_raw=proof)

    def test_fixture_origin_text_null_settings(self):
        obj = snapshot(origin="fixture")
        obj["account"]["account_ready"] = None
        obj["account"]["values"] = []
        obj["orders"]["rows"] = []
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["provenance"], "invented_fixture")
        self.assertEqual(out["orders_state"], "unknown")
        self.assertIsNone(out["proof_sha256"])

    def test_unavailable_shape(self):
        obj = snapshot(status="unavailable", reason="unmatched", client_id=None)
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["status"], "unavailable")
        self.assertIsNone(out["account"])
        self.assertIsNone(out["orders"])
        self.assertIsNone(out["account_id"])
        self.assertEqual(out["freshness"], "unavailable")
        self.assertEqual(out["orders_state"], "unknown")

    def test_unavailable_valid_lease_allowed(self):
        obj = snapshot(status="unavailable", reason="x", client_id=95)
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["status"], "unavailable")

    def test_unavailable_non_null_sections_rejected(self):
        obj = snapshot(status="unavailable", reason="x", client_id=None)
        obj["account"] = account()
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))

    def test_partial_shape(self):
        for account_end, orders_end, reason in (
            (True, False, "orders"),
            (False, True, "account"),
            (False, False, "account,orders"),
        ):
            obj = snapshot(
                status="partial",
                reason=reason,
                account_obj=account(end=account_end),
                orders_obj=orders(end=orders_end),
            )
            out = parse_account_readiness(encode(obj), as_of=iso(30))
            self.assertEqual(out["status"], "partial")
            self.assertEqual(out["orders_state"], "unknown")

    def test_partial_both_ends_rejected(self):
        obj = snapshot(status="partial", reason="x", account_obj=account(), orders_obj=orders())
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))


class FinanceSemanticsTests(unittest.TestCase):

    def test_settled_usd_unknown_across_currencies(self):
        rows = [
            {"tag": "SettledCash", "currency": "USD", "value": "10"},
            {"tag": "SettledCash", "currency": "HKD", "value": "5"},
            {"tag": "$LEDGER-SettledCash", "currency": "BASE", "value": "7"},
            {"tag": "CashBalance", "currency": "USD", "value": "100"},
        ]
        obj = snapshot()
        obj["account"]["values"] = rows
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertIsNone(out["settled_usd"])
        self.assertEqual(out["settled_usd_state"], "unknown")
        self.assertEqual(out["restrictions_state"], "unknown")
        self.assertFalse(out["executable"])

    def test_signed_and_negative_values_accepted(self):
        obj = snapshot()
        obj["account"]["values"] = [
            {"tag": "CashBalance", "currency": "USD", "value": "-100.5"},
            {"tag": "CashBalance", "currency": "EUR", "value": "3.0"},
            {"tag": "CashBalance", "currency": "", "value": "-0.0"},
        ]
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["status"], "complete")

    def test_quantity_nonnegative_but_limit_positive(self):
        obj = snapshot()
        obj["orders"]["rows"] = [order_row(quantity="0.0")]
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["order_count"], 1)
        bad = snapshot()
        bad["orders"]["rows"] = [order_row(limit_price="0")]
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(bad), as_of=iso(30))


class MutationTests(_SourceHarness):

    def assert_reject(self, obj):
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))

    def test_missing_extra_keys(self):
        obj = snapshot()
        del obj["reason"]
        self.assert_reject(obj)
        obj = snapshot()
        obj["extra"] = 1
        self.assert_reject(obj)
        obj = snapshot()
        del obj["account"]["values"]
        self.assert_reject(obj)
        obj = snapshot()
        obj["orders"]["nope"] = None
        self.assert_reject(obj)
        obj = snapshot()
        del obj["request"]["market_data"]
        self.assert_reject(obj)

    def test_null_and_type_mutations(self):
        obj = snapshot()
        obj["schema"] = True
        self.assert_reject(obj)
        for key in ("started_at", "completed_at"):
            obj = snapshot()
            obj[key] = None
            self.assert_reject(obj)
        obj = snapshot()
        obj["request"]["bind_orders"] = 0
        self.assert_reject(obj)
        obj = snapshot()
        obj["gateway_port"] = "4002"
        self.assert_reject(obj)
        obj = snapshot()
        obj["request"]["startup_fetch"] = True
        self.assert_reject(obj)

    def test_settings_and_client_lease(self):
        for cid in (89, 100, "90"):
            obj = snapshot()
            obj["request"]["client_id"] = cid
            self.assert_reject(obj)
        obj = snapshot()
        obj["request"]["client_id"] = 99
        self.assertEqual(
            parse_account_readiness(encode(obj), as_of=iso(30))["status"], "complete"
        )
        for field, value in (
            ("source", "other"),
            ("connection_mode", "write"),
            ("kind", "other"),
        ):
            obj = snapshot()
            obj[field] = value
            self.assert_reject(obj)

    def test_order_identity_duplicates(self):
        dup_perm = snapshot()
        dup_perm["orders"]["rows"] = [order_row(perm_id=3), order_row(perm_id=3)]
        self.assert_reject(dup_perm)
        dup_pair = snapshot()
        dup_pair["orders"]["rows"] = [
            order_row(perm_id=0, client_id=7, order_id=8),
            order_row(perm_id=0, client_id=7, order_id=8),
        ]
        self.assert_reject(dup_pair)

    def test_order_type_bool_decimal_and_symbol(self):
        for over in (
            {"perm_id": True},
            {"client_id": -1},
            {"order_id": 2147483648},
            {"account_id": "OTHER"},
            {"sec_type": "XX"},
            {"side": "HOLD"},
            {"symbol": "BAD$SYM"},
            {"symbol": ""},
            {"currency": "usd"},
            {"quantity": "1e2"},
            {"quantity": "nan"},
            {"quantity": "-1"},
            {"limit_price": "nan"},
            {"limit_price": "-2"},
            {"order_type": "LMT!"},
            {"status": "Filled\n"},
        ):
            obj = snapshot()
            obj["orders"]["rows"] = [order_row(**over)]
            self.assert_reject(obj)

    def test_foreign_identity_and_account_id(self):
        obj = snapshot(account_id="OTHER123")
        self.assert_reject(obj)
        obj = snapshot()
        obj["requested_account_id"] = "OTHER123"
        self.assert_reject(obj)

    def test_account_value_duplicates_and_bad_tags(self):
        obj = snapshot()
        obj["account"]["values"] = [
            {"tag": "CashBalance", "currency": "USD", "value": "1"},
            {"tag": "CashBalance", "currency": "USD", "value": "2"},
        ]
        self.assert_reject(obj)
        for tag, currency, value in (
            ("Unknown", "USD", "1"),
            ("CashBalance", "usd", "1"),
            ("CashBalance", "US", "1"),
            ("CashBalance", "USD", "nan"),
            ("CashBalance", "USD", 1),
            ("CashBalance", "USD", "1e5"),
        ):
            obj = snapshot()
            obj["account"]["values"] = [
                {"tag": tag, "currency": currency, "value": value}
            ]
            self.assert_reject(obj)

    def test_account_and_order_time_bounds(self):
        obj = snapshot()
        obj["account"]["ended_at"] = iso(99)
        self.assert_reject(obj)
        obj = snapshot()
        obj["orders"]["ended_at"] = iso(-5)
        self.assert_reject(obj)
        obj = snapshot()
        obj["completed_at"] = iso(200)
        self.assert_reject(obj)
        obj = snapshot()
        obj["started_at"] = iso(600)
        self.assert_reject(obj)
        obj = snapshot()
        obj["completed_at"] = "nooffset"
        self.assert_reject(obj)

    def test_end_received_invariants(self):
        obj = snapshot(status="partial", reason="x", orders_obj=orders(end=False))
        obj["orders"]["unattributed_count"] = 1
        self.assert_reject(obj)
        obj = snapshot()
        obj["account"]["account_ready"] = None
        self.assertIsNone(parse_account_readiness(encode(obj), as_of=iso(30))["account"]["account_ready"])
        obj = snapshot()
        obj["account"]["ended_at"] = None
        self.assert_reject(obj)

    def test_size_and_raw_shape(self):
        obj = snapshot()
        obj["orders"]["rows"] = [order_row() for _ in range(1001)]
        self.assert_reject(obj)
        with self.assertRaises(ValueError):
            parse_account_readiness(b"x" * (1024 * 1024 + 1), as_of=iso(30))
        with self.assertRaises(ValueError):
            parse_account_readiness(b"", as_of=iso(30))
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(snapshot()) + b"\xff\xfe", as_of=iso(30))
        with self.assertRaises(ValueError):
            parse_account_readiness(b"[]", as_of=iso(30))
        with self.assertRaises(ValueError):
            parse_account_readiness(b"{bad json", as_of=iso(30))

    def test_duplicate_keys_and_constants(self):
        raw = b'{"schema":1,"schema":1}'
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30))
        obj = snapshot()
        obj["orders"]["rows"] = [
            order_row(quantity="NaN")
        ]
        self.assert_reject(obj)
        raw = encode(snapshot()).replace(b'"status":"complete"', b'"status":NaN', 1)
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30))

    def test_depth_recursion_normalized(self):
        depth = 5000
        raw = ("[" * depth + "]" * depth).encode()
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30))

    def test_decimal_and_exponent_precision(self):
        obj = snapshot()
        obj["account"]["values"] = [
            {"tag": "CashBalance", "currency": "USD", "value": "123456789.12345678"}
        ]
        out = parse_account_readiness(encode(obj), as_of=iso(30))
        self.assertEqual(out["status"], "complete")
        for raw_value in ("1e2", "0x10", "1.2.3", "--1", "", "+3.0", "123456789.123456789"):
            obj = snapshot()
            obj["account"]["values"] = [
                {"tag": "CashBalance", "currency": "USD", "value": raw_value}
            ]
            self.assert_reject(obj)

    def test_current_visible_action_dict_is_not_boundary(self):
        with self.assertRaises(ValueError):
            parse_account_readiness(
                {"status": "complete"}, as_of=iso(30)
            )


class ProofAndSourceTests(_SourceHarness):

    def test_proof_wrong_source_hash(self):
        obj = snapshot()
        raw, proof = self.proof(obj)
        blob = json.loads(proof)
        blob["source_sha256"] = sha(b"different")
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30), proof_raw=encode(blob))

    def test_proof_wrong_raw_and_request_and_captured_at(self):
        obj = snapshot()
        raw, proof = self.proof(obj)
        for mutate in ("raw_sha256", "request_sha256", "captured_at"):
            blob = json.loads(proof)
            if mutate == "captured_at":
                blob[mutate] = iso(999)
            else:
                blob[mutate] = "0" * 64
            with self.assertRaises(ValueError):
                parse_account_readiness(raw, as_of=iso(30), proof_raw=encode(blob))
        blob = json.loads(proof)
        blob["returncode"] = 1
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30), proof_raw=encode(blob))
        blob = json.loads(proof)
        blob["kind"] = "other"
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30), proof_raw=encode(blob))
        blob = json.loads(proof)
        blob["extra"] = 1
        with self.assertRaises(ValueError):
            parse_account_readiness(raw, as_of=iso(30), proof_raw=encode(blob))

    def test_source_bytes_unchanged(self):
        before = CAPTURE_SOURCE_PATH
        obj = snapshot()
        raw, proof = self.proof(obj)
        parse_account_readiness(raw, as_of=iso(30), proof_raw=proof)
        self.assertEqual(mod.CAPTURE_SOURCE_PATH, mod.CAPTURE_SOURCE_PATH)
        self.assertTrue(CAPTURE_SOURCE_PATH.name.endswith("account_capture.py"))

    def test_inputs_never_mutated(self):
        obj = snapshot()
        raw, proof = self.proof(obj)
        raw_copy = bytes(raw)
        proof_copy = bytes(proof)
        parse_account_readiness(raw, as_of=iso(30), proof_raw=proof)
        self.assertEqual(raw, raw_copy)
        self.assertEqual(proof, proof_copy)


class TimestampAndStatusTests(unittest.TestCase):

    def test_reason_bounds_and_status(self):
        obj = snapshot(status="complete")
        obj["reason"] = "should be empty"
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))
        obj = snapshot(status="partial", reason="")
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))
        obj = snapshot(status="partial", reason="x" * 501)
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))

    def test_capture_interval_bound(self):
        obj = snapshot()
        obj["completed_at"] = iso(91)
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(200))

    def test_as_of_exact_fifteen_minutes_is_fresh(self):
        obj = snapshot()
        out = parse_account_readiness(encode(obj), as_of=iso(5 + 900))
        self.assertEqual(out["freshness"], "fresh")
        out = parse_account_readiness(encode(obj), as_of=iso(5 + 901))
        self.assertEqual(out["freshness"], "stale")

    def test_restrictions_reason_required_unknown(self):
        obj = snapshot()
        obj["restrictions"]["state"] = "approved"
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))
        obj = snapshot()
        obj["restrictions"]["reason"] = ""
        with self.assertRaises(ValueError):
            parse_account_readiness(encode(obj), as_of=iso(30))


if __name__ == "__main__":
    unittest.main()
