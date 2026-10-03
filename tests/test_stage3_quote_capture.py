"""Synthetic tests for stage3 quote capture; no real broker access."""

from __future__ import annotations

import asyncio
import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest import mock

sys.path.insert(0, "scripts/stage3_paper")
import quote_capture as qc  # noqa: E402

NOW = datetime(2024, 1, 2, 15, 30, tzinfo=timezone.utc)
LEASE = "90"


class FakeTicker:
    def __init__(self, index: int) -> None:
        self.bid = Decimal("10.1") + index
        self.ask = Decimal("10.2") + index
        self.last = Decimal("10.05") + index
        self.time = NOW
        self.lastTimestamp = NOW - timedelta(seconds=1)
        self.delayedLastTimestamp = NOW
        self.marketDataType = 1


class FakeWrapper:
    def __init__(self) -> None:
        self.start_calls = []
        self.restored = 0

    def startTicker(self, reqId, contract, tickType):
        self.start_calls.append((reqId, getattr(contract, "symbol", None), tickType))
        return FakeTicker(len(self.start_calls) - 1)

    def marketDataType(self, reqId, marketDataId):
        return None

    def tickSnapshotEnd(self, reqId):
        return None


class FakeIB:
    def __init__(self, *, matched=True, empty=False, fail_first=False, no_end=False,
                 feed=None, connect_fail=False, clock_fail=False, injected=True):
        self.RequestTimeout = 0
        self.RaiseRequestErrors = False
        self.connected = False
        self.disconnected = 0
        self.connect_args = None
        self.matched = matched
        self.empty = empty
        self.fail_first = fail_first
        self.no_end = no_end
        self.feed = feed
        self.connect_fail = connect_fail
        self.clock_fail = clock_fail
        self.injected = injected
        self.calls = []
        self.cancel_ids = []
        self.wrapper = FakeWrapper()
        self.factory = self._factory
        self.clock_served = False
        self.snapshot_count = 0
        self.contract_ids = []
        self.client = mock.Mock()
        self.client.cancelMktData = lambda reqId: self.cancel_ids.append(reqId)

    def isConnected(self):
        if self.connect_fail and not self.connected:
            raise RuntimeError("isConnected boom")
        return self.connected

    async def connectAsync(self, host, port, **kwargs):
        self.calls.append("connect")
        self.connect_args = (host, port, kwargs)
        if self.connect_fail:
            raise ConnectionError("no gateway")
        self.connected = True
        return self

    def disconnect(self):
        self.calls.append("disconnect")
        self.disconnected += 1
        self.connected = False

    def managedAccounts(self):
        self.calls.append("managedAccounts")
        return ["DUQ220152"] if self.matched else ["OTHER"]

    async def reqCurrentTimeAsync(self):
        self.calls.append("clock")
        if self.clock_fail:
            raise TimeoutError("clock")
        await asyncio.sleep(0)
        return NOW

    def reqMarketDataType(self, value):
        self.calls.append("type")

    async def reqTickersAsync(self, contract, regulatorySnapshot=False):
        self.calls.append("snapshot")
        self.contract_ids.append(hash(contract))  # Real installed SDK rejects zero conId.
        index = self.snapshot_count
        self.snapshot_count += 1
        reqId = 100 + index
        ticker = self.wrapper.startTicker(reqId, contract, "snapshot")
        await asyncio.sleep(0)
        if self.fail_first and index == 0:
            raise RuntimeError("boom")
        if self.empty:
            return [None]
        if index == 1 and self.feed is not None:
            self.wrapper.marketDataType(reqId, self.feed)
        if self.feed is not None and index == 0:
            self.wrapper.marketDataType(reqId, self.feed)
        if not (self.no_end and index == 0):
            self.wrapper.tickSnapshotEnd(reqId)
        return [ticker]

    def _factory(self):
        return self

    def reqMktData(self, *a, **k):
        raise AssertionError("forbidden reqMktData")

    def qualifyContracts(self, *a, **k):
        raise AssertionError("forbidden qualifyContracts")

    def placeOrder(self, *a, **k):
        raise AssertionError("forbidden placeOrder")

    def cancelOrder(self, *a, **k):
        raise AssertionError("forbidden cancelOrder")

    def reqOpenOrders(self, *a, **k):
        raise AssertionError("forbidden reqOpenOrders")

    def reqAllOpenOrders(self, *a, **k):
        raise AssertionError("forbidden reqAllOpenOrders")


