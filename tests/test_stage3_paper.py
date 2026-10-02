"""Snapshot parser and capture tests (invented fixtures only)."""
from __future__ import annotations

import contextlib
import io
import json
import unittest

from scripts.stage3_paper import capture, snapshot as snap

AS_OF = "2026-10-02T12:00:00+00:00"


def base_doc(**overrides):
    doc = {"schema": 1, "kind": "ibkr-paper-account-snapshot", "origin": "recorded",
           "source": "IBKR paper Gateway", "connection_mode": "readonly", "gateway_port": 4002,
           "requested_account_id": "DUQ220152", "account_id": "DUQ220152",
           "started_at": "2026-10-02T11:59:00+00:00", "completed_at": "2026-10-02T11:59:30+00:00",
           "status": "complete", "reason": "", "positions": [],
           "account_values": {"TotalCashValue": {"value": "100.50", "currency": "USD"},
                              "NetLiquidation": None, "AvailableFunds": None}}
    doc.update(overrides)
    return doc


def raw(doc):
    return json.dumps(doc).encode("utf-8")


def position(**overrides):
    pos = {"account_id": "DUQ220152", "contract_id": 12345, "symbol": "AAPL", "sec_type": "STK",
           "currency": "USD", "exchange": "NASDAQ", "quantity": "10", "average_cost": "150.25"}
    pos.update(overrides)
    return pos


class SnapshotTests(unittest.TestCase):
    def test_fresh(self):
        out = snap.parse_snapshot(raw(base_doc()), as_of="2026-10-02T12:05:00+00:00")
        self.assertEqual(out["freshness"], "fresh")
        self.assertEqual(len(out["raw_sha256"]), 64)

    def test_stale(self):
        doc = base_doc(started_at="2026-10-02T10:59:00+00:00", completed_at="2026-10-02T11:00:00+00:00")
        self.assertEqual(snap.parse_snapshot(raw(doc), as_of=AS_OF)["freshness"], "stale")

    def test_known_zero(self):
        self.assertEqual(snap.parse_snapshot(raw(base_doc(positions=[])), as_of=AS_OF)["positions"], [])

    def test_partial_and_unavailable(self):
        none_doc = base_doc(positions=None, account_values=None)
        out = snap.parse_snapshot(raw(none_doc), as_of=AS_OF)
        self.assertIsNone(out["positions"])
        self.assertIsNone(out["account_values"])
        un = base_doc(status="unavailable", account_id=None, positions=None,
                      account_values=None, reason="gateway refused")
        out = snap.parse_snapshot(raw(un), as_of=AS_OF)
        self.assertEqual(out["freshness"], "unavailable")

    def test_malformed_families(self):
        cases = [base_doc(gateway_port=True), base_doc(account_id="DUQ999999"),
                 base_doc(positions=[position(quantity="1e3")]),
                 base_doc(positions=[position(quantity="0")]),
                 base_doc(positions=[position(quantity="1.123456789")]),
                 base_doc(positions=[position(symbol="aapl")]),
                 base_doc(positions=[position(currency="usd")]),
                 base_doc(positions=[position(), position()]),
                 base_doc(completed_at="2027-01-01T00:00:00+00:00")]
        for doc in cases:
            with self.assertRaises(ValueError):
                snap.parse_snapshot(raw(doc), as_of=AS_OF)
        extra = base_doc()
        extra["extra"] = 1
        with self.assertRaises(ValueError):
            snap.parse_snapshot(raw(extra), as_of=AS_OF)
        with self.assertRaises(ValueError):
            snap.parse_snapshot(b'{"schema":1,"schema":2}', as_of=AS_OF)
        with self.assertRaises(ValueError):
            snap.parse_snapshot(b"x" * (snap.MAX_RAW_BYTES + 1), as_of=AS_OF)

    def test_trailing_newline_rejected(self):
        doc = base_doc(positions=[position(quantity="10\n")])
        with self.assertRaises(ValueError):
            snap.parse_snapshot(raw(doc), as_of=AS_OF)


class FakeContract:
    def __init__(self, con_id, symbol="AAPL", sec_type="STK", currency="USD", exchange="NASDAQ"):
        self.conId = con_id
        self.symbol = symbol
        self.secType = sec_type
        self.currency = currency
        self.exchange = exchange


class FakePositionRow:
    def __init__(self, account, contract, position, avg_cost):
        self.account = account
        self.contract = contract
        self.position = position
        self.avgCost = avg_cost


class FakeSummaryRow:
    def __init__(self, account, tag, value, currency):
        self.account = account
        self.tag = tag
        self.value = value
        self.currency = currency


