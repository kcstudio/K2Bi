"""Spec B §2 tests for journal-backed order deduplication."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from execution.connectors.mock import MockIBKRConnector
from execution.engine.main import DEFAULT_TICK_SECONDS, Engine, EngineConfig
from execution.journal.schema import (
    JournalReplayMalformedJsonError,
    JournalReplaySchemaVersionError,
    JournalReplayTruncatedLineError,
    JournalReplayUnknownEventTypeError,
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
    "instrument_whitelist": {"symbols": ["SPY"]},
}


def _mid_session_utc() -> datetime:
    return datetime(2026, 4, 21, 10, 30, tzinfo=ET).astimezone(timezone.utc)


def _write_strategy(dir: Path, *, name: str = "spy-rotational") -> Path:
    text = (
        "---\n"
        f"name: {name}\n"
        "status: approved\n"
        "strategy_type: hand_crafted\n"
        "risk_envelope_pct: 0.01\n"
        "approved_at: 2026-05-01T10:00:00Z\n"
        "approved_commit_sha: abc1234\n"
        "order:\n"
        "  ticker: SPY\n"
        "  side: buy\n"
        "  qty: 10\n"
        "  limit_price: 500.00\n"
        "  stop_loss: 495.00\n"
        "  time_in_force: DAY\n"
        "---\n\n## How This Works\n\nPlain-English block.\n"
    )
    path = dir / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


def _raw_record(
    *,
    event_type: str,
    schema_version: int = 2,
    strategy: str | None = "spy-rotational",
    broker_order_id: str | None = "42",
) -> str:
    return (
        "{"
        '"ts":"2026-05-10T12:00:00.000000+00:00",'
        f'"schema_version":{schema_version},'
        f'"event_type":"{event_type}",'
        '"trade_id":"T-raw",'
        '"journal_entry_id":"J-raw",'
        f'"strategy":{strategy!r},'
        '"git_sha":"test",'
        '"payload":{},'
        '"ticker":"SPY",'
        f'"broker_order_id":{broker_order_id!r}'
        "}"
    ).replace("'", '"')


class OrderDedupTests(unittest.IsolatedAsyncioTestCase):
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
        self.connector.marks = {"SPY": Decimal("500")}
        self.journal = JournalWriter(base_dir=self.journal_dir, git_sha="test02")
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

        self._orig_main_dt = getattr(self, "_orig_main_dt", main_mod.datetime)
        self._orig_mock_dt = getattr(self, "_orig_mock_dt", mock_mod.datetime)
        self._orig_writer_dt = getattr(
            self, "_orig_writer_dt", writer_mod.datetime
        )
        main_mod.datetime = _PatchedDT
        mock_mod.datetime = _PatchedDT
        writer_mod.datetime = _PatchedDT

    async def _unpatch_now(self) -> None:
        import execution.connectors.mock as mock_mod
        import execution.engine.main as main_mod
        import execution.journal.writer as writer_mod

        if hasattr(self, "_orig_main_dt"):
            main_mod.datetime = self._orig_main_dt
        if hasattr(self, "_orig_mock_dt"):
            mock_mod.datetime = self._orig_mock_dt
        if hasattr(self, "_orig_writer_dt"):
            writer_mod.datetime = self._orig_writer_dt

    async def _init_engine(self) -> None:
        await self._patch_now(_mid_session_utc())
        tick = await self.engine.tick_once()
        self.assertEqual(tick.state_after.value, "connected_idle")

    def _append_prior_submission(
        self,
        *,
        strategy: str = "spy-rotational",
        symbol: str = "SPY",
        broker_order_id: str = "42",
    ) -> None:
        self.journal.append(
            "order_submitted",
            payload={
                "status": "Submitted",
                "order_type": "LMT",
                "limit_price": "500.00",
                "stop_loss": "495.00",
                "time_in_force": "DAY",
            },
            strategy=strategy,
            trade_id=f"T-prior-{broker_order_id}",
            ticker=symbol,
            side="buy",
            qty=10,
            broker_order_id=broker_order_id,
            broker_perm_id=f"42{broker_order_id}",
        )

    def _append_terminal(
        self,
        *,
        strategy: str = "spy-rotational",
        symbol: str = "SPY",
        broker_order_id: str = "42",
        terminal_status: str = "Filled",
    ) -> None:
        self.journal.append(
            "order_terminal",
            payload={
                "broker_order_id": broker_order_id,
                "terminal_status": terminal_status,
            },
            strategy=strategy,
            trade_id=f"T-terminal-{broker_order_id}",
            ticker=symbol,
            side="buy",
            qty=10,
            broker_order_id=broker_order_id,
        )

    def _events(self, event_type: str) -> list[dict]:
        return [
            event
            for event in self.journal.read_all()
            if event["event_type"] == event_type
        ]

    async def test_d1_pending_prior_submission_blocks_new_submit(self) -> None:
        await self._init_engine()
        self._append_prior_submission()

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 0)
        self.assertEqual(len(self.connector.submitted_orders), 0)
        skips = self._events("cycle_skipped_pending_prior_submission")
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["payload"]["strategy_id"], "spy-rotational")
        self.assertEqual(skips[0]["payload"]["symbol"], "SPY")
        self.assertEqual(skips[0]["payload"]["pending_order_id"], "42")

    async def test_d2_terminal_filled_order_does_not_block_submit(self) -> None:
        await self._init_engine()
        self._append_prior_submission()
        self._append_terminal(terminal_status="Filled")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertEqual(self._events("cycle_skipped_pending_prior_submission"), [])

    async def test_d3_terminal_rejected_order_does_not_block_submit(self) -> None:
        await self._init_engine()
        self._append_prior_submission()
        self._append_terminal(terminal_status="Rejected")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertEqual(self._events("cycle_skipped_pending_prior_submission"), [])

    async def test_d4_pending_map_rebuilds_on_engine_startup(self) -> None:
        await self._patch_now(_mid_session_utc())
        self._append_prior_submission(broker_order_id="41")
        self._append_terminal(broker_order_id="41", terminal_status="Filled")
        self._append_prior_submission(broker_order_id="43")

        with patch.dict("os.environ", {"K2BI_ALLOW_RECOVERY_MISMATCH": "1"}):
            tick = await self.engine.tick_once()

        self.assertEqual(tick.state_after.value, "connected_idle")
        self.assertEqual(
            self.engine._pending_orders,
            {("spy-rotational", "SPY"): {"43"}},
        )

    async def test_d4a_malformed_json_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text("{not json}\n", encoding="utf-8")

        with self.assertRaises(JournalReplayMalformedJsonError):
            await self.engine.tick_once()

    async def test_d4b_unknown_event_type_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="unknown_spec_b_event") + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayUnknownEventTypeError):
            await self.engine.tick_once()

    async def test_d4c_truncated_final_line_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted"),
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplayTruncatedLineError):
            await self.engine.tick_once()

    async def test_d4d_schema_version_mismatch_fails_closed_on_replay(self) -> None:
        await self._patch_now(_mid_session_utc())
        self.journal.path_for_today().write_text(
            _raw_record(event_type="order_submitted", schema_version=999) + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(JournalReplaySchemaVersionError):
            await self.engine.tick_once()

    async def test_d5_cross_strategy_pending_order_does_not_block_submit(self) -> None:
        _write_strategy(self.strategies_dir, name="spy-secondary")
        await self._init_engine()
        self._append_prior_submission(strategy="spy-rotational")

        tick = await self.engine.tick_once()

        self.assertEqual(tick.orders_submitted, 1)
        self.assertEqual(len(self.connector.submitted_orders), 1)
        self.assertIn(":spy-secondary:", self.connector.submitted_orders[0].client_tag)
        self.assertEqual(len(self._events("cycle_skipped_pending_prior_submission")), 1)
