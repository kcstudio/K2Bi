"""Tests for the offline (fixture-only) Stage 3 risk preparation module."""

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest import mock

from scripts.stage3_paper.offline_risk import (
    evaluate_risk,
    load_json,
    parse_money,
    parse_timestamp,
)

CONFIG_PATH = Path("execution/validators/config.yaml")
CAPTURED = datetime(2024, 3, 4, 15, 0, 0, tzinfo=timezone.utc)  # Monday 10:00 ET
AS_OF = CAPTURED.isoformat()


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _order(ticker="SPY", side="buy", qty=1, limit="100", stop="99", submitted=None):
    return {
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "limit_price": limit,
        "stop_loss": stop,
        "strategy": "stage3-fixture",
        "submitted_at": _stamp(submitted or (CAPTURED - timedelta(seconds=30))),
        "extended_hours": False,
        "order_type": "LMT",
    }


def _record(origin="fixture", **overrides):
    record = {
        "schema": 1,
        "kind": "offline-risk-context",
        "origin": origin,
        "account_id": "DUQ220152",
        "currency": "USD",
        "captured_at": _stamp(CAPTURED),
        "expires_at": _stamp(CAPTURED + timedelta(minutes=10)),
        "proposal": _order(),
        "context": {
            "account_value": "100000",
            "cash": "100000",
            "positions": [],
            "pending_orders": [],
            "current_marks": {"SPY": "100.25"},
            "server_time": _stamp(CAPTURED - timedelta(seconds=10)),
        },
        "coverage": {
            "positions": True,
            "pending_orders": True,
            "settled_usd": True,
            "restrictions": True,
            "current_quote": True,
            "approved_rules": True,
        },
    }
    for key, value in overrides.items():
        record[key] = value
    return record


