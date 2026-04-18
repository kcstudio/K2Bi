"""Tests for execution.engine.recovery.

Covers the architect-specified Q3-refined catch-up + discrepancy
matrix: six catch-up cases (all should reconcile cleanly) + four
discrepancy cases (all should refuse to start unless
K2BI_ALLOW_RECOVERY_MISMATCH=1).
"""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal

from execution.connectors.types import (
    BrokerOpenOrder,
    BrokerOrderStatusEvent,
    BrokerPosition,
)
from execution.engine import recovery


NOW = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
EARLIER = datetime(2026, 5, 5, 11, 30, tzinfo=timezone.utc)


def _journal_pending(
    *,
    trade_id: str = "T1",
    strategy: str = "spy-rotational",
    broker_order_id: str = "1000",
    broker_perm_id: str = "2000000",
    ticker: str = "SPY",
    side: str = "buy",
    qty: int = 10,
    limit_price: str = "500",
) -> list[dict]:
    return [
        {
            "ts": EARLIER.isoformat(),
            "event_type": "order_submitted",
            "trade_id": trade_id,
            "journal_entry_id": "J1",
            "strategy": strategy,
            "git_sha": "abc",
            "broker_order_id": broker_order_id,
            "broker_perm_id": broker_perm_id,
            "ticker": ticker,
            "side": side,
            "qty": qty,
            "payload": {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "limit_price": limit_price,
                "submitted_at": EARLIER.isoformat(),
            },
        }
    ]


