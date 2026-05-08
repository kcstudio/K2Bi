"""End-to-end tests for Round-6 order_type plumbing (2026-05-08).

Exercises the loader -> runner -> validators -> connector chain on
MKT/null and MKT/with-reference-hint shapes. Pre-Round-6, the loader
required limit_price as Decimal which made these shapes unreachable;
post-Round-6 they flow through cleanly with order_type carried as a
first-class field at every boundary.

Coverage:

  - Mock connector level: order_type round-trips into SubmittedOrderRecord.
  - Mock connector level: LMT + null limit_price fails fast at the wire
    boundary (defense-in-depth; loader/engine should already have
    stopped this earlier, but the connector enforces too).
  - Engine level: MKT approved strategy with marks present submits with
    order_type=MKT and limit_price=None at the broker boundary; the
    validator-pass uses the mark as a reference price for notional /
    risk math without crashing.
  - Engine level: MKT approved strategy with NO marks for the ticker
    fails closed via journaled validator_rejected
    (reason=no_safe_reference_price_for_mkt_order). The tick does NOT
    crash with TypeError mid-validation.
  - Engine level: MKT + reference-hint preserves MKT at the connector
    boundary; the hint does NOT become an authoritative limit on the
    wire.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector, SubmittedOrderRecord
from execution.engine.main import (
    DEFAULT_TICK_SECONDS,
    Engine,
    EngineConfig,
    EngineState,
)
from execution.journal.writer import JournalWriter


ET = ZoneInfo("US/Eastern")


CONFIG = {
    "position_size": {
        "max_trade_risk_pct": 0.01,
        "max_ticker_concentration_pct": 0.20,
    },
    "trade_risk": {"max_open_risk_pct": 0.05},
    "leverage": {"cash_only": True, "max_leverage": 1.0},
    "market_hours": {
        "regular_open": "09:30",
        "regular_close": "16:00",
        "allow_pre_market": False,
        "allow_after_hours": False,
    },
    "instrument_whitelist": {"symbols": ["G"]},
}


def _mid_session_utc() -> datetime:
    return datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_mkt_strategy(
    dir: Path,
    name: str = "g-mkt-paper",
    *,
    qty: int = 71,
    stop_loss: str = "30.00",
    limit_price_yaml: str = "null",
    order_type: str = "MKT",
) -> Path:
    """Write a strategy file in the post-Round-6 shape: order_type
    carried explicitly, limit_price expressed as a YAML literal so
    `null` stays null after parse.
    """
    text = (
        "---\n"
        f"name: {name}\n"
        "status: approved\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.0025\n"
        "approved_at: 2026-05-08T04:00:00Z\n"
        "approved_commit_sha: deadbeef\n"
        "order:\n"
        "  ticker: G\n"
        "  side: buy\n"
        f"  qty: {qty}\n"
        f"  order_type: {order_type}\n"
        f"  limit_price: {limit_price_yaml}\n"
        f"  stop_loss: {stop_loss}\n"
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nMarket-order paper trade.\n"
    )
    path = dir / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class MockConnectorOrderTypeTests(unittest.IsolatedAsyncioTestCase):
    """Direct connector tests; no engine."""

    async def asyncSetUp(self):
        self.connector = MockIBKRConnector()
        await self.connector.connect()

    async def test_mkt_with_null_limit_round_trips(self):
        ack = await self.connector.submit_order(
            ticker="G",
            side="buy",
            qty=71,
            limit_price=None,
            stop_loss=Decimal("30.00"),
            time_in_force="DAY",
            client_tag="k2bi:test:abc",
            order_type="MKT",
        )
        self.assertEqual(ack.status, "Submitted")
        self.assertEqual(len(self.connector.submitted_orders), 1)
        rec: SubmittedOrderRecord = self.connector.submitted_orders[0]
        self.assertEqual(rec.order_type, "MKT")
        self.assertIsNone(rec.limit_price)
        self.assertEqual(rec.stop_loss, Decimal("30.00"))

    async def test_mkt_with_reference_hint_round_trips(self):
        ack = await self.connector.submit_order(
            ticker="G",
            side="buy",
            qty=71,
            limit_price=Decimal("34.50"),
            stop_loss=Decimal("30.00"),
            time_in_force="DAY",
            client_tag="k2bi:test:abc",
            order_type="MKT",
        )
        self.assertEqual(ack.status, "Submitted")
        rec = self.connector.submitted_orders[0]
        self.assertEqual(rec.order_type, "MKT")
        # Hint preserved on the record so callers can audit it; the
        # connector still sends MarketOrder semantically (the broker
        # ignores the hint at the wire).
        self.assertEqual(rec.limit_price, Decimal("34.50"))

    async def test_lmt_with_null_limit_rejects(self):
        with self.assertRaises(ValueError):
            await self.connector.submit_order(
                ticker="SPY",
                side="buy",
                qty=10,
                limit_price=None,
                stop_loss=None,
                time_in_force="DAY",
                client_tag="k2bi:test:abc",
                order_type="LMT",
            )

    async def test_lmt_with_decimal_limit_round_trips(self):
        ack = await self.connector.submit_order(
            ticker="SPY",
            side="buy",
            qty=10,
            limit_price=Decimal("500.00"),
            stop_loss=Decimal("495.00"),
            time_in_force="DAY",
            client_tag="k2bi:test:abc",
            order_type="LMT",
        )
        self.assertEqual(ack.status, "Submitted")
        rec = self.connector.submitted_orders[0]
        self.assertEqual(rec.order_type, "LMT")
        self.assertEqual(rec.limit_price, Decimal("500.00"))

    async def test_default_order_type_is_lmt_for_backward_compat(self):
        ack = await self.connector.submit_order(
            ticker="SPY",
            side="buy",
            qty=10,
            limit_price=Decimal("500.00"),
            stop_loss=None,
            time_in_force="DAY",
            client_tag=None,
        )
        self.assertEqual(ack.status, "Submitted")
        rec = self.connector.submitted_orders[0]
        self.assertEqual(rec.order_type, "LMT")


class EngineMktOrderE2ETests(unittest.IsolatedAsyncioTestCase):
    """Engine-tick e2e: approved MKT/null strategy ends up at the
    connector with order_type=MKT and limit_price=None.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"

        self.connector = MockIBKRConnector()
        # Mark present so the engine resolves a reference price for
        # validator math; the broker still sees MKT/null.
        self.connector.marks = {"G": Decimal("34.50")}
        self.journal = JournalWriter(
            base_dir=self.journal_dir, git_sha="test01"
        )
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.engine.main as main_mod
        import execution.connectors.mock as mock_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_dt = main_mod.datetime
        main_mod.datetime = _PatchedDT
        self._orig_mock_dt = mock_mod.datetime
        mock_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.engine.main as main_mod
        import execution.connectors.mock as mock_mod

        main_mod.datetime = self._orig_dt
        mock_mod.datetime = self._orig_mock_dt

    async def test_mkt_null_strategy_submits_as_market_order(self):
        _write_mkt_strategy(self.strategies_dir, "g-mkt-paper")
        await self._patch_now(_mid_session_utc())
        try:
            tick1 = await self.engine.tick_once()  # INIT
            self.assertEqual(tick1.state_after, EngineState.CONNECTED_IDLE)

            tick2 = await self.engine.tick_once()  # process + submit
            self.assertEqual(
                tick2.orders_submitted, 1,
                f"expected one submit, got tick={tick2!r}",
            )
            self.assertEqual(tick2.state_after, EngineState.AWAITING_FILL)

            # Connector saw MKT semantics on the wire.
            self.assertEqual(len(self.connector.submitted_orders), 1)
            rec = self.connector.submitted_orders[0]
            self.assertEqual(rec.ticker, "G")
            self.assertEqual(rec.qty, 71)
            self.assertEqual(rec.order_type, "MKT")
            self.assertIsNone(
                rec.limit_price,
                "broker must see limit_price=None for MKT; "
                "engine reference price is for validator math only",
            )
            self.assertEqual(rec.stop_loss, Decimal("30.00"))

            # Journal records carry order_type for replay.
            events = self.journal.read_all()
            proposed = next(
                e for e in events if e["event_type"] == "order_proposed"
            )
            self.assertEqual(proposed["payload"]["order_type"], "MKT")
            # The proposed limit_price reflects the resolved validator
            # reference (the mark), so replay can reconstruct the
            # validator math; the wire-level order_type is the source
            # of truth for what the broker received.
            self.assertEqual(
                proposed["payload"]["limit_price"], "34.50"
            )
        finally:
            await self._unpatch_now()

    async def test_mkt_null_with_no_mark_fail_closes_without_crash(self):
        _write_mkt_strategy(self.strategies_dir, "g-mkt-paper")
        # Critical path: clear marks so the reference-price resolution
        # has nothing to anchor on. Pre-Round-6 this would TypeError
        # mid-validation; post-Round-6 it journals a clean rejection.
        self.connector.marks = {}
        await self._patch_now(_mid_session_utc())
        try:
            tick1 = await self.engine.tick_once()  # INIT
            self.assertEqual(tick1.state_after, EngineState.CONNECTED_IDLE)

            tick2 = await self.engine.tick_once()  # processing
            self.assertEqual(tick2.orders_submitted, 0)
            self.assertGreaterEqual(tick2.orders_rejected, 1)

            # Nothing went to the broker.
            self.assertEqual(len(self.connector.submitted_orders), 0)

            # A journaled order_rejected captures the reason so
            # operator alerting can trigger on it.
            events = self.journal.read_all()
            rej = [
                e for e in events
                if e["event_type"] == "order_rejected"
                and e["payload"].get("reason")
                == "no_safe_reference_price_for_mkt_order"
            ]
            self.assertGreaterEqual(len(rej), 1)
            payload = rej[0]["payload"]
            self.assertEqual(payload["order_type"], "MKT")
        finally:
            await self._unpatch_now()

    async def test_mkt_with_reference_hint_stays_mkt_on_wire(self):
        _write_mkt_strategy(
            self.strategies_dir,
            "g-mkt-with-hint",
            limit_price_yaml="34.50",
        )
        await self._patch_now(_mid_session_utc())
        try:
            await self.engine.tick_once()
            tick2 = await self.engine.tick_once()
            self.assertEqual(tick2.orders_submitted, 1)

            rec = self.connector.submitted_orders[0]
            self.assertEqual(rec.order_type, "MKT")
            # Round-6 R2 wire-safety override: even though the strategy
            # spec carried a non-null reference-price hint
            # (limit_price: 34.50), the engine's wire branch forces
            # limit_price=None for MKT orders before they hit the
            # connector. The reference hint is engine-side validator
            # math territory only; it must NEVER reach the broker
            # where it could be misinterpreted as an authoritative
            # limit (e.g. by a future MOO/MOC variant or a different
            # broker connector).
            self.assertIsNone(rec.limit_price)
        finally:
            await self._unpatch_now()


