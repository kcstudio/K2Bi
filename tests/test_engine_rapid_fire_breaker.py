"""Spec B §3 tests for the rapid-fire circuit breaker."""

from __future__ import annotations

import tempfile
import unittest
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig, EngineState
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
    "rapid_fire_circuit_breaker": {
        "max_orders_per_window": 3,
        "window_seconds": 60,
    },
}


def _mid_session_utc() -> datetime:
    return datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_strategy(dir: Path, *, name: str = "g-2026-05") -> Path:
    text = (
        "---\n"
        f"name: {name}\n"
        "status: approved\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.01\n"
        "approved_at: 2026-05-01T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "order:\n"
        "  ticker: G\n"
        "  side: buy\n"
        "  qty: 71\n"
        "  limit_price: 32.00\n"
        "  stop_loss: 30.00\n"
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nPlain-English block.\n"
    )
    path = dir / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class RapidFireBreakerTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = self.base / ".killed"
        _write_strategy(self.strategies_dir)

        self.connector = MockIBKRConnector()
        self.connector.marks = {"G": Decimal("32.00")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test03")
        self.engine = self._make_engine()

        self._orig_main_dt = None
        self._orig_mock_dt = None
        self._orig_writer_dt = None

    def _make_engine(self) -> Engine:
        return Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod
        import execution.journal.writer as writer_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        if self._orig_main_dt is None:
            self._orig_main_dt = main_mod.datetime
        if self._orig_mock_dt is None:
            self._orig_mock_dt = mock_mod.datetime
        if self._orig_writer_dt is None:
            self._orig_writer_dt = writer_mod.datetime
        main_mod.datetime = _PatchedDT
        mock_mod.datetime = _PatchedDT
        writer_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        if self._orig_main_dt is None:
            return
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod
        import execution.journal.writer as writer_mod

        main_mod.datetime = self._orig_main_dt
        mock_mod.datetime = self._orig_mock_dt
        writer_mod.datetime = self._orig_writer_dt
        self._orig_main_dt = None
        self._orig_mock_dt = None
        self._orig_writer_dt = None

    async def _init_engine(self, now: datetime) -> None:
        await self._patch_now(now)
        tick = await self.engine.tick_once()
        self.assertEqual(tick.state_after, EngineState.CONNECTED_IDLE)

    def _events(self, event_type: str) -> list[dict]:
        return [
            record
            for record in self.journal.read_all(_mid_session_utc())
            if record["event_type"] == event_type
        ]

    def _append_completed_submission(self, *, order_id: str, ts: datetime) -> None:
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "order_type": "LMT",
                "limit_price": "32.00",
                "stop_loss": "30.00",
                "time_in_force": "DAY",
            },
            strategy="g-2026-05",
            trade_id=f"T-{order_id}",
            ticker="G",
            side="buy",
            qty=71,
            broker_order_id=order_id,
            broker_perm_id=f"900{order_id}",
            ts=ts,
        )
        self.journal.append(
            "order_terminal",
            payload={
                "broker_order_id": order_id,
                "terminal_status": "Filled",
            },
            strategy="g-2026-05",
            trade_id=f"T-{order_id}",
            ticker="G",
            side="buy",
            qty=71,
            broker_order_id=order_id,
            broker_perm_id=f"900{order_id}",
            ts=ts + timedelta(milliseconds=100),
        )

    def _append_trip(
        self,
        *,
        trip_id: int = 1,
        strategy_id: str = "g-2026-05",
        symbol: str = "G",
        ts: datetime | None = None,
    ) -> None:
        when = ts or _mid_session_utc()
        self.journal.append(
            "circuit_breaker_tripped_rapid_fire",
            payload={
                "strategy_id": strategy_id,
                "symbol": symbol,
                "trip_id": trip_id,
                "cycle_id": f"T-trip-{trip_id}",
                "submission_timestamps": [
                    (when - timedelta(seconds=3)).isoformat(),
                    (when - timedelta(seconds=2)).isoformat(),
                    (when - timedelta(seconds=1)).isoformat(),
                    when.isoformat(),
                ],
                "max_orders_per_window": 3,
                "window_seconds": 60,
            },
            strategy=strategy_id,
            trade_id=f"T-trip-{trip_id}",
            ticker=symbol,
            ts=when,
        )

    def _append_clear(
        self,
        *,
        trip_id: int = 1,
        clear_nonce: str = "nonce-A",
        strategy_id: str = "g-2026-05",
        symbol: str = "G",
        ts: datetime | None = None,
    ) -> None:
        self.journal.append(
            "circuit_breaker_cleared",
            payload={
                "trip_id": trip_id,
                "clear_nonce": clear_nonce,
                "operator_ts": (ts or _mid_session_utc()).isoformat(),
                "keys": [{"strategy_id": strategy_id, "symbol": symbol}],
            },
            ts=ts or _mid_session_utc(),
        )

    def _write_sentinel(
        self,
        *,
        trip_id: int = 1,
        clear_nonce: str = "nonce-A",
        strategy_id: str = "g-2026-05",
        symbol: str = "G",
    ) -> Path:
        path = self.kill_path.parent / ".rapid-fire-cleared.json"
        path.write_text(
            json.dumps(
                {
                    "trip_id": trip_id,
                    "clear_nonce": clear_nonce,
                    "operator_ts": _mid_session_utc().isoformat(),
                    "keys": [{"strategy_id": strategy_id, "symbol": symbol}],
                }
            ),
            encoding="utf-8",
        )
        return path

    async def test_r1_four_orders_in_ten_seconds_trips_and_skips(self) -> None:
        base = _mid_session_utc()
        await self._init_engine(base + timedelta(seconds=3))
        for offset, order_id in enumerate(("101", "102", "103")):
            self._append_completed_submission(
                order_id=order_id,
                ts=base + timedelta(seconds=offset),
            )

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        trips = self._events("circuit_breaker_tripped_rapid_fire")
        self.assertEqual(len(trips), 1)
        self.assertEqual(trips[0]["payload"]["strategy_id"], "g-2026-05")
        self.assertEqual(trips[0]["payload"]["symbol"], "G")
        self.assertEqual(len(trips[0]["payload"]["submission_timestamps"]), 4)

        pending = self.engine._pending_order
        self.assertIsNotNone(pending)
        self.engine._journal_order_terminal(pending, terminal_status="Filled")
        self.engine._pending_order = None
        self.engine.state = EngineState.CONNECTED_IDLE
        await self._patch_now(base + timedelta(seconds=4))

        skipped_tick = await self.engine.tick_once()

        self.assertEqual(skipped_tick.orders_submitted, 0)
        skips = self._events("cycle_skipped_rapid_fire_halt")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["strategy_id"], "g-2026-05")
        self.assertEqual(skips[0]["payload"]["symbol"], "G")

    async def test_r2_three_orders_in_sixty_seconds_does_not_trip(self) -> None:
        base = _mid_session_utc()
        await self._init_engine(base + timedelta(seconds=2))
        for offset, order_id in enumerate(("201", "202")):
            self._append_completed_submission(
                order_id=order_id,
                ts=base + timedelta(seconds=offset),
            )

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(self._events("circuit_breaker_tripped_rapid_fire"), [])

    async def test_r3_four_orders_spread_over_ninety_seconds_do_not_trip(self) -> None:
        base = _mid_session_utc()
        await self._init_engine(base + timedelta(seconds=90))
        for offset, order_id in ((0, "301"), (30, "302"), (61, "303")):
            self._append_completed_submission(
                order_id=order_id,
                ts=base + timedelta(seconds=offset),
            )

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(self._events("circuit_breaker_tripped_rapid_fire"), [])

    async def test_r4_cross_strategy_halt_does_not_block_other_strategy(
        self,
    ) -> None:
        _write_strategy(self.strategies_dir, name="g-alt")
        base = _mid_session_utc()
        await self._init_engine(base + timedelta(seconds=3))
        for offset, order_id in enumerate(("401", "402", "403")):
            self._append_completed_submission(
                order_id=order_id,
                ts=base + timedelta(seconds=offset),
            )
        await self.engine.tick_once()
        pending = self.engine._pending_order
        self.assertIsNotNone(pending)
        self.engine._journal_order_terminal(pending, terminal_status="Filled")
        self.engine._pending_order = None
        self.engine.state = EngineState.CONNECTED_IDLE
        await self._patch_now(base + timedelta(seconds=4))

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertIn(":g-alt:", self.connector.submitted_orders[-1].client_tag)
        skips = self._events("cycle_skipped_rapid_fire_halt")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["strategy_id"], "g-2026-05")

    async def test_r5_valid_rearm_sentinel_clears_halt_after_journal(self) -> None:
        base = _mid_session_utc()
        self._append_trip(ts=base)
        await self._init_engine(base)
        sentinel = self._write_sentinel()
        deleted_after_clear = []

        def capture_delete(path: Path) -> None:
            deleted_after_clear.append(bool(self._events("circuit_breaker_cleared")))
            path.unlink()

        self.engine._delete_rapid_fire_clear_sentinel = capture_delete

        await self.engine.tick_once()

        self.assertEqual(deleted_after_clear, [True])
        self.assertFalse(sentinel.exists())
        self.assertEqual(self._events("circuit_breaker_cleared")[0]["payload"]["trip_id"], 1)
        self.assertEqual(self._events("cycle_skipped_rapid_fire_halt"), [])

    async def test_r6_malformed_sentinel_is_rejected(self) -> None:
        base = _mid_session_utc()
        self._append_trip(ts=base)
        await self._init_engine(base)
        sentinel = self.kill_path.parent / ".rapid-fire-cleared.json"
        sentinel.write_text("{not-json", encoding="utf-8")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertTrue(sentinel.exists())
        malformed = self._events("circuit_breaker_cleared_malformed_sentinel")
        self.assertEqual(len(malformed), 1)
        self.assertTrue(malformed[0]["payload"]["telegram_alert_required"])
        self.assertEqual(len(self._events("cycle_skipped_rapid_fire_halt")), 1)

    async def test_r7_halt_persists_across_engine_restart(self) -> None:
        base = _mid_session_utc()
        self._append_trip(ts=base)
        self.engine = self._make_engine()
        await self._init_engine(base + timedelta(seconds=1))

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        skips = self._events("cycle_skipped_rapid_fire_halt")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["trip_id"], 1)

    async def test_r8_leftover_consumed_sentinel_is_ignored_and_deleted(
        self,
    ) -> None:
        base = _mid_session_utc()
        self._append_trip(ts=base)
        self._append_clear(ts=base + timedelta(seconds=1))
        sentinel = self._write_sentinel()
        self.engine = self._make_engine()
        await self._init_engine(base + timedelta(seconds=2))

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertFalse(sentinel.exists())
        ignored = self._events("circuit_breaker_cleared_stale_sentinel_ignored")
        self.assertEqual(len(ignored), 1)
        self.assertEqual(self._events("cycle_skipped_rapid_fire_halt"), [])

    async def test_r9_consumed_nonce_cannot_clear_new_trip(self) -> None:
        base = _mid_session_utc()
        self._append_trip(trip_id=1, ts=base)
        self._append_clear(trip_id=1, clear_nonce="nonce-A", ts=base + timedelta(seconds=1))
        self._append_trip(trip_id=2, ts=base + timedelta(seconds=2))
        self._write_sentinel(trip_id=1, clear_nonce="nonce-A")
        self.engine = self._make_engine()
        await self._init_engine(base + timedelta(seconds=3))

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        rejected = self._events("circuit_breaker_cleared_stale_sentinel_rejected")
        self.assertEqual(len(rejected), 1)
        self.assertTrue(rejected[0]["payload"]["telegram_alert_required"])
        skips = self._events("cycle_skipped_rapid_fire_halt")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["trip_id"], 2)
