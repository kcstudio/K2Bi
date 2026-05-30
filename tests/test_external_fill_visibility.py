"""Phase 4 P4-1 external-fill visibility tests."""

from __future__ import annotations

import asyncio
import inspect
import sys
import tempfile
import threading
import types
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from execution.connectors import types as connector_types
from execution.connectors.ibkr import IBKRConnector
from execution.connectors.mock import MockIBKRConnector
from execution.connectors.types import (
    BrokerPosition,
    POSITION_SOURCE_LIVE_REQ_POSITIONS,
    PositionSnapshot,
)
from execution.engine.main import (
    Engine,
    EngineConfig,
    EngineState,
    JournalDurabilityError,
    _ExternalFillHandoffItem,
)
from execution.journal.writer import JournalWriter


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


def _observation(**overrides):
    cls = getattr(connector_types, "BrokerFillObservation")
    payload = {
        "ticker": "G",
        "side": "sell",
        "qty": 71,
        "price": Decimal("29.93"),
        "filled_at": datetime(2026, 5, 13, 14, 26, 23, tzinfo=timezone.utc),
        "observed_at": datetime(2026, 5, 13, 14, 26, 24, tzinfo=timezone.utc),
        "broker_order_id": "96",
        "broker_perm_id": "1677427049",
        "exec_id": "0001.abc",
        "client_tag": "k2bi:g-2026-05_2nd-wave-paper-trade:T1:stop",
        "source": "trade_fill_event",
    }
    payload.update(overrides)
    return cls(**payload)


def _fill(
    *,
    side: str = "SLD",
    shares: int = 71,
    price: str = "29.93",
    order_id: int = 96,
    perm_id: int = 1677427049,
    exec_id: str = "0001.abc",
    filled_at: datetime | None = None,
):
    return types.SimpleNamespace(
        execution=types.SimpleNamespace(
            side=side,
            shares=shares,
            price=price,
            orderId=order_id,
            permId=perm_id,
            execId=exec_id,
            clientId=1,
            time=filled_at
            or datetime(2026, 5, 13, 14, 26, 23, tzinfo=timezone.utc),
        )
    )


class MockExternalFillObserverTests(unittest.TestCase):
    def test_mock_emit_noops_without_observer_and_calls_registered_observer(self):
        connector = MockIBKRConnector()
        observation = _observation()

        connector.emit_external_fill_observed(observation)

        seen = []
        connector.set_external_fill_observer(seen.append)
        connector.emit_external_fill_observed(observation)

        self.assertEqual(seen, [observation])

    def test_mock_observer_registration_rejects_invalid_callback(self):
        connector = MockIBKRConnector()

        with self.assertRaises(TypeError):
            connector.set_external_fill_observer("not-callable")  # type: ignore[arg-type]

        async def async_observer(_observation):
            return None

        with self.assertRaises(TypeError):
            connector.set_external_fill_observer(async_observer)  # type: ignore[arg-type]

        def zero_arg_observer():
            return None

        with self.assertRaises(TypeError):
            connector.set_external_fill_observer(zero_arg_observer)  # type: ignore[arg-type]

    def test_mock_emit_catches_observer_exception_like_live_connector(self):
        connector = MockIBKRConnector()

        def raising_observer(_observation):
            raise RuntimeError("journal unavailable")

        connector.set_external_fill_observer(raising_observer)

        with self.assertLogs("k2bi.connector.mock", level="WARNING") as logs:
            connector.emit_external_fill_observed(_observation())

        self.assertTrue(
            any("external fill observer raised" in line for line in logs.output)
        )


async def _wait_for_event(journal: JournalWriter, event_type: str, count: int = 1):
    for _ in range(50):
        events = [e for e in journal.read_all() if e["event_type"] == event_type]
        if len(events) >= count:
            return events
        await asyncio.sleep(0.01)
    return [e for e in journal.read_all() if e["event_type"] == event_type]


class EngineExternalFillObserverTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal = JournalWriter(base_dir=self.base / "journal", git_sha="p4-1")
        self.connector = MockIBKRConnector()
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    async def test_mock_observation_journals_external_fill_without_mutating_positions(self):
        old_position = BrokerPosition(ticker="G", qty=71, avg_price=Decimal("30.32"))
        self.engine._positions = [old_position]

        self.connector.emit_external_fill_observed(_observation())
        await _wait_for_event(self.journal, "external_fill_observed")

        self.assertEqual(self.engine._positions, [old_position])
        events = self._events("external_fill_observed")
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["strategy"], "g-2026-05_2nd-wave-paper-trade")
        self.assertEqual(event["trade_id"], "T1")
        self.assertEqual(event["ticker"], "G")
        self.assertEqual(event["side"], "sell")
        self.assertEqual(event["qty"], 71)
        self.assertEqual(event["broker_order_id"], "96")
        self.assertEqual(event["broker_perm_id"], "1677427049")
        self.assertTrue(event["payload"]["is_stop_child"])
        self.assertEqual(event["payload"]["source"], "trade_fill_event")

    async def test_malformed_observation_logs_warning_and_journals_dropped_event(self):
        with self.assertLogs("k2bi.engine", level="WARNING"):
            self.connector.emit_external_fill_observed(
                _observation(qty=0, client_tag="not-k2bi")
            )
            await _wait_for_event(self.journal, "external_fill_malformed")

        self.assertEqual(self._events("external_fill_observed"), [])
        malformed = self._events("external_fill_malformed")
        self.assertEqual(len(malformed), 1)
        payload = malformed[0]["payload"]
        self.assertEqual(payload["client_tag"], "not-k2bi")
        self.assertEqual(payload["ticker"], "G")
        self.assertEqual(payload["qty"], 0)
        self.assertEqual(payload["exception_type"], "ValueError")
        self.assertIn("client_tag", payload["exception_message"])

    async def test_malformed_observation_journal_failure_logs_raw_observation(self):
        def fail_malformed(event_type, *args, **kwargs):  # type: ignore[no-untyped-def]
            if event_type == "external_fill_malformed":
                raise JournalDurabilityError("malformed audit fsync failed")
            return None

        self.journal.append = fail_malformed  # type: ignore[method-assign]

        with self.assertLogs("k2bi.engine", level="ERROR") as logs:
            self.connector.emit_external_fill_observed(
                _observation(qty=0, client_tag="not-k2bi")
            )
            for _ in range(50):
                await asyncio.sleep(0.01)
                if self.engine._shutdown_requested:
                    break

        joined = "\n".join(logs.output)
        self.assertIn("external_fill_malformed raw observation", joined)
        self.assertIn("not-k2bi", joined)

    async def test_non_observation_payload_journals_malformed_type_event(self):
        with self.assertLogs("k2bi.engine", level="WARNING"):
            self.engine._observe_external_fill({"client_tag": "not-an-observation"})  # type: ignore[arg-type]

        malformed = self._events("external_fill_malformed_type")
        self.assertEqual(len(malformed), 1)
        payload = malformed[0]["payload"]
        self.assertEqual(payload["observation_type"], "dict")
        self.assertIn("BrokerFillObservation", payload["expected_type"])

    async def test_live_connector_malformed_fill_event_reaches_engine_audit(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        engine = Engine(
            connector=connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )
        old_position = BrokerPosition(ticker="G", qty=71, avg_price=Decimal("30.32"))
        engine._positions = [old_position]
        order = _FakeStopOrder("SELL", 71, 29.93, tif="GTC")
        order.orderId = 96
        order.permId = 1677427049
        order.orderRef = "not-k2bi"
        trade = _FakeTrade(_FakeStock("G", "SMART", "USD"), order)

        with self.assertLogs("k2bi.engine", level="WARNING"):
            connector._on_trade_fill_event(
                trade,
                _fill(
                    order_id=96,
                    perm_id=1677427049,
                    exec_id="bad-tag-fill",
                    filled_at=datetime(2026, 5, 28, 15, 15, tzinfo=timezone.utc),
                ),
            )
            await _wait_for_event(self.journal, "external_fill_malformed")

        self.assertEqual(engine._positions, [old_position])
        self.assertEqual(self._events("external_fill_observed"), [])
        malformed = self._events("external_fill_malformed")
        self.assertEqual(len(malformed), 1)
        self.assertEqual(malformed[0]["payload"]["client_tag"], "not-k2bi")
        self.assertEqual(malformed[0]["payload"]["exec_id"], "bad-tag-fill")

    async def test_live_connector_fill_conversion_failure_reaches_engine_audit(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        engine = Engine(
            connector=connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )
        old_position = BrokerPosition(ticker="G", qty=71, avg_price=Decimal("30.32"))
        engine._positions = [old_position]
        order = _FakeStopOrder("SELL", 71, 29.93, tif="GTC")
        order.orderId = 96
        order.permId = 1677427049
        order.orderRef = "k2bi:g-2026-05_2nd-wave-paper-trade:T1:stop"
        trade = _FakeTrade(_FakeStock("G", "SMART", "USD"), order)
        trade.orderStatus.status = "Filled"
        connector._attach_external_fill_observer(trade)

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING"):
            trade.fillEvent.emit(trade, types.SimpleNamespace())
            await _wait_for_event(self.journal, "external_fill_malformed")

        self.assertEqual(engine._positions, [old_position])
        self.assertEqual(self._events("external_fill_observed"), [])
        malformed = self._events("external_fill_malformed")
        self.assertEqual(len(malformed), 1)
        payload = malformed[0]["payload"]
        self.assertEqual(payload["client_tag"], order.orderRef)
        self.assertEqual(payload["broker_order_id"], "96")
        self.assertEqual(payload["broker_perm_id"], "1677427049")
        self.assertEqual(payload["exec_id"], "fill_event_conversion_failed")
        self.assertEqual(payload["qty"], 0)
        self.assertEqual(
            connector._fill_observations_by_trade_id,
            {},
        )
        self.assertIsNone(
            connector._filled_ack_if_trade_filled(
                trade,
                submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
            )
        )

    async def test_observed_journal_failure_requests_shutdown_without_malformed_audit(self):
        calls = []
        real_append = self.journal.append

        def fail_observed(event_type, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(event_type)
            if event_type == "external_fill_observed":
                raise JournalDurabilityError("external fill observed fsync failed")
            return real_append(event_type, *args, **kwargs)

        self.journal.append = fail_observed  # type: ignore[method-assign]

        with self.assertLogs("k2bi.engine", level="ERROR"):
            self.connector.emit_external_fill_observed(_observation())
            await asyncio.sleep(0)

        self.assertEqual(calls, ["external_fill_observed"])
        self.assertEqual(self.engine.state, EngineState.SHUTDOWN)
        self.assertTrue(self.engine._shutdown_requested)
        self.assertEqual(self._events("external_fill_observed"), [])
        self.assertEqual(self._events("external_fill_malformed"), [])

    async def test_unavailable_journal_failure_requests_shutdown_without_malformed_audit(self):
        calls = []
        real_append = self.journal.append

        def fail_unavailable(event_type, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(event_type)
            if event_type == "external_fill_event_unavailable":
                raise JournalDurabilityError("external fill unavailable fsync failed")
            return real_append(event_type, *args, **kwargs)

        self.journal.append = fail_unavailable  # type: ignore[method-assign]
        observation = _observation(
            qty=0,
            price=Decimal("0"),
            exec_id="fill_event_unavailable",
            source="fill_event_unavailable",
        )

        with self.assertLogs("k2bi.engine", level="ERROR"):
            self.engine._observe_external_fill(observation)

        self.assertEqual(calls, ["external_fill_event_unavailable"])
        self.assertEqual(self.engine.state, EngineState.SHUTDOWN)
        self.assertTrue(self.engine._shutdown_requested)
        self.assertEqual(self._events("external_fill_event_unavailable"), [])
        self.assertEqual(self._events("external_fill_malformed"), [])

    async def test_threaded_connector_callback_journals_only_after_engine_loop_drain(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        engine = Engine(
            connector=connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )
        trade = _FakeTrade(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.order.orderId = 96
        trade.order.permId = 1677427049
        trade.order.orderRef = "k2bi:g-2026-05_2nd-wave-paper-trade:T1:stop"
        append_threads: list[str] = []
        real_append = self.journal.append

        def recording_append(event_type, *args, **kwargs):  # type: ignore[no-untyped-def]
            if event_type == "external_fill_observed":
                append_threads.append(threading.current_thread().name)
            return real_append(event_type, *args, **kwargs)

        self.journal.append = recording_append  # type: ignore[method-assign]

        callback_thread = threading.Thread(
            target=lambda: connector._on_trade_fill_event(trade, _fill()),
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)

        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(self._events("external_fill_observed"), [])

        events = await _wait_for_event(self.journal, "external_fill_observed")

        self.assertEqual(len(events), 1)
        self.assertNotEqual(append_threads, ["ibkr-fill-callback"])
        self.assertIs(engine._positions_lock, engine._positions_lock)

    async def test_threaded_journal_failure_requests_shutdown_on_engine_loop_only(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        engine = Engine(
            connector=connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )
        trade = _FakeTrade(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.order.orderId = 96
        trade.order.permId = 1677427049
        trade.order.orderRef = "k2bi:g-2026-05_2nd-wave-paper-trade:T1:stop"
        calls: list[str] = []

        def fail_observed(event_type, *args, **kwargs):  # type: ignore[no-untyped-def]
            calls.append(f"{event_type}:{threading.current_thread().name}")
            if event_type == "external_fill_observed":
                raise JournalDurabilityError("external fill observed fsync failed")
            return None

        self.journal.append = fail_observed  # type: ignore[method-assign]

        callback_thread = threading.Thread(
            target=lambda: connector._on_trade_fill_event(trade, _fill()),
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)

        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(engine.state, EngineState.INIT)
        self.assertFalse(engine._shutdown_requested)

        with self.assertLogs("k2bi.engine", level="ERROR"):
            for _ in range(50):
                await asyncio.sleep(0.01)
                if engine._shutdown_requested:
                    break

        self.assertTrue(engine._shutdown_requested)
        self.assertEqual(engine.state, EngineState.SHUTDOWN)
        self.assertTrue(all("ibkr-fill-callback" not in call for call in calls))
        self.assertEqual(
            engine._external_fill_observer_lifecycle,
            "stopped",
        )
        self.assertIsNone(connector._external_fill_observer)

        calls_after_shutdown = list(calls)
        connector._on_trade_fill_event(
            trade,
            _fill(exec_id="post-shutdown-fill"),
        )
        await asyncio.sleep(0)
        self.assertEqual(calls, calls_after_shutdown)

    async def test_lifecycle_stopped_callback_drops_without_scheduling_or_journaling(self):
        self.engine._external_fill_observer_lifecycle = "stopped"

        self.connector.emit_external_fill_observed(_observation())
        await asyncio.sleep(0)

        self.assertEqual(self._events("external_fill_observed"), [])
        self.assertEqual(self.engine.state, EngineState.INIT)

    async def test_bounded_handoff_overflow_emits_coalesced_drop_audit(self):
        connector = MockIBKRConnector()
        journal = JournalWriter(base_dir=self.base / "overflow-journal", git_sha="p4-1")
        engine = Engine(
            connector=connector,
            journal=journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                strategies_dir=self.base / "strategies",
                external_fill_handoff_limit=2,
            ),
        )

        callback_thread = threading.Thread(
            target=lambda: [
                connector.emit_external_fill_observed(
                    _observation(exec_id=f"overflow-{idx}")
                )
                for idx in range(3)
            ],
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)
        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(journal.read_all(), [])

        observed = await _wait_for_event(journal, "external_fill_observed", count=2)
        dropped = await _wait_for_event(journal, "external_fill_handoff_dropped")

        self.assertEqual([e["payload"]["handoff_sequence"] for e in observed], [1, 2])
        self.assertEqual(len(dropped), 1)
        drop_payload = dropped[0]["payload"]
        self.assertEqual(drop_payload["dropped_count"], 1)
        self.assertEqual(drop_payload["first_dropped_sequence"], 3)
        self.assertEqual(drop_payload["last_dropped_sequence"], 3)
        self.assertEqual(drop_payload["observer_epoch"], engine._external_fill_observer_epoch)
        self.assertEqual(engine.external_fill_handoff_metrics()["cumulative_dropped"], 1)

    async def test_fifo_handoff_assigns_monotonic_sequences_in_drain_order(self):
        callback_thread = threading.Thread(
            target=lambda: [
                self.connector.emit_external_fill_observed(
                    _observation(exec_id=f"seq-{idx}")
                )
                for idx in range(3)
            ],
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)
        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(self._events("external_fill_observed"), [])

        observed = await _wait_for_event(self.journal, "external_fill_observed", count=3)

        self.assertEqual(
            [event["payload"]["exec_id"] for event in observed],
            ["seq-0", "seq-1", "seq-2"],
        )
        self.assertEqual(
            [event["payload"]["handoff_sequence"] for event in observed],
            [1, 2, 3],
        )

    async def test_shutdown_drains_pending_observations_and_drop_audit_before_stopped(self):
        connector = MockIBKRConnector()
        journal = JournalWriter(base_dir=self.base / "shutdown-journal", git_sha="p4-1")
        engine = Engine(
            connector=connector,
            journal=journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                strategies_dir=self.base / "strategies",
                external_fill_handoff_limit=1,
            ),
        )
        callback_thread = threading.Thread(
            target=lambda: [
                connector.emit_external_fill_observed(
                    _observation(exec_id=f"shutdown-{idx}")
                )
                for idx in range(3)
            ],
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)

        await engine._shutdown()

        event_types = [event["event_type"] for event in journal.read_all()]
        self.assertIn("external_fill_observed", event_types)
        self.assertIn("external_fill_handoff_dropped", event_types)
        self.assertEqual(engine._external_fill_observer_lifecycle, "stopped")

    async def test_drain_reentrancy_guard_prevents_double_consuming_sequences(self):
        self.connector.emit_external_fill_observed(_observation(exec_id="reentrant"))
        self.engine._external_fill_drain_in_progress = True

        self.engine._drain_external_fill_handoff()
        await asyncio.sleep(0)

        self.assertEqual(self._events("external_fill_observed"), [])

        self.engine._external_fill_drain_in_progress = False
        self.engine._drain_external_fill_handoff()

        observed = await _wait_for_event(self.journal, "external_fill_observed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["payload"]["handoff_sequence"], 1)

    async def test_epoch_reset_drains_pending_observations_before_sequence_reset(self):
        callback_thread = threading.Thread(
            target=lambda: self.connector.emit_external_fill_observed(
                _observation(exec_id="pre-reset")
            ),
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)
        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(self._events("external_fill_observed"), [])

        self.engine._reset_external_fill_handoff_epoch()

        observed = self._events("external_fill_observed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["payload"]["exec_id"], "pre-reset")
        self.assertEqual(observed[0]["payload"]["observer_epoch"], 1)
        self.assertEqual(self.engine._external_fill_observer_epoch, 2)

    async def test_epoch_reset_audits_pending_observations_left_after_drain(self):
        real_drain = self.engine._drain_external_fill_handoff

        def drain_then_leave_pending_race_item() -> None:
            real_drain()
            self.engine._external_fill_pending.append(
                _ExternalFillHandoffItem(
                    sequence=7,
                    observer_epoch=self.engine._external_fill_observer_epoch,
                    observation=_observation(exec_id="reset-race"),
                    accepted_at=datetime.now(timezone.utc),
                )
            )

        self.engine._drain_external_fill_handoff = drain_then_leave_pending_race_item  # type: ignore[method-assign]

        self.engine._reset_external_fill_handoff_epoch()

        self.assertEqual(self._events("external_fill_observed"), [])
        dropped = self._events("external_fill_handoff_dropped")
        self.assertEqual(len(dropped), 1)
        payload = dropped[0]["payload"]
        self.assertEqual(payload["drop_reason"], "epoch_reset")
        self.assertEqual(payload["dropped_count"], 1)
        self.assertEqual(payload["first_dropped_sequence"], 7)
        self.assertEqual(payload["last_dropped_sequence"], 7)
        self.assertEqual(payload["observer_epoch"], 1)
        self.assertEqual(self.engine.external_fill_handoff_metrics()["buffer_depth"], 0)
        self.assertEqual(
            self.engine.external_fill_handoff_metrics()["cumulative_dropped"],
            1,
        )

    async def test_schedule_failure_pending_is_audited_on_epoch_reset(self):
        class _FailingScheduleLoop:
            def is_closed(self) -> bool:
                return False

            def call_soon_threadsafe(self, _callback) -> None:  # type: ignore[no-untyped-def]
                raise RuntimeError("loop closed during schedule")

        self.engine._external_fill_loop = _FailingScheduleLoop()  # type: ignore[assignment]
        self.engine._external_fill_observer_lifecycle = "running"

        with self.assertLogs("k2bi.engine", level="WARNING") as logs:
            self.engine._enqueue_external_fill_observation(
                _observation(exec_id="schedule-failed")
            )

        self.assertTrue(
            any("drain scheduling failed" in line for line in logs.output)
        )
        self.assertEqual(self._events("external_fill_observed"), [])
        self.assertEqual(self._events("external_fill_handoff_dropped"), [])
        self.assertEqual(self.engine.external_fill_handoff_metrics()["buffer_depth"], 1)

        self.engine._reset_external_fill_handoff_epoch()

        self.assertEqual(self._events("external_fill_observed"), [])
        dropped = self._events("external_fill_handoff_dropped")
        self.assertEqual(len(dropped), 1)
        payload = dropped[0]["payload"]
        self.assertEqual(payload["drop_reason"], "drain_schedule_failed")
        self.assertEqual(payload["dropped_count"], 1)
        self.assertEqual(payload["first_dropped_sequence"], 1)
        self.assertEqual(payload["last_dropped_sequence"], 1)
        self.assertEqual(payload["observer_epoch"], 1)
        self.assertEqual(self.engine.external_fill_handoff_metrics()["buffer_depth"], 0)
        self.assertEqual(
            self.engine.external_fill_handoff_metrics()["cumulative_dropped"],
            1,
        )

    async def test_handoff_lock_contention_does_not_block_broker_callback(self):
        self.engine._external_fill_handoff_lock.acquire()
        callback_thread = threading.Thread(
            target=lambda: self.connector.emit_external_fill_observed(
                _observation(exec_id="lock-contention")
            ),
            name="ibkr-fill-callback",
        )
        try:
            callback_thread.start()
            callback_thread.join(timeout=0.1)
            blocked = callback_thread.is_alive()
        finally:
            self.engine._external_fill_handoff_lock.release()
            callback_thread.join(timeout=1)

        self.assertFalse(blocked)
        await asyncio.sleep(0)
        self.assertEqual(self._events("external_fill_observed"), [])
        self.assertEqual(
            self.engine.external_fill_handoff_metrics()["cumulative_dropped"],
            1,
        )


class _RecordingLock:
    def __init__(self) -> None:
        self.entered = 0
        self.exited = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.exited += 1
        return False


class PositionCacheSingleWriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.engine = Engine(
            connector=MockIBKRConnector(),
            journal=JournalWriter(base_dir=self.base / "journal", git_sha="p4-1"),
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    async def test_position_snapshot_write_helper_updates_cache_metadata_under_lock(
        self,
    ) -> None:
        old_position = BrokerPosition(ticker="G", qty=71, avg_price=Decimal("30.32"))
        new_position = BrokerPosition(ticker="SPY", qty=2, avg_price=Decimal("707.72"))
        fetched_at = datetime(2026, 5, 28, 8, 0, tzinfo=timezone.utc)
        self.engine._positions = [old_position]
        lock = _RecordingLock()
        self.engine._positions_lock = lock

        await self.engine._write_position_snapshot(
            PositionSnapshot(
                positions=[new_position],
                valid=True,
                source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
                fetched_at=fetched_at,
            )
        )

        self.assertEqual(lock.entered, 1)
        self.assertEqual(lock.exited, 1)
        self.assertEqual(self.engine._positions_prev, [old_position])
        self.assertEqual(self.engine._positions, [new_position])
        self.assertEqual(self.engine._position_source, POSITION_SOURCE_LIVE_REQ_POSITIONS)
        self.assertTrue(self.engine._position_visibility_valid)
        self.assertEqual(self.engine._positions_refreshed_at, fetched_at)

    async def test_real_asyncio_lock_blocks_position_snapshot_write(self) -> None:
        self.assertIsInstance(self.engine._positions_lock, asyncio.Lock)
        old_position = BrokerPosition(ticker="G", qty=71, avg_price=Decimal("30.32"))
        new_position = BrokerPosition(ticker="SPY", qty=2, avg_price=Decimal("707.72"))
        fetched_at = datetime(2026, 5, 28, 8, 30, tzinfo=timezone.utc)
        self.engine._positions = [old_position]

        await self.engine._positions_lock.acquire()
        task = asyncio.create_task(
            self.engine._write_position_snapshot(
                PositionSnapshot(
                    positions=[new_position],
                    valid=True,
                    source=POSITION_SOURCE_LIVE_REQ_POSITIONS,
                    fetched_at=fetched_at,
                )
            )
        )
        await asyncio.sleep(0)

        self.assertFalse(task.done())
        self.assertEqual(self.engine._positions, [old_position])

        self.engine._positions_lock.release()
        await task

        self.assertEqual(self.engine._positions_prev, [old_position])
        self.assertEqual(self.engine._positions, [new_position])
        self.assertEqual(self.engine._positions_refreshed_at, fetched_at)


class _FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, trade, fill) -> None:
        for handler in list(self.handlers):
            handler(trade, fill)


class _FakeStock:
    def __init__(self, symbol: str, exchange: str, currency: str) -> None:
        self.symbol = symbol
        self.exchange = exchange
        self.currency = currency


class _FakeBaseOrder:
    def __init__(self, action: str, qty: int, *, tif: str) -> None:
        self.action = action
        self.totalQuantity = qty
        self.tif = tif
        self.transmit = True
        self.parentId = 0
        self.orderRef = ""
        self.orderId = 0
        self.permId = 0
        self.lmtPrice = 0.0
        self.auxPrice = 0.0


class _FakeLimitOrder(_FakeBaseOrder):
    def __init__(self, action: str, qty: int, limit_price: float, *, tif: str) -> None:
        super().__init__(action, qty, tif=tif)
        self.orderType = "LMT"
        self.lmtPrice = limit_price


class _FakeMarketOrder(_FakeBaseOrder):
    def __init__(self, action: str, qty: int, *, tif: str) -> None:
        super().__init__(action, qty, tif=tif)
        self.orderType = "MKT"


class _FakeStopOrder(_FakeBaseOrder):
    def __init__(self, action: str, qty: int, stop_price: float, *, tif: str) -> None:
        super().__init__(action, qty, tif=tif)
        self.orderType = "STP"
        self.auxPrice = stop_price


class _FakeOrderStatus:
    status = "Submitted"
    whyHeld = ""


class _FakeTrade:
    def __init__(self, contract: _FakeStock, order: _FakeBaseOrder) -> None:
        self.contract = contract
        self.order = order
        self.orderStatus = _FakeOrderStatus()
        self.fillEvent = _FakeEvent()


class _FakeTradeWithoutFillEvent:
    def __init__(self, contract: _FakeStock, order: _FakeBaseOrder) -> None:
        self.contract = contract
        self.order = order
        self.orderStatus = _FakeOrderStatus()


class _FakeIB:
    def __init__(self) -> None:
        self.trades: list[_FakeTrade] = []
        self._next_order_id = 95
        self._next_perm_id = 1677427048

    def placeOrder(self, contract: _FakeStock, order: _FakeBaseOrder) -> _FakeTrade:
        order.orderId = self._next_order_id
        order.permId = self._next_perm_id
        self._next_order_id += 1
        self._next_perm_id += 1
        trade = _FakeTrade(contract, order)
        self.trades.append(trade)
        return trade


class _FakeConnectableIB(_FakeIB):
    def __init__(self) -> None:
        super().__init__()
        self.disconnected = False

    async def connectAsync(self, **_kwargs) -> None:
        return None

    async def reqAccountSummaryAsync(self) -> list:
        return []

    def disconnect(self) -> None:
        self.disconnected = True


class _DelayedFillIB(_FakeIB):
    def __init__(self) -> None:
        super().__init__()
        self._pending_trade: _FakeTrade | None = None
        self._pending_order_id = self._next_order_id
        self._pending_perm_id = self._next_perm_id
        self._emitted = False

    def placeOrder(self, contract: _FakeStock, order: _FakeBaseOrder) -> _FakeTrade:
        order.orderId = 0
        order.permId = 0
        self._next_order_id += 1
        self._next_perm_id += 1
        trade = _FakeTrade(contract, order)
        self.trades.append(trade)
        self._pending_trade = trade
        return trade

    async def sleep_and_emit(self, _: float) -> None:
        if self._pending_trade is None or self._emitted:
            return
        self._emitted = True
        trade = self._pending_trade
        trade.order.orderId = self._pending_order_id
        trade.order.permId = self._pending_perm_id
        trade.fillEvent.emit(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="BOT",
                    shares=3,
                    price="30.32",
                    orderId=self._pending_order_id,
                    permId=self._pending_perm_id,
                    execId="parent-immediate",
                    clientId=1,
                    time=datetime(2026, 5, 28, 14, 0, tzinfo=timezone.utc),
                )
            ),
        )


class _StandaloneStopFillDuringOrderIdTimeoutIB(_FakeIB):
    def __init__(self) -> None:
        super().__init__()
        self._pending_trade: _FakeTrade | None = None
        self._emitted = False
        self.cancelled_orders: list[_FakeBaseOrder] = []

    def placeOrder(self, contract: _FakeStock, order: _FakeBaseOrder) -> _FakeTrade:
        order.orderId = 0
        order.permId = 0
        trade = _FakeTrade(contract, order)
        self.trades.append(trade)
        self._pending_trade = trade
        return trade

    def cancelOrder(self, order: _FakeBaseOrder) -> None:
        self.cancelled_orders.append(order)

    async def sleep_and_fill(self, _: float) -> None:
        if self._pending_trade is None or self._emitted:
            return
        self._emitted = True
        trade = self._pending_trade
        trade.orderStatus.status = "Filled"
        trade.fillEvent.emit(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="standalone-timeout-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 14, 45, tzinfo=timezone.utc),
                )
            ),
        )


def _fake_ib_async_module() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        Stock=_FakeStock,
        LimitOrder=_FakeLimitOrder,
        MarketOrder=_FakeMarketOrder,
        StopOrder=_FakeStopOrder,
    )


class IBKRFillEventObservationTests(unittest.IsolatedAsyncioTestCase):
    def test_live_observer_registration_rejects_invalid_callback(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )

        with self.assertRaises(TypeError):
            connector.set_external_fill_observer("not-callable")

        async def async_observer(_observation):
            return None

        with self.assertRaises(TypeError):
            connector.set_external_fill_observer(async_observer)

        def zero_arg_observer():
            return None

        with self.assertRaises(TypeError):
            connector.set_external_fill_observer(zero_arg_observer)

    async def test_submit_order_observes_parent_fill_during_broker_wait(self):
        fake_ib = _DelayedFillIB()
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector._connected = True
        seen = []
        connector.set_external_fill_observer(seen.append)

        with patch.dict(sys.modules, {"ib_async": _fake_ib_async_module()}):
            with patch("execution.connectors.ibkr.asyncio.sleep", fake_ib.sleep_and_emit):
                await connector.submit_order(
                    ticker="G",
                    side="buy",
                    qty=3,
                    limit_price=None,
                    stop_loss=None,
                    time_in_force="DAY",
                    client_tag="k2bi:g-2026-05_2nd-wave-paper-trade:T-parent",
                    order_type="MKT",
                )

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].exec_id, "parent-immediate")
        self.assertEqual(seen[0].side, "buy")
        self.assertEqual(seen[0].client_tag, "k2bi:g-2026-05_2nd-wave-paper-trade:T-parent")
        source = inspect.getsource(IBKRConnector.submit_order)
        self.assertLess(
            source.index("_attach_external_fill_observer(parent_trade)"),
            source.index("await asyncio.sleep"),
        )

    async def test_submit_order_fill_event_emits_broker_neutral_observation(self):
        fake_ib = _FakeIB()
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector._connected = True
        seen = []
        connector.set_external_fill_observer(seen.append)

        with patch.dict(sys.modules, {"ib_async": _fake_ib_async_module()}):
            await connector.submit_order(
                ticker="G",
                side="buy",
                qty=71,
                limit_price=Decimal("30.32"),
                stop_loss=Decimal("29.93"),
                time_in_force="DAY",
                client_tag="k2bi:g-2026-05_2nd-wave-paper-trade:T1",
                order_type="LMT",
            )

        stop_trade = fake_ib.trades[1]
        stop_trade.fillEvent.emit(
            stop_trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=96,
                    permId=1677427049,
                    execId="0001.abc",
                    clientId=1,
                    time=datetime(2026, 5, 13, 14, 26, 23, tzinfo=timezone.utc),
                )
            ),
        )

        self.assertEqual(len(seen), 1)
        observation = seen[0]
        self.assertEqual(observation.ticker, "G")
        self.assertEqual(observation.side, "sell")
        self.assertEqual(observation.qty, 71)
        self.assertEqual(observation.price, Decimal("29.93"))
        self.assertEqual(observation.broker_order_id, "96")
        self.assertEqual(observation.broker_perm_id, "1677427049")
        self.assertEqual(observation.exec_id, "0001.abc")
        self.assertEqual(
            observation.client_tag,
            "k2bi:g-2026-05_2nd-wave-paper-trade:T1:stop",
        )
        self.assertEqual(observation.source, "trade_fill_event")

    async def test_standalone_stop_fill_event_emits_broker_neutral_observation(self):
        fake_ib = _FakeIB()
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector._connected = True
        seen = []
        connector.set_external_fill_observer(seen.append)

        with patch.dict(sys.modules, {"ib_async": _fake_ib_async_module()}):
            await connector.submit_standalone_stop_order(
                ticker="G",
                side="sell",
                qty=71,
                stop_price=Decimal("29.93"),
                time_in_force="GTC",
                client_tag="k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop",
            )

        stop_trade = fake_ib.trades[0]
        stop_trade.fillEvent.emit(
            stop_trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="standalone-stop-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 14, 30, tzinfo=timezone.utc),
                )
            ),
        )

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].exec_id, "standalone-stop-fill")
        self.assertEqual(
            seen[0].client_tag,
            "k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop",
        )

    async def test_standalone_stop_fill_during_orderid_timeout_returns_filled_ack(self):
        fake_ib = _StandaloneStopFillDuringOrderIdTimeoutIB()
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector._connected = True
        seen = []
        connector.set_external_fill_observer(seen.append)

        with patch.dict(sys.modules, {"ib_async": _fake_ib_async_module()}):
            with patch(
                "execution.connectors.ibkr.asyncio.sleep",
                fake_ib.sleep_and_fill,
            ):
                ack = await connector.submit_standalone_stop_order(
                    ticker="G",
                    side="sell",
                    qty=71,
                    stop_price=Decimal("29.93"),
                    time_in_force="GTC",
                    client_tag="k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop",
                )

        self.assertEqual(ack.status, "Filled")
        self.assertEqual(ack.broker_order_id, "95")
        self.assertEqual(ack.broker_perm_id, "1677427048")
        self.assertEqual(fake_ib.cancelled_orders, [])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].exec_id, "standalone-timeout-fill")
        self.assertLess(ack.submitted_at, seen[0].observed_at)

    def test_missing_fill_event_emits_unavailable_observation_for_audit(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        seen = []
        connector.set_external_fill_observer(seen.append)
        order = _FakeStopOrder("SELL", 71, 29.93, tif="GTC")
        order.orderId = 95
        order.permId = 1677427048
        order.orderRef = "k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop"
        trade = _FakeTradeWithoutFillEvent(_FakeStock("G", "SMART", "USD"), order)

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING"):
            connector._attach_external_fill_observer(trade)

        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].source, "fill_event_unavailable")
        self.assertEqual(seen[0].qty, 0)
        self.assertEqual(
            seen[0].client_tag,
                "k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop",
            )

    async def test_missing_fill_event_observation_reaches_engine_unavailable_audit(self):
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            journal = JournalWriter(base_dir=base / "journal", git_sha="p4-1")
            Engine(
                connector=connector,
                journal=journal,
                validator_config=CONFIG,
                engine_config=EngineConfig(strategies_dir=base / "strategies"),
            )
            order = _FakeStopOrder("SELL", 71, 29.93, tif="GTC")
            order.orderId = 95
            order.permId = 1677427048
            order.orderRef = "k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop"
            trade = _FakeTradeWithoutFillEvent(
                _FakeStock("G", "SMART", "USD"),
                order,
            )

            with self.assertLogs("k2bi.connector.ibkr", level="WARNING"):
                connector._attach_external_fill_observer(trade)

            await _wait_for_event(journal, "external_fill_event_unavailable")
            events = journal.read_all()

        observed = [e for e in events if e["event_type"] == "external_fill_observed"]
        malformed = [e for e in events if e["event_type"] == "external_fill_malformed"]
        unavailable = [
            e for e in events if e["event_type"] == "external_fill_event_unavailable"
        ]
        self.assertEqual(observed, [])
        self.assertEqual(malformed, [])
        self.assertEqual(len(unavailable), 1)
        payload = unavailable[0]["payload"]
        self.assertEqual(payload["source"], "fill_event_unavailable")
        self.assertEqual(payload["unavailable_reason"], "trade_fill_event_missing")
        self.assertNotIn("qty", payload)
        self.assertNotIn("price", payload)
        self.assertNotIn("exec_id", payload)
        self.assertEqual(
            payload["client_tag"],
            "k2bi:g-2026-05_2nd-wave-paper-trade:repair:stop",
        )

    async def test_fill_observation_cache_is_consumed_by_filled_ack(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.orderStatus.status = "Filled"
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector.set_external_fill_observer(lambda _observation: None)
        connector._on_trade_fill_event(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="standalone-cache-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc),
                )
            ),
        )

        cache_key = connector._fill_observation_cache_key(trade)
        self.assertIn(cache_key, connector._fill_observations_by_trade_id)
        ack = connector._filled_ack_if_trade_filled(
            trade,
            submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(ack)
        self.assertNotIn(cache_key, connector._fill_observations_by_trade_id)

    async def test_fill_observation_cache_epoch_mismatch_returns_no_safe_ack(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.orderStatus.status = "Filled"
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector.set_external_fill_observer(lambda _observation: None)
        connector._on_trade_fill_event(trade, _fill())
        stale_key = connector._fill_observation_cache_key(trade)
        connector._fill_observation_cache_epoch += 1

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING") as logs:
            ack = connector._filled_ack_if_trade_filled(
                trade,
                submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
            )

        self.assertIsNone(ack)
        self.assertNotIn(stale_key, connector._fill_observations_by_trade_id)
        self.assertTrue(any("cache epoch mismatch" in line for line in logs.output))

    async def test_stale_fill_observation_cache_mismatch_returns_no_safe_ack(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.orderStatus.status = "Filled"
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector.set_external_fill_observer(lambda _observation: None)
        connector._on_trade_fill_event(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=999,
                    permId=999999,
                    execId="stale-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc),
                )
            ),
        )

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING"):
            ack = connector._filled_ack_if_trade_filled(
                trade,
                submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
            )

        self.assertIsNone(ack)
        self.assertNotIn(
            connector._fill_observation_cache_key(trade),
            connector._fill_observations_by_trade_id,
        )

    async def test_missing_fill_observation_cache_returns_no_safe_ack(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.orderStatus.status = "Filled"
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )

        ack = connector._filled_ack_if_trade_filled(
            trade,
            submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
        )

        self.assertIsNone(ack)

    async def test_fill_observation_cache_clears_on_connect_and_disconnect(self):
        fake_ib = _FakeConnectableIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector._ib = fake_ib
        connector.set_external_fill_observer(lambda _observation: None)
        connector._on_trade_fill_event(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="stale-before-connect",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc),
                )
            ),
        )
        self.assertIn(
            connector._fill_observation_cache_key(trade),
            connector._fill_observations_by_trade_id,
        )

        with patch.dict(
            sys.modules,
            {"ib_async": types.SimpleNamespace(IB=lambda: fake_ib)},
        ):
            await connector.connect()

        self.assertEqual(connector._fill_observations_by_trade_id, {})

        connector._on_trade_fill_event(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="stale-before-disconnect",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 1, tzinfo=timezone.utc),
                )
            ),
        )
        self.assertIn(
            connector._fill_observation_cache_key(trade),
            connector._fill_observations_by_trade_id,
        )

        await connector.disconnect()

        self.assertEqual(connector._fill_observations_by_trade_id, {})

    async def test_stale_fill_observation_same_broker_ids_rejects_ticker_mismatch(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.orderStatus.status = "Filled"
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector.set_external_fill_observer(lambda _observation: None)
        connector._on_trade_fill_event(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="stale-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc),
                )
            ),
        )
        trade.contract.symbol = "SPY"

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING") as logs:
            ack = connector._filled_ack_if_trade_filled(
                trade,
                submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
            )

        self.assertIsNone(ack)
        self.assertTrue(any("ticker mismatch" in line for line in logs.output))

    async def test_stale_fill_observation_same_broker_ids_rejects_exec_id_mismatch(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        trade.orderStatus.status = "Filled"
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        connector.set_external_fill_observer(lambda _observation: None)
        connector._on_trade_fill_event(
            trade,
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="stale-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 0, tzinfo=timezone.utc),
                )
            ),
        )
        trade.fills = [
            types.SimpleNamespace(
                execution=types.SimpleNamespace(
                    side="SLD",
                    shares=71,
                    price="29.93",
                    orderId=95,
                    permId=1677427048,
                    execId="fresh-fill",
                    clientId=1,
                    time=datetime(2026, 5, 28, 15, 1, tzinfo=timezone.utc),
                )
            )
        ]

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING") as logs:
            ack = connector._filled_ack_if_trade_filled(
                trade,
                submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
            )

        self.assertIsNone(ack)
        self.assertTrue(any("exec_id mismatch" in line for line in logs.output))

    def test_fill_observation_started_before_cache_clear_is_discarded(self):
        fake_ib = _FakeIB()
        trade = fake_ib.placeOrder(
            _FakeStock("G", "SMART", "USD"),
            _FakeStopOrder("SELL", 71, 29.93, tif="GTC"),
        )
        connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        seen = []
        connector.set_external_fill_observer(seen.append)
        real_build = connector._build_fill_observation

        def build_after_clear(trade_arg, fill_arg):  # type: ignore[no-untyped-def]
            observation = real_build(trade_arg, fill_arg)
            connector._clear_fill_observation_cache()
            return observation

        connector._build_fill_observation = build_after_clear  # type: ignore[method-assign]

        with self.assertLogs("k2bi.connector.ibkr", level="WARNING") as logs:
            connector._on_trade_fill_event(
                trade,
                types.SimpleNamespace(
                    execution=types.SimpleNamespace(
                        side="SLD",
                        shares=71,
                        price="29.93",
                        orderId=95,
                        permId=1677427048,
                        execId="race-fill",
                        clientId=1,
                        time=datetime(2026, 5, 28, 15, 2, tzinfo=timezone.utc),
                    )
                ),
            )

        self.assertEqual(seen, [])
        self.assertNotIn(
            connector._fill_observation_cache_key(trade),
            connector._fill_observations_by_trade_id,
        )
        self.assertTrue(any("cache epoch changed" in line for line in logs.output))


class CacheHandoffAlignmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal = JournalWriter(base_dir=self.base / "journal", git_sha="p4-1")
        self.connector = IBKRConnector(
            account_id=None,
            host="127.0.0.1",
            port=4002,
            client_id=1,
        )
        self.engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(strategies_dir=self.base / "strategies"),
        )

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _trade(self) -> _FakeTrade:
        order = _FakeStopOrder("SELL", 71, 29.93, tif="GTC")
        order.orderId = 96
        order.permId = 1677427049
        order.orderRef = "k2bi:g-2026-05_2nd-wave-paper-trade:T1:stop"
        trade = _FakeTrade(_FakeStock("G", "SMART", "USD"), order)
        trade.orderStatus.status = "Filled"
        return trade

    async def test_ack_cache_consumption_does_not_remove_pending_handoff_entry(self):
        trade = self._trade()
        callback_thread = threading.Thread(
            target=lambda: self.connector._on_trade_fill_event(trade, _fill()),
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)
        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(self.journal.read_all(), [])

        ack = self.connector._filled_ack_if_trade_filled(
            trade,
            submitted_at=datetime(2026, 5, 28, 14, 59, tzinfo=timezone.utc),
        )

        self.assertIsNotNone(ack)
        self.assertEqual(ack.broker_order_id, "96")
        self.assertEqual(ack.broker_perm_id, "1677427049")
        self.assertNotIn(
            self.connector._fill_observation_cache_key(trade),
            self.connector._fill_observations_by_trade_id,
        )
        observed = await _wait_for_event(self.journal, "external_fill_observed")
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0]["payload"]["exec_id"], "0001.abc")

    async def test_connector_snapshots_payload_before_mutable_trade_changes(self):
        trade = self._trade()
        fill = _fill(exec_id="snapshot-original")
        callback_thread = threading.Thread(
            target=lambda: self.connector._on_trade_fill_event(trade, fill),
            name="ibkr-fill-callback",
        )
        callback_thread.start()
        callback_thread.join(timeout=1)
        self.assertFalse(callback_thread.is_alive())
        self.assertEqual(self.journal.read_all(), [])

        trade.contract.symbol = "SPY"
        fill.execution.execId = "snapshot-mutated"

        observed = await _wait_for_event(self.journal, "external_fill_observed")

        payload = observed[0]["payload"]
        self.assertEqual(payload["ticker"], "G")
        self.assertEqual(payload["exec_id"], "snapshot-original")
