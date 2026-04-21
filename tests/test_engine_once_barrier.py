"""Tests for Q33 --once pre-exit barrier.

Q33 (architect scope 2026-04-21): the engine's `--once` mode exits
immediately after submitting an order, before IBKR's fill callback
has a chance to reach the journal writer. Session F's run 2/3 on
2026-04-20 hit this: paper order filled at broker, `order_filled`
never journaled, next `--once` run saw phantom_position mismatch.

Fix: after `run_once()`'s submit body returns with
`state_after=AWAITING_FILL`, enter a bounded pre-exit wait that polls
broker state until (a) pending clears, (b) wall-time reaches
`EngineConfig.once_exit_wait_seconds` (default 10.0). On timeout, emit
a new `once_exit_barrier_timeout` journal event with the architect-
specified payload so Q39-B recovery can promote evidence from
`crash_gap` to `barrier_timeout` on the next restart.

Event shape (architect 2026-04-21):
    event_type: once_exit_barrier_timeout
    payload: {
        barrier_seconds_elapsed: float,
        last_known_state: str,
        pending_orders: [
            {trade_id, broker_order_id, broker_perm_id, ticker,
             side, qty, limit_price, stop_loss},
            ...
        ],
    }
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    BrokerExecution,
    BrokerOpenOrder,
)
from execution.engine.main import (
    DEFAULT_TICK_SECONDS,
    AwaitingOrderState,
    Engine,
    EngineConfig,
    EngineState,
    TickResult,
)
from execution.journal.writer import JournalWriter
from execution.validators.types import Order

from tests.test_engine_main import (
    CONFIG,
    _mid_session_utc,
    _write_strategy,
)


def _awaiting_pending(
    *,
    trade_id: str = "T1",
    strategy: str = "spy-rotational",
    ticker: str = "SPY",
    side: str = "buy",
    qty: int = 10,
    limit_price: Decimal = Decimal("500"),
    stop_loss: Decimal | None = Decimal("495"),
    broker_order_id: str = "1",
    broker_perm_id: str = "2000000",
    submitted_at: datetime | None = None,
) -> AwaitingOrderState:
    when = submitted_at or _mid_session_utc()
    return AwaitingOrderState(
        trade_id=trade_id,
        strategy=strategy,
        order=Order(
            ticker=ticker,
            side=side,
            qty=qty,
            limit_price=limit_price,
            stop_loss=stop_loss,
            strategy=strategy,
            submitted_at=when,
        ),
        broker_order_id=broker_order_id,
        broker_perm_id=broker_perm_id,
        submitted_at=when,
        filled_qty=0,
    )


class OnceExitBarrierTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"
        _write_strategy(self.strategies_dir)

        self.connector = MockIBKRConnector()
        self.connector.marks = {"SPY": Decimal("500")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test01")
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
                # Short barrier window for fast tests.
                once_exit_wait_seconds=0.5,
            ),
        )
        # Engine comes up connected so the barrier's inner tick loop
        # does not detour through INIT -- we drive barrier behavior
        # directly from AWAITING_FILL.
        await self.connector.connect()
        self.engine.state = EngineState.AWAITING_FILL
        self.engine._init_completed = True

    async def asyncTearDown(self):
        self._tmp.cleanup()

    def _journal_events(self) -> list[dict]:
        return self.journal.read_all()

    def _event_types(self) -> list[str]:
        return [e["event_type"] for e in self._journal_events()]

    async def test_barrier_reconciles_pre_existing_fill_before_exit(self):
        """If the broker execution already reached the mock before the
        barrier polls, the barrier's first tick must reconcile the fill
        and clear pending. No barrier-timeout event emitted because
        the barrier resolved cleanly."""
        pending = _awaiting_pending()
        self.engine._pending_order = pending
        # Broker side: order is no longer in open_orders; execution is
        # already in the history. Mock's open_orders defaults to empty.
        self.connector.executions_history.append(
            BrokerExecution(
                exec_id="E1",
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                price=Decimal("500.05"),
                filled_at=_mid_session_utc(),
            )
        )
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        self.assertIsNone(self.engine._pending_order)
        self.assertEqual(self.engine.state, EngineState.CONNECTED_IDLE)
        types = self._event_types()
        self.assertIn("order_filled", types)
        self.assertNotIn("once_exit_barrier_timeout", types)

    async def test_barrier_times_out_and_emits_event(self):
        """Broker keeps the order open for longer than the barrier
        window. Barrier must emit once_exit_barrier_timeout and return
        with the pending still intact for the next run's recovery."""
        pending = _awaiting_pending()
        self.engine._pending_order = pending
        # Broker still shows the order as open -- never fills.
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            )
        ]
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        # Pending remains -- the next run's recovery will reconcile.
        self.assertIsNotNone(self.engine._pending_order)
        self.assertEqual(self.engine.state, EngineState.AWAITING_FILL)
        types = self._event_types()
        self.assertIn("once_exit_barrier_timeout", types)

    async def test_barrier_timeout_payload_matches_architect_shape(self):
        """Architect 2026-04-21: the event payload must carry
        barrier_seconds_elapsed, last_known_state, and a pending_orders
        list of {trade_id, broker_order_id, broker_perm_id, ticker,
        side, qty, limit_price, stop_loss}. Q39-B recovery reads the
        trade_ids here to promote evidence."""
        pending = _awaiting_pending(
            trade_id="T42",
            broker_order_id="17",
            broker_perm_id="222703140",
            qty=2,
            limit_price=Decimal("715"),
            stop_loss=Decimal("697.13"),
        )
        self.engine._pending_order = pending
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=2,
                filled_qty=0,
                limit_price=Decimal("715"),
                status="Submitted",
                tif="DAY",
            )
        ]
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        events = self._journal_events()
        barrier_events = [
            e for e in events if e["event_type"] == "once_exit_barrier_timeout"
        ]
        self.assertEqual(len(barrier_events), 1)
        payload = barrier_events[0]["payload"]
        self.assertIn("barrier_seconds_elapsed", payload)
        self.assertIsInstance(payload["barrier_seconds_elapsed"], float)
        self.assertGreater(payload["barrier_seconds_elapsed"], 0)
        self.assertEqual(
            payload["last_known_state"], EngineState.AWAITING_FILL.value
        )
        self.assertIn("pending_orders", payload)
        self.assertIsInstance(payload["pending_orders"], list)
        self.assertEqual(len(payload["pending_orders"]), 1)
        entry = payload["pending_orders"][0]
        expected_keys = {
            "trade_id",
            "broker_order_id",
            "broker_perm_id",
            "ticker",
            "side",
            "qty",
            "limit_price",
            "stop_loss",
        }
        self.assertEqual(set(entry.keys()), expected_keys)
        self.assertEqual(entry["trade_id"], "T42")
        self.assertEqual(entry["broker_order_id"], "17")
        self.assertEqual(entry["broker_perm_id"], "222703140")
        self.assertEqual(entry["ticker"], "SPY")
        self.assertEqual(entry["side"], "buy")
        self.assertEqual(entry["qty"], 2)
        self.assertEqual(entry["limit_price"], "715")
        self.assertEqual(entry["stop_loss"], "697.13")

    async def test_barrier_noop_when_no_pending(self):
        """If the engine is not in AWAITING_FILL (nothing to wait on),
        the barrier returns immediately without journaling."""
        self.engine.state = EngineState.CONNECTED_IDLE
        self.engine._pending_order = None
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.CONNECTED_IDLE,
                state_after=EngineState.CONNECTED_IDLE,
            )
        )
        self.assertEqual(self._event_types(), [])

    async def test_run_once_invokes_barrier_on_awaiting_fill(self):
        """Q33 integration: run_once() must trigger the barrier after
        the submit body returns state_after=AWAITING_FILL. Verified by
        mocking a full pipeline that leaves a pending order unresolved
        -- run_once should therefore journal once_exit_barrier_timeout
        before returning."""
        import tempfile as _tmp

        tmp = _tmp.TemporaryDirectory()
        try:
            base = Path(tmp.name)
            journal_dir = base / "journal"
            journal_dir.mkdir()
            strategies_dir = base / "strategies"
            strategies_dir.mkdir()
            kill_path = base / ".killed"
            _write_strategy(strategies_dir)

            connector = MockIBKRConnector()
            connector.marks = {"SPY": Decimal("500")}
            journal = JournalWriter(base_dir=journal_dir, git_sha="test02")
            engine = Engine(
                connector=connector,
                journal=journal,
                validator_config=CONFIG,
                engine_config=EngineConfig(
                    tick_seconds=DEFAULT_TICK_SECONDS,
                    strategies_dir=strategies_dir,
                    kill_path=kill_path,
                    once_exit_wait_seconds=0.3,
                ),
            )
            # Drive a full run_once at a valid session time. Submit
            # body leaves us in AWAITING_FILL; mock keeps the order
            # open so the barrier times out.
            import execution.engine.main as main_mod
            from datetime import datetime as real_dt

            patched = _mid_session_utc()

            class _PatchedDT(real_dt):
                @classmethod
                def now(cls, tz=None):
                    return patched if tz is None else patched.astimezone(tz)

            orig_dt = main_mod.datetime
            main_mod.datetime = _PatchedDT

            def _mock_submit_hook(record):
                # Park the submitted order in broker open_orders so the
                # barrier polls it as still-open rather than immediately
                # reconciling.
                connector.open_orders.append(
                    BrokerOpenOrder(
                        broker_order_id=record.broker_order_id,
                        broker_perm_id=record.broker_perm_id,
                        ticker=record.ticker,
                        side=record.side,
                        qty=record.qty,
                        filled_qty=0,
                        limit_price=record.limit_price,
                        status="Submitted",
                        tif=record.time_in_force,
                    )
                )
                from execution.connectors.types import BrokerOrderAck

                return BrokerOrderAck(
                    broker_order_id=record.broker_order_id,
                    broker_perm_id=record.broker_perm_id,
                    submitted_at=patched,
                    status="Submitted",
                )

            connector.submit_hook = _mock_submit_hook
            try:
                await engine.run_once()
            finally:
                main_mod.datetime = orig_dt

            types = [e["event_type"] for e in journal.read_all()]
            self.assertIn("order_submitted", types)
            self.assertIn("once_exit_barrier_timeout", types)
        finally:
            tmp.cleanup()

    async def test_barrier_breaks_early_on_kill_file(self):
        """MiniMax Q33 R1 finding #2 (2026-04-21): the barrier uses
        _poll_awaiting directly and bypasses tick_once's kill-file
        check. A human writing .killed mid-barrier must be honored
        within the barrier's poll cadence, not ignored for the full
        wait window."""
        pending = _awaiting_pending()
        self.engine._pending_order = pending
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            )
        ]
        # Long barrier window so ONLY the kill check can break it.
        self.engine.engine_config.once_exit_wait_seconds = 5.0
        # Pre-write .killed before the barrier starts so the first
        # in-loop kill check fires.
        self.kill_path.write_text("q33-barrier-kill", encoding="utf-8")
        start = datetime.now(timezone.utc)
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        # Should exit well before the 5s wall-clock window.
        self.assertLess(elapsed, 2.0)
        # Kill transition observed: no barrier-timeout event (state
        # transitioned out of AWAITING_FILL via the kill handler).
        types = self._event_types()
        self.assertNotIn("once_exit_barrier_timeout", types)

    async def test_config_caps_once_exit_wait_seconds(self):
        """MiniMax Q33 R1 finding #3 (2026-04-21): a misconfigured
        deployment setting once_exit_wait_seconds to 86400 or infinity
        would hang --once for that duration. EngineConfig must cap the
        value at ONCE_EXIT_WAIT_SECONDS_MAX (300s) in __post_init__ so
        neither direct construction nor YAML loading can land an
        unreasonable window."""
        import execution.engine.main as main_mod

        # Direct construction path.
        cfg = EngineConfig(once_exit_wait_seconds=86400.0)
        self.assertLessEqual(
            cfg.once_exit_wait_seconds,
            main_mod.ONCE_EXIT_WAIT_SECONDS_MAX,
        )
        # YAML loader path.
        cfg2 = main_mod._engine_config_from_dict(
            {"once_exit_wait_seconds": 86400.0}
        )
        self.assertLessEqual(
            cfg2.once_exit_wait_seconds,
            main_mod.ONCE_EXIT_WAIT_SECONDS_MAX,
        )
        # Negative / zero still disables the barrier (pre-existing
        # behavior); post_init must not clamp those to the cap.
        cfg3 = EngineConfig(once_exit_wait_seconds=0.0)
        self.assertEqual(cfg3.once_exit_wait_seconds, 0.0)

    async def test_barrier_breaks_on_disconnect_without_timeout_event(self):
        """MiniMax Q33 R2 finding #1 regression guard (2026-04-21):
        if _poll_awaiting raises DisconnectedError, state transitions
        to DISCONNECTED inside the poll. The barrier loop checks state
        AFTER the poll and must break cleanly -- no timeout event
        emitted, no hanging loop, pending left intact for next-run
        recovery (which Q39-B classifies as crash_gap per the evidence
        tiering rule). The reviewer hypothesized the loop would
        continue past a DISCONNECTED transition; pin the correct
        behavior here."""
        pending = _awaiting_pending()
        self.engine._pending_order = pending
        # Force _poll_awaiting to raise DisconnectedError on its first
        # broker call by making the connector's get_executions_since
        # raise.
        from execution.connectors.types import DisconnectedError

        async def _raise_disconnect(since):
            raise DisconnectedError("mid-barrier disconnect")

        self.connector.get_executions_since = _raise_disconnect
        self.engine.engine_config.once_exit_wait_seconds = 1.0
        start = datetime.now(timezone.utc)
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        # Should exit immediately once state flips to DISCONNECTED,
        # not wait the full 1s window.
        self.assertLess(elapsed, 0.5)
        self.assertEqual(self.engine.state, EngineState.DISCONNECTED)
        types = self._event_types()
        self.assertNotIn("once_exit_barrier_timeout", types)

    async def test_run_once_returns_killed_state_when_kill_fires_during_barrier(
        self,
    ):
        """MiniMax Q33 R2 finding #2 (2026-04-21): a kill mid-barrier
        must be reflected in the TickResult the caller gets back. The
        barrier transitions self.state to KILLED but without this fix,
        run_once returns body_result with the stale state_after=
        AWAITING_FILL, hiding the kill from the caller."""
        import tempfile as _tmp

        tmp = _tmp.TemporaryDirectory()
        try:
            base = Path(tmp.name)
            journal_dir = base / "journal"
            journal_dir.mkdir()
            strategies_dir = base / "strategies"
            strategies_dir.mkdir()
            kill_path = base / ".killed"
            _write_strategy(strategies_dir)

            connector = MockIBKRConnector()
            connector.marks = {"SPY": Decimal("500")}
            journal = JournalWriter(base_dir=journal_dir, git_sha="test03")
            engine = Engine(
                connector=connector,
                journal=journal,
                validator_config=CONFIG,
                engine_config=EngineConfig(
                    tick_seconds=DEFAULT_TICK_SECONDS,
                    strategies_dir=strategies_dir,
                    kill_path=kill_path,
                    once_exit_wait_seconds=2.0,
                ),
            )

            def _mock_submit_hook(record):
                # Park order as still open so barrier keeps waiting.
                connector.open_orders.append(
                    BrokerOpenOrder(
                        broker_order_id=record.broker_order_id,
                        broker_perm_id=record.broker_perm_id,
                        ticker=record.ticker,
                        side=record.side,
                        qty=record.qty,
                        filled_qty=0,
                        limit_price=record.limit_price,
                        status="Submitted",
                        tif=record.time_in_force,
                    )
                )
                # Write .killed immediately after submit so the
                # barrier's first kill-check sees it.
                kill_path.write_text("mid-barrier", encoding="utf-8")
                from execution.connectors.types import BrokerOrderAck

                return BrokerOrderAck(
                    broker_order_id=record.broker_order_id,
                    broker_perm_id=record.broker_perm_id,
                    submitted_at=_mid_session_utc(),
                    status="Submitted",
                )

            connector.submit_hook = _mock_submit_hook

            import execution.engine.main as main_mod
            from datetime import datetime as real_dt

            patched = _mid_session_utc()

            class _PatchedDT(real_dt):
                @classmethod
                def now(cls, tz=None):
                    return patched if tz is None else patched.astimezone(tz)

            orig_dt = main_mod.datetime
            main_mod.datetime = _PatchedDT
            try:
                result = await engine.run_once()
            finally:
                main_mod.datetime = orig_dt

            # Caller must see KILLED, not the stale AWAITING_FILL.
            self.assertEqual(result.state_after, EngineState.KILLED)
        finally:
            tmp.cleanup()

    async def test_barrier_journal_write_failure_does_not_silently_swallow(
        self,
    ):
        """MiniMax Q33 R2 finding #3 (2026-04-21): if the timeout
        journal.append fails (disk full, flock contention, etc.), the
        barrier must surface the failure -- silently swallowing leaves
        the pending order in undetectable limbo. Fail-loud matches
        CLAUDE.md's hard-rules discipline (loud data-integrity
        failures over silent drops)."""
        pending = _awaiting_pending()
        self.engine._pending_order = pending
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            )
        ]
        self.engine.engine_config.once_exit_wait_seconds = 0.3

        # Poison the journal so the timeout append raises.
        orig_append = self.engine.journal.append

        class _JournalWriteError(RuntimeError):
            pass

        def _failing_append(event_type, payload, **kw):
            if event_type == "once_exit_barrier_timeout":
                raise _JournalWriteError("simulated disk full")
            return orig_append(event_type, payload, **kw)

        self.engine.journal.append = _failing_append
        with self.assertRaises(_JournalWriteError):
            await self.engine._once_exit_barrier(
                TickResult(
                    state_before=EngineState.AWAITING_FILL,
                    state_after=EngineState.AWAITING_FILL,
                )
            )

    async def test_q39b_consumes_barrier_timeout_event(self):
        """MiniMax Q33 R1 finding #1 integration test: Q39-B recovery
        already shipped in the preceding Q39 commits, but finding #1
        asked for a cross-module test proving the loop closes.

        Scenario: engine's Q33 barrier times out and writes the event;
        on the next restart, recovery reads that exact event shape and
        promotes evidence=barrier_timeout for the pending's trade_id.
        """
        import json

        pending = _awaiting_pending(
            trade_id="T_Q33_INTEG",
            broker_perm_id="P_Q33_INTEG",
            limit_price=Decimal("500"),
            stop_loss=Decimal("495"),
        )
        self.engine._pending_order = pending
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            )
        ]
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        # Read the journal back and simulate a Run-2 recovery pass
        # that consumes both order_submitted (hypothetical) + the
        # barrier event we just wrote.
        journal_records = self.journal.read_all()
        barrier = [
            r for r in journal_records
            if r["event_type"] == "once_exit_barrier_timeout"
        ]
        self.assertEqual(len(barrier), 1)

        from execution.engine import recovery as recovery_mod

        # Q39-B's promotion hook: _barrier_timeout_trade_ids must
        # extract the trade_id so evidence is promoted on next
        # recovery. This is the proof the loop closes end-to-end.
        extracted = recovery_mod._barrier_timeout_trade_ids(
            journal_records
        )
        self.assertIn(pending.trade_id, extracted)

    async def test_barrier_respects_custom_timeout_config(self):
        """once_exit_wait_seconds on EngineConfig governs the barrier
        window. A shorter window must observably shorten the wait."""
        pending = _awaiting_pending()
        self.engine._pending_order = pending
        self.connector.open_orders = [
            BrokerOpenOrder(
                broker_order_id=pending.broker_order_id,
                broker_perm_id=pending.broker_perm_id,
                ticker="SPY",
                side="buy",
                qty=10,
                filled_qty=0,
                limit_price=Decimal("500"),
                status="Submitted",
                tif="DAY",
            )
        ]
        self.engine.engine_config.once_exit_wait_seconds = 0.2
        start = datetime.now(timezone.utc)
        await self.engine._once_exit_barrier(
            TickResult(
                state_before=EngineState.AWAITING_FILL,
                state_after=EngineState.AWAITING_FILL,
            )
        )
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        # Wide tolerance so the test is not flaky on a loaded machine,
        # but tight enough to prove the window is shorter than default.
        self.assertLess(elapsed, 3.0)
        events = self.journal.read_all()
        barrier_events = [
            e for e in events if e["event_type"] == "once_exit_barrier_timeout"
        ]
        self.assertEqual(len(barrier_events), 1)
        self.assertLess(
            barrier_events[0]["payload"]["barrier_seconds_elapsed"], 3.0
        )
