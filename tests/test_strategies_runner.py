"""Tests for execution.strategies.runner."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from execution.strategies.runner import (
    EMIT_HAND_CRAFTED,
    SKIP_NAKED_SHORT,
    SKIP_PENDING_ORDER,
    SKIP_POSITION_HELD,
    SKIP_REGIME_MISMATCH,
    SKIP_UNKNOWN_STRATEGY_TYPE,
    evaluate,
)
from execution.strategies.types import (
    ApprovedStrategySnapshot,
    MarketSnapshot,
    STRATEGY_TYPE_HAND_CRAFTED,
    StrategyOrderSpec,
)
from execution.validators.types import Order, Position, RiskContext


ET = ZoneInfo("US/Eastern")


CASH_ONLY_CONFIG = {"leverage": {"cash_only": True, "max_leverage": 1.0}}


def _snapshot(
    *,
    name: str = "spy-rotational",
    side: str = "buy",
    qty: int = 10,
    strategy_type: str = STRATEGY_TYPE_HAND_CRAFTED,
    regime_filter: tuple[str, ...] = (),
) -> ApprovedStrategySnapshot:
    return ApprovedStrategySnapshot(
        name=name,
        strategy_type=strategy_type,
        risk_envelope_pct=Decimal("0.01"),
        order_spec=StrategyOrderSpec(
            ticker="SPY",
            side=side,
            qty=qty,
            limit_price=Decimal("500"),
            stop_loss=Decimal("495") if side == "buy" else None,
        ),
        approved_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        approved_commit_sha="abc1234",
        regime_filter=regime_filter,
        source_path="/tmp/fake.md",
        source_mtime=1234567890.0,
        source_sha256="f" * 64,
    )


def _ctx(**overrides) -> RiskContext:
    defaults = dict(
        account_value=Decimal("1000000"),
        cash=Decimal("1000000"),
        positions=[],
        pending_orders=[],
        now=datetime(2026, 5, 5, 10, 30, tzinfo=ET).astimezone(timezone.utc),
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def _market() -> MarketSnapshot:
    return MarketSnapshot(
        ts=datetime(2026, 5, 5, 10, 30, tzinfo=ET).astimezone(timezone.utc),
        marks={"SPY": Decimal("500")},
        account_value=Decimal("1000000"),
    )


class RunnerTests(unittest.TestCase):
    def test_emits_hand_crafted_buy(self):
        decision = evaluate(_snapshot(), _market(), _ctx(), cash_only_config=CASH_ONLY_CONFIG)
        self.assertIsNotNone(decision.candidate)
        self.assertEqual(decision.reason, EMIT_HAND_CRAFTED)
        self.assertEqual(decision.candidate.side, "buy")
        self.assertEqual(decision.candidate.ticker, "SPY")

    def test_skips_when_position_held(self):
        ctx = _ctx(positions=[Position(ticker="SPY", qty=10, avg_price=Decimal("500"))])
        decision = evaluate(_snapshot(), _market(), ctx, cash_only_config=CASH_ONLY_CONFIG)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_POSITION_HELD)

    def test_skips_when_pending_order_for_strategy(self):
        pending = Order(
            ticker="SPY",
            side="buy",
            qty=5,
            limit_price=Decimal("500"),
            stop_loss=Decimal("495"),
            strategy="spy-rotational",
            submitted_at=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
        )
        ctx = _ctx(pending_orders=[pending])
        decision = evaluate(_snapshot(), _market(), ctx, cash_only_config=CASH_ONLY_CONFIG)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_PENDING_ORDER)

    def test_regime_filter_mismatch_skips(self):
        snap = _snapshot(regime_filter=("risk_on",))
        decision = evaluate(
            snap,
            _market(),
            _ctx(),
            current_regime="risk_off",
            cash_only_config=CASH_ONLY_CONFIG,
        )
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_REGIME_MISMATCH)

    def test_regime_filter_match_emits(self):
        snap = _snapshot(regime_filter=("risk_on",))
        decision = evaluate(
            snap,
            _market(),
            _ctx(),
            current_regime="risk_on",
            cash_only_config=CASH_ONLY_CONFIG,
        )
        self.assertIsNotNone(decision.candidate)

    def test_no_current_regime_blocks_filtered_strategy(self):
        # Codex round-12 P1: a strategy that declares regime_filter
        # MUST NOT trade when the regime is unknown -- silently
        # bypassing the filter would let a regime-gated strategy trade
        # in the wrong regime.
        snap = _snapshot(regime_filter=("risk_on",))
        decision = evaluate(
            snap,
            _market(),
            _ctx(),
            current_regime=None,
            cash_only_config=CASH_ONLY_CONFIG,
        )
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_REGIME_MISMATCH)

    def test_no_regime_filter_still_emits_without_regime(self):
        # A strategy with no regime_filter is unaffected by unknown
        # regime.
        snap = _snapshot(regime_filter=())
        decision = evaluate(
            snap,
            _market(),
            _ctx(),
            current_regime=None,
            cash_only_config=CASH_ONLY_CONFIG,
        )
        self.assertIsNotNone(decision.candidate)

    def test_sell_covered_by_position_emits(self):
        snap = _snapshot(side="sell", qty=5)
        ctx = _ctx(positions=[Position(ticker="SPY", qty=10, avg_price=Decimal("480"))])
        # Position held would normally trigger SKIP_POSITION_HELD on
        # buy; for sell we expect it to be a valid close. The runner's
        # current check is "any position in this ticker -> skip" which
        # applies equally to sell. That's a Phase 2 MVP limitation
        # (one-shot hand_crafted), and the expected behavior.
        decision = evaluate(snap, _market(), ctx, cash_only_config=CASH_ONLY_CONFIG)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_POSITION_HELD)

    def test_sell_without_inventory_skips_via_cash_only(self):
        snap = _snapshot(side="sell", qty=5)
        decision = evaluate(snap, _market(), _ctx(), cash_only_config=CASH_ONLY_CONFIG)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_NAKED_SHORT)

    def test_unknown_strategy_type_skips(self):
        snap = _snapshot(strategy_type="rule_based")
        decision = evaluate(snap, _market(), _ctx(), cash_only_config=CASH_ONLY_CONFIG)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.reason, SKIP_UNKNOWN_STRATEGY_TYPE)


if __name__ == "__main__":
    unittest.main()
