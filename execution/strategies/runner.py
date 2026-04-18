"""Pure strategy evaluation.

Takes ApprovedStrategySnapshot + MarketSnapshot + engine context,
returns a CandidateOrder or None. Contains NO I/O, NO connector calls,
NO validator invocation -- the engine's tick owns those.

Why this lives separately from the engine (architect Q1-refined):
    - Bundle 4's invest-backtest reuses `evaluate()` against historical
      bars, so strategy logic cannot live inside the engine loop.
    - Unit-testing pure evaluation is trivial without spinning asyncio
      or mocking a connector -- a pure function over data classes.

cash_only invariant: sell-side orders are routed through
`execution.risk.cash_only.check_sell_covered` so the runner never
emits a sell that would become a naked short. The engine's validator
run on the returned CandidateOrder does the same check again -- the
runner's pre-check is a fast-path optimization, not the authoritative
gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..risk import cash_only
from ..validators.types import Order as ValidatorOrder
from ..validators.types import Position as ValidatorPosition
from ..validators.types import RiskContext
from .types import (
    ApprovedStrategySnapshot,
    CandidateOrder,
    MarketSnapshot,
    STRATEGY_TYPE_HAND_CRAFTED,
)


@dataclass(frozen=True)
class EvaluationDecision:
    """Why the runner did or did not emit an order.

    Engine writes the `reason` into the journal on both paths so
    post-mortems can ask "why didn't the engine fire on 2026-05-03?"
    and get a one-line answer.
    """

    candidate: CandidateOrder | None
    reason: str
    detail: dict[str, Any]


SKIP_POSITION_HELD = "position_already_open_for_ticker"
SKIP_PENDING_ORDER = "pending_order_for_strategy"
SKIP_REGIME_MISMATCH = "regime_filter_mismatch"
SKIP_NAKED_SHORT = "would_open_naked_short"
SKIP_UNKNOWN_STRATEGY_TYPE = "unknown_strategy_type"
EMIT_HAND_CRAFTED = "hand_crafted_order_emitted"


def evaluate(
    snapshot: ApprovedStrategySnapshot,
    market: MarketSnapshot,
    ctx: RiskContext,
    *,
    current_regime: str | None = None,
    cash_only_config: dict[str, Any] | None = None,
) -> EvaluationDecision:
    """Single-strategy evaluation entrypoint.

    Phase 2 MVP supports `hand_crafted` only. Phase 3+ introduces
    `rule_based` strategies; this dispatcher grows a new arm when that
    lands. Unknown strategy types return a `skip` decision, never an
    exception -- the engine journals the skip and continues.
    """
    if snapshot.strategy_type != STRATEGY_TYPE_HAND_CRAFTED:
        return EvaluationDecision(
            candidate=None,
            reason=SKIP_UNKNOWN_STRATEGY_TYPE,
            detail={"strategy_type": snapshot.strategy_type},
        )

    # Regime filter: engine caller passes the active regime; the
    # strategy's declared regime_filter is the AND set. Codex round-12
    # P1: a strategy that declares a regime_filter MUST NOT trade when
    # the regime is unknown (failure to pass a regime from engine /
    # regime skill not yet running). Block rather than silently bypass.
    if snapshot.regime_filter:
        if current_regime is None:
            return EvaluationDecision(
                candidate=None,
                reason=SKIP_REGIME_MISMATCH,
                detail={
                    "current_regime": None,
                    "required": list(snapshot.regime_filter),
                    "note": (
                        "regime_filter set on strategy but current "
                        "regime unknown; strategy blocked until regime "
                        "skill publishes wiki/regimes/current.md"
                    ),
                },
            )
        if current_regime not in snapshot.regime_filter:
            return EvaluationDecision(
                candidate=None,
                reason=SKIP_REGIME_MISMATCH,
                detail={
                    "current_regime": current_regime,
                    "required": list(snapshot.regime_filter),
                },
            )

    spec = snapshot.order_spec
    ticker = spec.ticker

    # Suppress duplicate orders: if the strategy already has an open
    # position in its ticker OR a pending order, skip. Hand_crafted
    # strategies are single-shot per "engine-lifetime or human reset";
    # the runner never stacks.
    if _any_position(ticker, ctx):
        return EvaluationDecision(
            candidate=None,
            reason=SKIP_POSITION_HELD,
            detail={"ticker": ticker},
        )
    if _any_pending_order_for_strategy(snapshot.name, ctx):
        return EvaluationDecision(
            candidate=None,
            reason=SKIP_PENDING_ORDER,
            detail={"strategy": snapshot.name},
        )

    # Pre-emit cash-only fast path for sell side. Engine re-runs the
    # full validator cascade (including cash_only via leverage) once
    # the CandidateOrder lands, so this is a short-circuit, not the
    # authoritative gate.
    if spec.side == "sell":
        pre_order = _to_validator_order(snapshot, market)
        pre_result = cash_only.check_sell_covered(
            pre_order, ctx, cash_only_config or {"leverage": {"cash_only": True}}
        )
        if not pre_result.approved:
            return EvaluationDecision(
                candidate=None,
                reason=SKIP_NAKED_SHORT,
                detail=pre_result.detail,
            )

    candidate = CandidateOrder(
        strategy=snapshot.name,
        ticker=ticker,
        side=spec.side,
        qty=spec.qty,
        limit_price=spec.limit_price,
        stop_loss=spec.stop_loss,
        time_in_force=spec.time_in_force,
        reason=EMIT_HAND_CRAFTED,
    )
    return EvaluationDecision(
        candidate=candidate,
        reason=EMIT_HAND_CRAFTED,
        detail={
            "ticker": ticker,
            "side": spec.side,
            "qty": spec.qty,
            "strategy_type": snapshot.strategy_type,
        },
    )


def _any_position(ticker: str, ctx: RiskContext) -> bool:
    return any(p.ticker == ticker and p.qty != 0 for p in ctx.positions)


def _any_pending_order_for_strategy(name: str, ctx: RiskContext) -> bool:
    return any(o.strategy == name for o in ctx.pending_orders)


def _to_validator_order(
    snapshot: ApprovedStrategySnapshot,
    market: MarketSnapshot,
) -> ValidatorOrder:
    spec = snapshot.order_spec
    return ValidatorOrder(
        ticker=spec.ticker,
        side=spec.side,
        qty=spec.qty,
        limit_price=spec.limit_price,
        stop_loss=spec.stop_loss,
        strategy=snapshot.name,
        submitted_at=market.ts,
        extended_hours=False,
    )


__all__ = [
    "EMIT_HAND_CRAFTED",
    "EvaluationDecision",
    "SKIP_NAKED_SHORT",
    "SKIP_PENDING_ORDER",
    "SKIP_POSITION_HELD",
    "SKIP_REGIME_MISMATCH",
    "SKIP_UNKNOWN_STRATEGY_TYPE",
    "evaluate",
]
