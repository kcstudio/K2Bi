"""Spec B §9.2 tests for stopped-out strategy lifecycle."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.engine import main as engine_main
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
}


def _mid_session_utc() -> datetime:
    return datetime(2026, 5, 15, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_strategy(dir_path: Path) -> Path:
    path = dir_path / "strategy_g-2026-05_2nd-wave-paper-trade.md"
    path.write_text(
        "---\n"
        "name: g-2026-05_2nd-wave-paper-trade\n"
        "status: approved\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.0025\n"
        "approved_at: 2026-05-01T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "order:\n"
        "  ticker: G\n"
        "  side: buy\n"
        "  qty: 71\n"
        "  limit_price: 34.50\n"
        "  stop_loss: 30.00\n"
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nPlain-English block.\n",
        encoding="utf-8",
    )
    return path


class ActiveProtectiveStopStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal_dir = self.base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = self.base / "strategies"
        self.strategies_dir.mkdir()
        self.strategy_path = _write_strategy(self.strategies_dir)
        self.kill_path = self.base / ".killed"
        self.connector = MockIBKRConnector()
        self.connector.marks = {"G": Decimal("34.50")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test92")

    async def asyncTearDown(self) -> None:
        await self._unpatch_now()
        self._tmp.cleanup()

    async def _patch_now(self, patched: datetime) -> None:
        from datetime import datetime as real_dt

        class _PatchedDT(real_dt):
            @classmethod
            def now(cls, tz=None):
                return patched if tz is None else patched.astimezone(tz)

        self._orig_main_dt = getattr(self, "_orig_main_dt", engine_main.datetime)
        engine_main.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        if hasattr(self, "_orig_main_dt"):
            engine_main.datetime = self._orig_main_dt

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

    async def test_mock_bracket_submit_ack_exposes_protective_stop_child(self) -> None:
        await self.connector.connect()

        ack = await self.connector.submit_order(
            ticker="G",
            side="buy",
            qty=71,
            limit_price=Decimal("34.50"),
            stop_loss=Decimal("30.00"),
            time_in_force="DAY",
            client_tag="k2bi:g-2026-05_2nd-wave-paper-trade:T1",
            order_type="LMT",
        )

        self.assertEqual(ack.broker_perm_id, "2000000")
        self.assertEqual(ack.stop_broker_perm_id, "2000001")
        self.assertEqual(ack.stop_price, Decimal("30.00"))

    async def test_bracket_stop_ack_populates_active_protective_stop_record(self) -> None:
        await self._patch_now(_mid_session_utc())
        engine = self._make_engine()

        startup = await engine.tick_once()
        self.assertEqual(startup.state_after, EngineState.CONNECTED_IDLE)
        submitted = await engine.tick_once()
        self.assertEqual(submitted.state_after, EngineState.AWAITING_FILL)

        record = engine._active_protective_stops["G"]
        self.assertEqual(record.ticker, "G")
        self.assertEqual(record.strategy_id, "g-2026-05_2nd-wave-paper-trade")
        self.assertEqual(record.parent_perm_id, 2000000)
        self.assertEqual(record.stop_perm_id, 2000001)
        self.assertEqual(record.stop_price, Decimal("30.00"))
        self.assertIsInstance(record.submitted_at, datetime)

    async def test_stop_terminal_statuses_clear_active_protective_stop_record(self) -> None:
        ProtectiveStopRecord = getattr(engine_main, "ProtectiveStopRecord", None)
        self.assertIsNotNone(ProtectiveStopRecord)
        engine = self._make_engine()
        record = ProtectiveStopRecord(
            ticker="G",
            stop_perm_id=2000001,
            stop_price=Decimal("30.00"),
            parent_perm_id=2000000,
            submitted_at=_mid_session_utc(),
            strategy_id="g-2026-05_2nd-wave-paper-trade",
        )

        engine._active_protective_stops["G"] = record
        engine._clear_active_protective_stop_on_terminal(
            ticker="G",
            stop_perm_id=2000001,
            terminal_status="Submitted",
        )
        self.assertEqual(engine._active_protective_stops["G"], record)

        engine._clear_active_protective_stop_on_terminal(
            ticker="G",
            stop_perm_id=9999999,
            terminal_status="Filled",
        )
        self.assertEqual(engine._active_protective_stops["G"], record)

        for status in ("Filled", "Cancelled", "Inactive"):
            engine._active_protective_stops["G"] = record
            engine._clear_active_protective_stop_on_terminal(
                ticker="G",
                stop_perm_id=2000001,
                terminal_status=status,
            )
            self.assertNotIn("G", engine._active_protective_stops)