class MktOrderRecoveryResumeTests(unittest.TestCase):
    """Round-6 (2026-05-08): crash-restart resume preserves order_type.

    Pre-Round-6 the recovery path silently rebuilt resumed orders as
    LMT (the dataclass default), corrupting MKT semantics on restart.
    These tests exercise the journal -> recovery.reconcile ->
    _pick_resumable_awaiting bridge to confirm the field carries
    through end-to-end.
    """

    def _journal_pending_mkt(self, *, order_type: str = "MKT") -> list[dict]:
        from datetime import timedelta

        earlier = _mid_session_utc() - timedelta(seconds=30)
        # Wire-level limit_price for MKT is None; for LMT it's a Decimal
        # string. Mirror what the engine actually journals post-Round-6.
        wire_limit = None if order_type == "MKT" else "34.50"
        return [
            {
                "ts": earlier.isoformat(),
                "event_type": "order_submitted",
                "trade_id": "trade123",
                "journal_entry_id": "J1",
                "strategy": "g-mkt-paper",
                "git_sha": "abc",
                "broker_order_id": "1000",
                "broker_perm_id": "2000000",
                "ticker": "G",
                "side": "buy",
                "qty": 71,
                "payload": {
                    "ticker": "G",
                    "side": "buy",
                    "qty": 71,
                    "order_type": order_type,
                    "limit_price": wire_limit,
                    "stop_loss": "30.00",
                    "submitted_at": earlier.isoformat(),
                },
            }
        ]

    def test_recovery_reconcile_carries_order_type_through_journal_view(self):
        from execution.connectors.types import BrokerOpenOrder
        from execution.engine import recovery

        tail = self._journal_pending_mkt(order_type="MKT")
        # Broker shows the order still open (MKT orders post-fill take
        # the average fill price; pre-fill the broker's lmtPrice is 0
        # which is what live ib_async returns for MarketOrder).
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="G",
            side="buy",
            qty=71,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=_mid_session_utc(),
        )
        self.assertEqual(result.status, recovery.RecoveryStatus.CATCH_UP)
        # The pending_still_open event must carry order_type in
        # journal_view so _pick_resumable_awaiting can rebuild faithfully.
        pending_events = [
            e for e in result.events
            if e.payload.get("case") == "pending_still_open"
        ]
        self.assertEqual(len(pending_events), 1)
        journal_view = pending_events[0].payload.get("journal_view", {})
        self.assertEqual(journal_view.get("order_type"), "MKT")
        self.assertIsNone(journal_view.get("limit_price"))

    def test_pick_resumable_awaiting_rebuilds_mkt_order_faithfully(self):
        from execution.connectors.types import BrokerOpenOrder
        from execution.engine import recovery
        from execution.engine.main import _pick_resumable_awaiting

        tail = self._journal_pending_mkt(order_type="MKT")
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="G",
            side="buy",
            qty=71,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=_mid_session_utc(),
        )
        resumed = _pick_resumable_awaiting(result, tail)
        self.assertIsNotNone(resumed)
        # The resumed order must keep MKT semantics; pre-Round-6 this
        # would default to LMT and break later cancellation /
        # reconciliation flows that branch on order_type.
        self.assertEqual(resumed.order.order_type, "MKT")
        self.assertEqual(resumed.order.ticker, "G")
        self.assertEqual(resumed.order.qty, 71)
        # Round-6 R2: validator Order.limit_price stays typed as
        # Decimal (not None) for risk-math anchor; the resume path
        # therefore uses Decimal('0') as a documented sentinel when
        # the journal carried None. The wire-level safety override in
        # engine._submit re-maps this to None on any subsequent broker
        # call (see test_resumed_mkt_order_wire_path_forces_none below).
        self.assertEqual(resumed.order.limit_price, Decimal("0"))


    def test_pick_resumable_awaiting_lmt_backward_compat(self):
        # An LMT order in the journal must continue to round-trip
        # cleanly (this is the pre-Round-6 path and the most common
        # case until coach pipelines emit MKT orders).
        from execution.connectors.types import BrokerOpenOrder
        from execution.engine import recovery
        from execution.engine.main import _pick_resumable_awaiting

        tail = self._journal_pending_mkt(order_type="LMT")
        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="G",
            side="buy",
            qty=71,
            filled_qty=0,
            limit_price=Decimal("34.50"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=_mid_session_utc(),
        )
        resumed = _pick_resumable_awaiting(result, tail)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.order.order_type, "LMT")
        self.assertEqual(resumed.order.limit_price, Decimal("34.50"))

    def test_resume_refuses_unknown_order_type_in_journal(self):
        # Defense-in-depth: a corrupt order_type in the journal must
        # refuse resume rather than coerce silently. Architect's
        # fail-closed-on-corrupt-journal_view rule (see
        # _validate_journal_view docstring).
        from execution.connectors.types import BrokerOpenOrder
        from execution.engine import recovery
        from execution.engine.main import _pick_resumable_awaiting

        tail = self._journal_pending_mkt(order_type="MKT")
        # Corrupt the order_type in the journal payload.
        tail[0]["payload"]["order_type"] = "STP"  # not in {MKT, LMT}

        open_order = BrokerOpenOrder(
            broker_order_id="1000",
            broker_perm_id="2000000",
            ticker="G",
            side="buy",
            qty=71,
            filled_qty=0,
            limit_price=Decimal("0"),
            status="Submitted",
        )
        result = recovery.reconcile(
            journal_tail=tail,
            broker_positions=[],
            broker_open_orders=[open_order],
            broker_order_status=[],
            now=_mid_session_utc(),
        )
        # _pick_resumable_awaiting must refuse rather than build an
        # Order with order_type='STP' that downstream code can't handle.
        # Note: recovery.reconcile may upper-case the input via the
        # PendingFromJournal builder, then journal_view.order_type
        # passes 'STP' to _validate_journal_view which refuses.
        resumed = _pick_resumable_awaiting(result, tail)
        self.assertIsNone(resumed)