class FakeIB:
    def __init__(self, managed=("DUQ220152",), positions=None, summary=None, connected=True):
        self._managed = list(managed)
        self._positions = positions if positions is not None else []
        self._summary = summary if summary is not None else []
        self._connected = connected
        self.connect_kwargs = None
        self.disconnected = False

    def managedAccounts(self):
        return list(self._managed)

    def reqPositions(self):
        return list(self._positions)

    def accountSummary(self, account):
        return list(self._summary)

    def isConnected(self):
        return self._connected

    def connect(self, host, port, clientId=None, timeout=None, readonly=None, fetchFields=None):
        self.connect_kwargs = {"host": host, "port": port, "clientId": clientId,
                               "timeout": timeout, "readonly": readonly, "fetchFields": fetchFields}
        self._connected = True

    def disconnect(self):
        self.disconnected = True
        self._connected = False


def run_capture(ib, env=None, fetch=None):
    env = env or {"K2BI_GATEWAY_CLIENT_ID": "90"}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        capture.main(ib=ib, fetch_fields=fetch, env=env)
    return json.loads(buf.getvalue())


class CaptureTests(unittest.TestCase):
    def test_connect_and_account_filter(self):
        row = FakePositionRow("DUQ220152", FakeContract(1), 10, 150.0)
        other = FakePositionRow("DUQ999999", FakeContract(2), 1, 1.0)
        summary = [FakeSummaryRow("DUQ220152", "TotalCashValue", 100.0, "BASE"),
                   FakeSummaryRow("DUQ220152", "NetLiquidation", 500.0, "BASE"),
                   FakeSummaryRow("DUQ220152", "AvailableFunds", 50.0, "BASE")]
        ib = FakeIB(positions=[row, other], summary=summary)
        payload = run_capture(ib, fetch="FIELDS")
        self.assertEqual(ib.connect_kwargs["host"], "127.0.0.1")
        self.assertEqual(ib.connect_kwargs["port"], 4002)
        self.assertEqual(ib.connect_kwargs["timeout"], 10)
        self.assertTrue(ib.connect_kwargs["readonly"])
        self.assertEqual(ib.connect_kwargs["clientId"], 90)
        self.assertTrue(ib.disconnected)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(len(payload["positions"]), 1)

    def test_section_independence(self):
        class Bad(FakeIB):
            def reqPositions(self):
                raise RuntimeError("boom")
        ib = Bad(summary=[FakeSummaryRow("DUQ220152", "TotalCashValue", 5.0, "BASE")])
        payload = run_capture(ib)
        self.assertEqual(payload["status"], "complete")
        self.assertEqual(payload["account_id"], "DUQ220152")
        self.assertIsNone(payload["positions"])
        self.assertIsNotNone(payload["account_values"])
        self.assertTrue(payload["reason"])

    def test_invalid_position_row(self):
        bad = FakePositionRow("DUQ220152", FakeContract(1, symbol="aapl"), 10, 150.0)
        ib = FakeIB(positions=[bad], summary=[])
        payload = run_capture(ib)
        self.assertIsNone(payload["positions"])
        self.assertEqual(payload["status"], "complete")

    def test_unknown_account_row_is_error(self):
        row = FakePositionRow(None, FakeContract(1), 10, 150.0)
        ib = FakeIB(positions=[row], summary=[])
        self.assertIsNone(run_capture(ib)["positions"])

    def test_missing_account_overall_unavailable(self):
        payload = run_capture(FakeIB(managed=("OTHER",)))
        self.assertEqual(payload["status"], "unavailable")
        self.assertIsNone(payload["account_id"])

    def test_invalid_lease(self):
        ib = FakeIB()
        payload = run_capture(ib, env={"K2BI_GATEWAY_CLIENT_ID": "1"})
        self.assertEqual(payload["status"], "unavailable")
        self.assertIsNone(ib.connect_kwargs)


class CaptureIntegrationTests(unittest.TestCase):
    def test_failed_connect_after_partial_connection_is_disconnected(self):
        class Failed(FakeIB):
            def connect(self, *args, **kwargs):
                self._connected = True
                raise TimeoutError('late handshake')
        ib = Failed()
        self.assertEqual(run_capture(ib)['status'], 'unavailable')
        self.assertTrue(ib.disconnected)

    def test_bool_capture_is_unknown_and_decimal_string_is_exact(self):
        row = FakePositionRow('DUQ220152', FakeContract(1), True, '150.12345678')
        self.assertIsNone(run_capture(FakeIB(positions=[row]))['positions'])
        self.assertEqual(capture._decimal_text('999999999999.12345678'), '999999999999.12345678')