def run(ib, *, env=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = qc.main(ib, env=env if env is not None else {"K2BI_GATEWAY_CLIENT_ID": LEASE}, now_fn=lambda: NOW)
    return code, json.loads(buf.getvalue())


class TestCapture(unittest.TestCase):
    def test_connect_args_and_counts(self):
        ib = FakeIB()
        code, payload = run(ib)
        self.assertEqual(code, 0)
        host, port, kwargs = ib.connect_args
        self.assertEqual((host, port), ("127.0.0.1", 4002))
        self.assertEqual(kwargs["clientId"], 90)
        self.assertTrue(kwargs["readonly"])
        self.assertEqual(kwargs["timeout"], 10)
        from ib_async.ib import StartupFetch
        self.assertEqual(kwargs["fetchFields"], StartupFetch(0))
        self.assertEqual(ib.RequestTimeout, 15.0)
        self.assertTrue(ib.RaiseRequestErrors)
        self.assertEqual(ib.calls.count("snapshot"), 3)
        self.assertEqual(ib.contract_ids, [756733, 45411028, 5552])
        self.assertEqual(ib.calls.count("clock"), 1)
        self.assertEqual(ib.calls.count("type"), 1)
        self.assertEqual(ib.disconnected, 1)
        self.assertEqual(payload["origin"], "fixture")
        self.assertEqual(payload["status"], "complete")

    def test_invalid_lease_no_connect(self):
        ib = FakeIB()
        code, payload = run(ib, env={"K2BI_GATEWAY_CLIENT_ID": "42"})
        self.assertEqual(code, 0)
        self.assertEqual(ib.calls, [])
        self.assertIsNone(payload["client_id"])
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(payload["origin"], "fixture")
        self.assertEqual(payload["rows"][0]["reason"], "invalid client lease")

    def test_account_mismatch_no_requests(self):
        ib = FakeIB(matched=False)
        code, payload = run(ib)
        self.assertFalse(payload["account_matched"])
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(ib.calls.count("snapshot"), 0)
        self.assertEqual(ib.calls.count("clock"), 0)
        self.assertEqual(ib.calls.count("type"), 0)
        self.assertEqual(ib.disconnected, 1)
        self.assertEqual(payload["rows"][0]["reason"], "account not managed")

    def test_connect_failure_still_disconnects(self):
        ib = FakeIB(connect_fail=True)
        code, payload = run(ib)
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "unavailable")
        self.assertEqual(ib.disconnected, 1)

    def test_clock_failure_continues(self):
        ib = FakeIB(clock_fail=True)
        code, payload = run(ib)
        self.assertEqual(payload["status"], "complete")
        self.assertIsNone(payload["server_time"])
        self.assertEqual(ib.calls.count("snapshot"), 3)

    def test_no_end_callback_marks_unavailable(self):
        ib = FakeIB(no_end=True)
        code, payload = run(ib)
        self.assertEqual(payload["status"], "partial")
        rows = payload["rows"]
        self.assertTrue(rows[0]["snapshot_end_observed"] is False)
        self.assertEqual(rows[0]["status"], "unavailable")
        self.assertEqual(rows[0]["reason"], "snapshot end not observed")
        self.assertEqual(rows[1]["status"], "received")

    def test_default_type_not_observed(self):
        ib = FakeIB()
        _, payload = run(ib)
        for row in payload["rows"]:
            self.assertIsNone(row["observed_data_type"])
            self.assertTrue(row["snapshot_end_observed"])

    def test_observed_feed_type(self):
        ib = FakeIB(feed=3)
        _, payload = run(ib)
        self.assertEqual(payload["rows"][0]["observed_data_type"], 3)
        self.assertIsNotNone(payload["rows"][0]["observed_data_type_at"])

    def test_failed_row_continues_and_code_only(self):
        ib = FakeIB(fail_first=True)
        code, payload = run(ib)
        self.assertEqual(ib.calls.count("snapshot"), 3)
        row = payload["rows"][0]
        self.assertEqual(row["status"], "unavailable")
        self.assertNotIn("boom", json.dumps(payload))
        self.assertEqual(payload["status"], "partial")

    def test_source_and_local_time_distinct(self):
        ib = FakeIB()
        _, payload = run(ib)
        row = payload["rows"][0]
        self.assertNotEqual(row["last_trade_at"], row["received_at"])
        self.assertEqual(datetime.fromisoformat(row["last_trade_at"]), NOW - timedelta(seconds=1))

    def test_wrapper_restored_and_ids_cancelled(self):
        ib = FakeIB(no_end=True)
        before = ib.wrapper.startTicker
        run(ib)
        self.assertEqual(ib.cancel_ids, [100])
        self.assertEqual(ib.wrapper.startTicker, before)

    def test_unrelated_ids_ignored(self):
        ib = FakeIB()
        ib.wrapper.marketDataType(999, 3)
        ib.wrapper.tickSnapshotEnd(999)
        _, payload = run(ib)
        self.assertIsNone(payload["rows"][0]["observed_data_type"])

    def test_explicit_main_constructs_real_client(self):
        created = FakeIB()
        with mock.patch.object(qc, "_make_ib", return_value=created):
            with mock.patch.dict("os.environ", {"K2BI_GATEWAY_CLIENT_ID": LEASE}):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    code = qc.main(now_fn=lambda: NOW)
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertEqual(payload["origin"], "recorded")
        self.assertEqual(created.connect_args[1], 4002)

    def test_parser_compatibility(self):
        from scripts.stage3_paper import quote_evidence  # test-only import
        ib = FakeIB()
        _, payload = run(ib)
        result = quote_evidence.parse_quote_evidence(json.dumps(payload).encode(), as_of=NOW.isoformat())
        self.assertFalse(result["quote_eligibility"])
        self.assertEqual(payload["kind"], qc.KIND)
        json.dumps(payload, allow_nan=False)

    def test_partial_callback_and_empty_prices_are_honest(self):
        from scripts.stage3_paper.quote_evidence import parse_quote_evidence
        ib = FakeIB(feed=3)
        original = ib.reqTickersAsync
        async def request(contract, **kwargs):
            ticks = await original(contract, **kwargs)
            if contract.symbol == "SPY":
                ticks[0].bid = ticks[0].ask = ticks[0].last = float("nan")
            return ticks
        ib.reqTickersAsync = request
        _, payload = run(ib)
        result = parse_quote_evidence(json.dumps(payload).encode(), as_of=NOW.isoformat())
        self.assertTrue(payload["rows"][0]["snapshot_end_observed"])
        self.assertIsNone(payload["rows"][0]["bid"])
        self.assertFalse(result["quote_eligibility"])
        ib = FakeIB(feed=3)
        async def timeout(contract, **kwargs):
            if contract.symbol != "SPY": return await original(contract, **kwargs)
            ib.snapshot_count += 1
            ib.wrapper.startTicker(100, contract, "snapshot")
            ib.wrapper.marketDataType(100, 3)
            raise TimeoutError("private message")
        original = ib.reqTickersAsync
        ib.reqTickersAsync = timeout
        _, payload = run(ib)
        parse_quote_evidence(json.dumps(payload).encode(), as_of=NOW.isoformat())
        self.assertEqual(payload["rows"][0]["observed_data_type_at"], NOW.isoformat())
        self.assertEqual(payload["rows"][0]["observed_data_type"], 3)
        self.assertEqual(ib.cancel_ids, [100])

    def test_forbidden_apis_untouched(self):
        ib = FakeIB()
        run(ib)
        self.assertEqual(ib.calls.count("snapshot"), 3)
        self.assertTrue(set(ib.calls) <= {"connect", "managedAccounts", "clock", "type", "snapshot", "disconnect"})


if __name__ == "__main__":
    unittest.main()