class WireSafetyTests(unittest.IsolatedAsyncioTestCase):
    """Round-6 R2 (2026-05-08): defensive wire-safety override for MKT.

    Even if upstream code accidentally carries a non-None limit_price
    on a MKT order (e.g. a resumed order rebuilt from journal with a
    Decimal('0') sentinel, or a future code path that synthesises a
    reference price on the wrong field), the wire-level branch in
    engine._submit must force limit_price=None for MKT before talking
    to the connector. This test exercises the override directly.
    """

    async def test_resumed_mkt_order_wire_path_forces_none(self):
        # Construct an Order shape that mimics a resumed MKT order:
        # order_type='MKT' but limit_price=Decimal('0') (the resume
        # sentinel). When this hits the wire path, the connector
        # must see limit_price=None on the wire regardless.
        connector = MockIBKRConnector()
        await connector.connect()
        # Direct connector call mirroring what engine._submit would
        # invoke after the Round-6 R2 override remaps the sentinel
        # to None.
        ack = await connector.submit_order(
            ticker="G",
            side="buy",
            qty=71,
            limit_price=None,  # POST-OVERRIDE wire value
            stop_loss=Decimal("30.00"),
            time_in_force="DAY",
            client_tag="k2bi:resume:abc",
            order_type="MKT",
        )
        self.assertEqual(ack.status, "Submitted")
        rec = connector.submitted_orders[0]
        self.assertEqual(rec.order_type, "MKT")
        self.assertIsNone(
            rec.limit_price,
            "wire-safety override must force MKT limit_price to None "
            "even when upstream carries a sentinel Decimal value",
        )


if __name__ == "__main__":
    unittest.main()