def _blob(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


class TestOfflineRisk(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config_raw = CONFIG_PATH.read_bytes()

    def evaluate(self, payload=None, *, origin="fixture", as_of=AS_OF, raw=None, **overrides):
        if raw is None:
            body = _record(origin=origin, **overrides) if payload is None else payload
            raw = _blob(body)
        return raw, evaluate_risk(raw, as_of=as_of, config_raw=self.config_raw)

    def statuses(self, out):
        return [r["status"] for r in out["results"]]

    def assert_never_approved(self, out):
        self.assertFalse(out["approved"])
        self.assertFalse(out["executable"])
        self.assertFalse(out["fill_eligible"])

    # --- helpers ---------------------------------------------------
    def test_helper_parse_families(self):
        self.assertEqual(parse_timestamp("2024-03-04T14:00:00Z", "t").tzinfo, timezone.utc)
        self.assertEqual(parse_money("-12.50", "m"), Decimal("-12.50"))
        for bad in ("2024-03-04", "2024-03-04T14:00:00", "garbage", "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-14:00", 5):
            with self.subTest(ts=bad), self.assertRaises(ValueError):
                parse_timestamp(bad, "t")
        for bad in ("1e5", "+01", "1.", ".5", "1,000", " 1", "1\n", True, 5, None, "1e13"):
            with self.subTest(money=bad), self.assertRaises(ValueError):
                parse_money(bad, "m")
        with self.assertRaises(ValueError):
            parse_money("0", "m", positive=True)

    def test_helper_load_json_rejects(self):
        for raw in (b"", b"not json", b'{"a":1,"a":2}', b"NaN", b"[]", b'{"a":' * 34 + b'1' + b'}' * 34):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_json(raw)
        with self.assertRaises(ValueError):
            load_json("str")
        self.assertEqual(load_json(b'{"a": 1}'), {"a": 1})

    # --- structured not_run ----------------------------------------
    def test_none_stale_incomplete_not_run(self):
        raw, out = None, evaluate_risk(None, as_of=AS_OF, config_raw=self.config_raw)
        self.assertEqual(out["status"], "not_run")
        self.assertIsNone(out["proposal"])
        self.assertEqual(out["raw_sha256"], "")
        self.assertEqual(self.statuses(out), ["not_run"] * 5)
        self.assert_never_approved(out)

        _, out = self.evaluate(as_of=_stamp(CAPTURED + timedelta(minutes=20)))
        self.assertEqual(out["status"], "not_run")
        self.assertIn("stale", out["reason"])

        _, out = self.evaluate(coverage={
            "positions": True, "pending_orders": False, "settled_usd": True,
            "restrictions": True, "current_quote": True, "approved_rules": True,
        })
        self.assertEqual(out["status"], "not_run")
        self.assertIn("coverage", out["reason"])

    def test_missing_stops_and_marks_not_run(self):
        _, out = self.evaluate(proposal=_order(stop=None))
        self.assertEqual(out["status"], "not_run")
        self.assertIn("stop", out["reason"])

        held = _record(context={
            "account_value": "100000", "cash": "50000",
            "positions": [{"ticker": "SPY", "qty": 1, "avg_price": "100", "stop_loss": None}],
            "pending_orders": [], "current_marks": {"SPY": "100"},
            "server_time": _stamp(CAPTURED - timedelta(seconds=5)),
        })
        _, out = self.evaluate(payload=held)
        self.assertEqual(out["status"], "not_run")
        self.assertIn("held position", out["reason"])

        _, out = self.evaluate(context={
            "account_value": "100000", "cash": "100000", "positions": [],
            "pending_orders": [], "current_marks": {}, "server_time": _stamp(CAPTURED),
        })
        self.assertEqual(out["status"], "not_run")
        self.assertIn("marks", out["reason"])

    # --- malformed supplied input raises ValueError -----------------
    def test_malformed_raises(self):
        cases = {
            "dup": _blob(_record()).replace(b"}", b"}", 1)[:-1] + b',"extra":1}',
            "utf8": b"{\xff\xfe}",
            "account": _blob(_record(account_id="XXXX")),
            "hkd": _blob(_record(currency="HKD")),
            "base": _blob(_record(currency="BASE")),
            "future": _blob(_record(captured_at=_stamp(CAPTURED + timedelta(minutes=1)),
                                    expires_at=_stamp(CAPTURED + timedelta(minutes=5)))),
            "naive": _blob(_record(captured_at="2024-03-04T14:00:00")),
            "overflow": _blob(_record(captured_at="0001-01-01T00:00:00+14:00")),
            "decimalnl": _blob(_record(proposal=_order(limit="100\n"))),
            "nonstr": _blob(_record(proposal=_order(limit=100))),
            "boolqty": _blob(_record(proposal=_order(qty=True))),
        }
        for name, raw in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                evaluate_risk(raw, as_of=AS_OF, config_raw=self.config_raw)

    def test_shape_and_expiry_and_pending_future_raise(self):
        payload = _record()
        del payload["currency"]
        with self.assertRaises(ValueError):
            self.evaluate(payload=payload)
        with self.assertRaises(ValueError):
            self.evaluate(expires_at=_stamp(CAPTURED))
        pending = _order(submitted=CAPTURED + timedelta(minutes=1))
        with self.assertRaises(ValueError):
            self.evaluate(context={
                "account_value": "100000", "cash": "100000", "positions": [],
                "pending_orders": [pending], "current_marks": {"SPY": "100"},
                "server_time": _stamp(CAPTURED - timedelta(seconds=5)),
            })
        with self.assertRaises(ValueError):
            evaluate_risk(b"{}", as_of=AS_OF, config_raw=b"\xff\xfe")

    # --- fixture runner --------------------------------------------
    def test_fixture_pass_and_repeat(self):
        raw, out = self.evaluate()
        self.assertEqual(out["status"], "fixture_pass")
        self.assertEqual(out["proposal"]["ticker"], "SPY")
        self.assertEqual(out["proposal"]["limit_price"], "100")
        self.assertEqual(self.statuses(out), ["approved"] * 5)
        self.assertTrue(out["risk_ok"])
        self.assert_never_approved(out)
        self.assertNotIn("submission", out)
        self.assertNotIn("adoption", out)
        self.assertEqual(out["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(out["config_sha256"], hashlib.sha256(self.config_raw).hexdigest())
        _, again = self.evaluate(raw=raw)
        self.assertEqual(out, again)

    def test_whitelist_reject_then_not_run(self):
        doc = _record(proposal=_order(ticker="CALX")); doc["context"]["current_marks"]["CALX"] = "100"
        _, out = self.evaluate(payload=doc)
        self.assertEqual(out["status"], "fixture_rejected")
        self.assertEqual(out["results"][0]["module"], "instrument_whitelist")
        self.assertEqual(out["results"][0]["status"], "rejected")
        self.assertEqual(self.statuses(out)[1:], ["not_run"] * 4)
        self.assertFalse(out["risk_ok"])

    def test_concentration_open_risk_cash_only_reject(self):
        pending = [_order(limit="400000", stop="399000")]
        _, out = self.evaluate(
            proposal=_order(limit="400000", stop="399000"),
            context={
                "account_value": "100000", "cash": "100000", "positions": [],
                "pending_orders": pending, "current_marks": {"SPY": "400000"},
                "server_time": _stamp(CAPTURED - timedelta(seconds=5)),
            },
        )
        self.assertEqual(out["status"], "fixture_rejected")
        self.assertFalse(out["risk_ok"])
        self.assertIn("rejected", self.statuses(out))

    def test_sell_covered_and_uncovered(self):
        uncovered = self.evaluate(proposal=_order(side="sell", stop=None), context={
            "account_value": "100000", "cash": "100000", "positions": [],
            "pending_orders": [], "current_marks": {"SPY": "100"},
            "server_time": _stamp(CAPTURED - timedelta(seconds=5)),
        })[1]
        self.assertEqual(uncovered["status"], "fixture_rejected")
        self.assertEqual(uncovered["results"][-1]["module"], "leverage")
        self.assertEqual(uncovered["results"][-1]["status"], "rejected")

        covered_raw, _ = self.evaluate(
            proposal=_order(side="sell", stop=None),
            context={
                "account_value": "100000", "cash": "100000",
                "positions": [{"ticker": "SPY", "qty": 5, "avg_price": "100", "stop_loss": "95"}],
                "pending_orders": [], "current_marks": {"SPY": "100"},
                "server_time": _stamp(CAPTURED - timedelta(seconds=5)),
            },
        )
        out = evaluate_risk(covered_raw, as_of=AS_OF, config_raw=self.config_raw)
        self.assertFalse(out["approved"])
        self.assertIn(out["status"], ("fixture_pass", "fixture_rejected"))

    # --- recorded origin -------------------------------------------
    def test_recorded_never_calls_runner(self):
        with mock.patch("execution.validators.runner.run_all") as spy:
            raw, out = self.evaluate(origin="recorded")
            spy.assert_not_called()
        self.assertEqual(out["status"], "not_run")
        self.assertEqual(out["origin"], "recorded")
        self.assertIsNone(out["proposal"])
        self.assertIn("recorded", out["reason"])
        self.assertEqual(out["raw_sha256"], hashlib.sha256(raw).hexdigest())
        self.assert_never_approved(out)


if __name__ == "__main__":
    unittest.main()