class CatchUpTests(unittest.TestCase):
    def test_empty_state_is_clean(self):
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CLEAN)
        self.assertEqual(result.events, [])
        self.assertEqual(result.mismatch_reasons, [])

    def test_journal_pending_ibkr_filled(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500.01"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500.01"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)

    def test_journal_pending_ibkr_cancelled(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Cancelled",
            filled_qty=0,
            remaining_qty=10,
            avg_fill_price=None,
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_cancelled", cases)

    def test_journal_pending_ibkr_partial_fill(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=7,
            remaining_qty=3,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=7, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_partially_filled", cases)

    def test_journal_pending_ibkr_still_open(self):
        tail = _journal_pending()
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_still_open", cases)

    def test_journal_pending_ibkr_rejected(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Rejected",
            filled_qty=0,
            remaining_qty=10,
            avg_fill_price=None,
            last_update_at=EARLIER,
            reason="out of hours",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_rejected", cases)
        rejection_event = next(
            e for e in result.events if e.payload.get("case") == "pending_rejected"
        )
        self.assertEqual(rejection_event.payload["broker_reason"], "out of hours")

    def test_avg_price_drift_on_qty_match(self):
        # Journal says we hold 10 SPY at $500; broker says 10 at $502.
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {"ticker": "SPY", "side": "buy", "qty": 10, "fill_price": "500"},
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("502"))],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        drift_events = [e for e in result.events if e.event_type == "avg_price_drift"]
        self.assertEqual(len(drift_events), 1)
        self.assertEqual(drift_events[0].payload["ticker"], "SPY")
        self.assertEqual(drift_events[0].payload["journal_avg_price"], "500")
        self.assertEqual(drift_events[0].payload["broker_avg_price"], "502")


class DiscrepancyTests(unittest.TestCase):
    def test_phantom_position_refuses(self):
        # IBKR shows NVDA that journal never mentioned.
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("phantom_position", cases)

    def test_oversized_position_refuses(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 5,
                "payload": {"ticker": "SPY", "side": "buy", "qty": 5, "fill_price": "500"},
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=20, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("position_oversized_vs_journal", cases)

    def test_missing_position_refuses(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {"ticker": "SPY", "side": "buy", "qty": 10, "fill_price": "500"},
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],  # IBKR no longer holds SPY
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("journal_position_missing_at_broker", cases)

    def test_phantom_open_order_refuses(self):
        phantom = BrokerOpenOrder(
            broker_order_id="9999",
            broker_perm_id="7777777",
            ticker="SPY",
            side="buy",
            qty=5,
            filled_qty=0,
            limit_price=Decimal("495"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=[],  # journal never proposed this order
            broker_positions=[],
            broker_open_orders=[phantom],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        cases = [m["case"] for m in result.mismatch_reasons]
        self.assertIn("phantom_open_order", cases)

    def test_override_env_bypasses_refusal(self):
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="1",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_OVERRIDE)
        mismatch_events = [
            e for e in result.events if e.event_type == "recovery_state_mismatch"
        ]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(
            mismatch_events[0].payload["resolution"], "proceeding_with_override"
        )

    def test_mismatch_event_records_configured_env_name(self):
        # Codex round-7 P3: when a caller passes a custom env name, the
        # mismatch event must record that name in payload.override_env
        # so operators see the right remediation instruction.
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
            override_env_name="K2BI_PAPER_ALLOW_MISMATCH",
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.MISMATCH_REFUSED)
        mismatch_events = [
            e for e in result.events if e.event_type == "recovery_state_mismatch"
        ]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(
            mismatch_events[0].payload["override_env"],
            "K2BI_PAPER_ALLOW_MISMATCH",
        )

    def test_refused_status_still_emits_mismatch_event(self):
        result = recovery.reconcile(
            journal_tail=[],
            broker_positions=[
                BrokerPosition(ticker="NVDA", qty=5, avg_price=Decimal("600"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
            override_env="",
        )
        mismatch_events = [
            e for e in result.events if e.event_type == "recovery_state_mismatch"
        ]
        self.assertEqual(len(mismatch_events), 1)
        self.assertEqual(
            mismatch_events[0].payload["resolution"], "engine_refuses_start"
        )


class PartialThenCancelledTests(unittest.TestCase):
    """Codex round-2 P1: a Cancelled / Rejected terminal with
    filled_qty > 0 must be treated as a partial fill so the filled
    shares show up in reconciliation_deltas."""

    def test_partial_then_cancelled_includes_filled_in_delta(self):
        tail = _journal_pending(qty=10)
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Cancelled",
            filled_qty=4,
            remaining_qty=6,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=4, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        # Classified as partial even though status is Cancelled.
        self.assertIn("pending_partially_filled", cases)
        # No phantom against the 4 filled shares.
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_position"
        ]
        self.assertEqual(phantoms, [])

    def test_rejected_with_zero_fill_stays_rejected(self):
        tail = _journal_pending()
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Rejected",
            filled_qty=0,
            remaining_qty=10,
            avg_fill_price=None,
            last_update_at=EARLIER,
            reason="fat finger",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_rejected", cases)


class PendingKeyConsistencyTests(unittest.TestCase):
    """Codex round-2 P2: order_proposed keys on trade_id (no perm
    yet); downstream submitted/filled events must land on the SAME
    key so terminal cleanup clears the proposal entry."""

    def test_completed_trade_does_not_leave_phantom_proposal(self):
        # Full lifecycle in the tail: proposed -> submitted -> filled.
        tail = [
            {
                "ts": "2026-05-05T10:00:00+00:00",
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": "2026-05-05T10:00:00+00:00",
                },
            },
            {
                "ts": "2026-05-05T10:00:01+00:00",
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": "2026-05-05T10:00:01+00:00",
                },
            },
            {
                "ts": "2026-05-05T10:00:05+00:00",
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J3",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "500",
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # Completed trade must NOT leave a phantom pending entry.
        phantom_pendings = [
            e for e in result.events
            if e.payload.get("case") == "pending_no_broker_counterpart"
        ]
        self.assertEqual(phantom_pendings, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)


class CorruptJournalFieldsTolerantTests(unittest.TestCase):
    """R16-minimax: corrupt Decimal / qty fields in journal payload
    must not crash the engine during reconcile. Graceful degradation:
    log + None/0 fallback so recovery can still classify by broker ID."""

    def test_corrupt_stop_loss_does_not_raise(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "not-a-number",
                    "submitted_at": EARLIER.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                },
            }
        ]
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
        )
        # Must not raise InvalidOperation.
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        still_open = [
            e for e in result.events
            if e.payload.get("case") == "pending_still_open"
        ]
        self.assertEqual(len(still_open), 1)
        # stop_loss degrades to None on corrupt input.
        self.assertIsNone(still_open[0].payload["journal_view"].get("stop_loss"))

    def test_corrupt_limit_price_does_not_raise(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "garbage",
                    "submitted_at": EARLIER.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                },
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # pending_no_broker_counterpart event emitted without crash.
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_no_broker_counterpart", cases)


class StopLossPreservedThroughRecoveryTests(unittest.TestCase):
    """R15-minimax finding: stop_loss must flow journal -> recovery ->
    AwaitingOrderState on resume. Broker's bracket child still holds
    the protective stop, but engine-internal tracking of the order
    needs the strategy-level stop reference for journaling + any
    engine-side risk re-evaluation mid-flight."""

    def test_pending_payload_includes_stop_loss(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "stop_loss": "495",
                    "submitted_at": EARLIER.isoformat(),
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                },
            }
        ]
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        still_open_events = [
            e for e in result.events
            if e.payload.get("case") == "pending_still_open"
        ]
        self.assertEqual(len(still_open_events), 1)
        # journal_view in the event payload must carry stop_loss.
        journal_view = still_open_events[0].payload.get("journal_view", {})
        self.assertEqual(journal_view.get("stop_loss"), "495")


