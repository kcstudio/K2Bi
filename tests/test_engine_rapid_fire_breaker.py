"""Spec B §3 tests for the rapid-fire circuit breaker."""

from __future__ import annotations

import tempfile
import unittest
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

        self._orig_main_dt = None
        self._orig_mock_dt = None

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        if self._orig_main_dt is None:
            self._orig_main_dt = main_mod.datetime
        if self._orig_mock_dt is None:
            self._orig_mock_dt = mock_mod.datetime
        main_mod.datetime = _PatchedDT
        mock_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        if self._orig_main_dt is None:
            return
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod

        main_mod.datetime = self._orig_main_dt
        mock_mod.datetime = self._orig_mock_dt
        self._orig_main_dt = None
        self._orig_mock_dt = None

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

        self.engine._pending_order = None
        self.engine.state = EngineState.CONNECTED_IDLE
        await self._patch_now(base + timedelta(seconds=4))

        skipped_tick = await self.engine.tick_once()

        self.assertEqual(skipped_tick.orders_submitted, 0)
        skips = self._events("cycle_skipped_rapid_fire_halt")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["strategy_id"], "g-2026-05")
        self.assertEqual(skips[0]["payload"]["symbol"], "G")
