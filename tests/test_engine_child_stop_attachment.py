"""Spec B §4 tests for child-stop attachment and recovery-only stop repair."""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.connectors.types import BrokerOrderAck, BrokerPosition
from execution.journal.writer import JournalWriter
from execution.strategies import runner as strategy_runner


class _AttachmentConnector:
    def __init__(self, positions: list[BrokerPosition] | None = None) -> None:
        self.positions = positions or []
        self.stop_orders: list[dict] = []

    async def get_positions(self) -> list[BrokerPosition]:
        return list(self.positions)

    async def submit_standalone_stop_order(
        self,
        *,
        ticker: str,
        side: str,
        qty: int,
        stop_price: Decimal,
        time_in_force: str,
        client_tag: str | None = None,
    ) -> BrokerOrderAck:
        self.stop_orders.append(
            {
                "ticker": ticker,
                "side": side,
                "qty": qty,
                "stop_price": stop_price,
                "time_in_force": time_in_force,
                "client_tag": client_tag,
            }
        )
        return BrokerOrderAck(
            broker_order_id="7001",
            broker_perm_id="8001",
            submitted_at=datetime.now(timezone.utc),
            status="Submitted",
        )


class ChildStopAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.journal = JournalWriter(base_dir=self.base / "journal", git_sha="test04")

    async def asyncTearDown(self) -> None:
        self._tmp.cleanup()

    def _events(self, event_type: str) -> list[dict]:
        return [
            record
            for record in self.journal.read_all()
            if record["event_type"] == event_type
        ]

    async def test_c6_missing_recovery_context_refuses_without_broker_call(self) -> None:
        connector = _AttachmentConnector(
            positions=[BrokerPosition(ticker="G", qty=71, avg_price=Decimal("32.00"))]
        )

        with self.assertRaises(strategy_runner.RecoveryContextError):
            await strategy_runner.attach_protective_stop_to_existing_position(
                connector=connector,
                journal=self.journal,
                symbol="G",
                qty=71,
                stop_price=Decimal("30.00"),
                strategy_id="g-2026-05",
                recovery_context=None,
            )

        self.assertEqual(connector.stop_orders, [])
        refused = self._events("protective_stop_attach_refused_no_recovery_context")
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["payload"]["strategy_id"], "g-2026-05")
        self.assertEqual(refused[0]["payload"]["symbol"], "G")


if __name__ == "__main__":
    unittest.main()