class KillBlockedClearsProposalTests(unittest.TestCase):
    """Codex round-9 P2: kill_blocked after order_proposed means the
    order never reached the broker. Must clear the proposal so recovery
    does not flag it as pending_no_broker_counterpart."""

    def test_kill_blocked_clears_proposal(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "kill_blocked",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "reason": "kill_file_present_at_submit",
                    "ticker": "SPY",
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # No pending events -- kill_blocked cleared the proposal.
        pending_events = [
            e for e in result.events
            if e.event_type == "recovery_reconciled"
            and e.payload.get("case") == "pending_no_broker_counterpart"
        ]
        self.assertEqual(pending_events, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CLEAN)


class PartialFillMidstreamTests(unittest.TestCase):
    """Codex round-6 P1: a partial fill in the journal must NOT clear
    the pending entry. The order only terminates when
    cumulative_filled_qty >= order qty."""

    def test_partial_fill_leaves_pending_open(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 3,  # this record's fill qty
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "fill_qty": 3,
                    "fill_price": "500",
                    "cumulative_filled_qty": 3,  # only 3 of 10 so far
                    "remaining_qty": 7,
                },
            },
        ]
        # Broker shows the order still open with 3 filled, 7 remaining.
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=3,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=3, avg_price=Decimal("500"))],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        # The remaining live order should be classified pending_still_open
        # (not phantom_open_order).
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_still_open", cases)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])

    def test_full_fill_clears_pending(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "fill_qty": 10,
                    "fill_price": "500",
                    "cumulative_filled_qty": 10,
                    "remaining_qty": 0,
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # No pending events -- the full-fill clears the entry.
        pending_events = [
            e for e in result.events
            if e.event_type == "recovery_reconciled"
        ]
        self.assertEqual(pending_events, [])
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)


class StopChildRecognitionTests(unittest.TestCase):
    """Codex round-4 P1: after a parent with stop_loss fills, the GTC
    stop child remains open at broker. Recovery must recognize it via
    client_tag rather than flag it as phantom_open_order."""

    def test_stop_child_after_parent_fill_is_not_phantom(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "status": "Submitted",
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            },
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J2",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "fill_price": "500",
                },
            },
        ]
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-rotational:T1:stop",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])
        recognized = [
            e for e in result.events
            if e.payload.get("case") == "stop_child_recognized"
        ]
        self.assertEqual(len(recognized), 1)
        self.assertEqual(recognized[0].payload["client_tag"], "k2bi:spy-rotational:T1:stop")


class StopChildExcludedFromTradeIdIndexTests(unittest.TestCase):
    """Codex round-14 P1: crash-window scenario where parent filled
    before restart and only the :stop child remains open at broker.
    The stop child MUST NOT be used as a fallback match for the
    parent's trade_id -- that would classify the parent as
    pending_still_open when it actually already filled.
    """

    def test_stop_child_alone_does_not_match_parent_trade_id(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        # Parent already filled; broker status history confirms.
        parent_fill_status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
            client_tag="k2bi:spy-rotational:T1",
        )
        # Only the stop child is still open.
        stop_child = BrokerOpenOrder(
            broker_order_id="1001",
            broker_perm_id="2000001",
            ticker="SPY",
            side="sell",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
            tif="GTC",
            client_tag="k2bi:spy-rotational:T1:stop",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[stop_child],
            broker_order_status=[parent_fill_status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        # Parent classified via the status (pending_filled), NOT as
        # pending_still_open on the stop child.
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)
        self.assertNotIn("pending_still_open", cases)
        # Stop child separately recognized.
        self.assertIn("stop_child_recognized", cases)


class TradeIdStatusMatchTests(unittest.TestCase):
    """Codex round-11 P1: when a process crashes after submit_order
    succeeded (journal has only order_proposed, no broker IDs) and
    the broker terminates the order before restart, recovery must
    match the status event by trade_id via client_tag."""

    def test_terminal_status_matched_by_trade_id(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        status = BrokerOrderStatusEvent(
            broker_order_id="1000",
            broker_perm_id="2000000",
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
            client_tag="k2bi:spy-rotational:T1",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)
        # Must NOT be classified as pending_no_broker_counterpart.
        self.assertNotIn("pending_no_broker_counterpart", cases)


class TradeIdFallbackMatchTests(unittest.TestCase):
    """Codex round-4 P1: crash between submit_order success and
    order_submitted journal write. Journal has only order_proposed
    (no perm/order_id yet); broker has live open order with our
    client_tag. Recovery must match via trade_id fallback."""

    def test_proposed_only_journal_matches_via_client_tag(self):
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "order_proposed",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 10,
                # note: no broker_order_id / broker_perm_id yet --
                # crash happened before order_submitted wrote them
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 10,
                    "limit_price": "500",
                    "submitted_at": EARLIER.isoformat(),
                },
            }
        ]
        broker_open = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="SPY",
            side="buy",
            qty=10,
            filled_qty=0,
            limit_price=Decimal("500"),
            status="Submitted",
            tif="DAY",
            client_tag="k2bi:spy-rotational:T1",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[broker_open],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_open_order"
        ]
        self.assertEqual(phantoms, [])
        # The order should be classified as pending_still_open (matched
        # via trade_id fallback) rather than phantom or no-counterpart.
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_still_open", cases)
        no_counterpart = [e for e in result.events
                          if e.payload.get("case") == "pending_no_broker_counterpart"]
        self.assertEqual(no_counterpart, [])


class AdoptedPositionsReplayTests(unittest.TestCase):
    """Codex round-1 P2: engine_recovered.adopted_positions must seed
    journal-implied positions so a post-override restart does not
    re-flag the same broker holdings as phantoms."""

    def test_adopted_positions_seed_implied_state(self):
        # Earlier session adopted SPY via mismatch_override; the
        # recovery wrote an engine_recovered event with adopted
        # positions. Fresh restart sees only that event + broker
        # reports the same position -> CLEAN catch-up, not phantom.
        tail = [
            {
                "ts": EARLIER.isoformat(),
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J1",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "mismatch_override",
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "500"}
                    ],
                },
            }
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons if m["case"] == "phantom_position"
        ]
        self.assertEqual(phantoms, [])

    def test_adopted_positions_supersede_prior_fills(self):
        # Replay order: earlier order_filled for 5 SPY -> later
        # engine_recovered with adopted_positions 10 SPY. The
        # engine_recovered is the authoritative checkpoint and should
        # replace the accumulated state.
        tail = [
            {
                "ts": "2026-05-05T10:00:00+00:00",
                "event_type": "order_filled",
                "trade_id": "T1",
                "journal_entry_id": "J1",
                "strategy": "spy-rotational",
                "git_sha": "abc",
                "ticker": "SPY",
                "side": "buy",
                "qty": 5,
                "payload": {
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 5,
                    "fill_price": "500",
                },
            },
            {
                "ts": "2026-05-05T11:00:00+00:00",
                "event_type": "engine_recovered",
                "trade_id": None,
                "journal_entry_id": "J2",
                "strategy": None,
                "git_sha": "abc",
                "payload": {
                    "status": "mismatch_override",
                    "adopted_positions": [
                        {"ticker": "SPY", "qty": 10, "avg_price": "505"}
                    ],
                },
            },
        ]
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[
                BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("505"))
            ],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # qty matches the adopted snapshot (10 not 5); no mismatch.
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        phantoms = [
            m for m in result.mismatch_reasons
            if m["case"] in {
                "phantom_position",
                "position_oversized_vs_journal",
                "position_undersized_vs_journal",
            }
        ]
        self.assertEqual(phantoms, [])


class IdentityMatchingTests(unittest.TestCase):
    def test_perm_id_preferred_over_order_id(self):
        # Journal records perm_id=2000000 + order_id=1000; broker-side
        # has a status for perm_id=2000000 but order_id reissued to
        # 2000 (simulating an IB Gateway restart). Recovery should
        # still match on perm.
        tail = _journal_pending(broker_order_id="1000", broker_perm_id="2000000")
        status = BrokerOrderStatusEvent(
            broker_order_id="2000",         # reissued
            broker_perm_id="2000000",       # stable
            status="Filled",
            filled_qty=10,
            remaining_qty=0,
            avg_fill_price=Decimal("500"),
            last_update_at=EARLIER,
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[BrokerPosition(ticker="SPY", qty=10, avg_price=Decimal("500"))],
            broker_open_orders=[],
            broker_order_status=[status],
            now=NOW,
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_filled", cases)

    def test_pending_no_broker_counterpart(self):
        tail = _journal_pending()
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[],
            broker_order_status=[],
            now=NOW,
        )
        # No phantom position + no phantom order; journal-pending
        # without broker trace is a clean catch-up case.
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        cases = [e.payload.get("case") for e in result.events]
        self.assertIn("pending_no_broker_counterpart", cases)


if __name__ == "__main__":
    unittest.main()
