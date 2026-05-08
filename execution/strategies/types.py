# cash-only invariant: no sell-side enforcement in this module (data
# types only). Sell-side gating owned by execution.risk.cash_only and
# called by runner.py + engine main pre-submit hook.
"""Typed dataclasses shared across loader, runner, and the engine.

Two distinct shapes, per architect Q2-refined decision:

    StrategyDocument          -- everything the .md file contains.
                                 Consumed by Bundle 3
                                 (invest-propose-limits) and Bundle 4
                                 (invest-bear-case) which need the full
                                 authored artifact, including the
                                 "How This Works" pedagogical block.

    ApprovedStrategySnapshot  -- immutable runtime config the engine
                                 loads at startup (or on
                                 proposed->approved transition via
                                 /invest-ship). Mid-flight .md edits
                                 do NOT change engine behavior; loader
                                 detects drift and surfaces a
                                 strategy_file_modified_post_approval
                                 event so Keith re-approves
                                 intentionally.

The runtime snapshot captures file_sha256 + source_mtime at load
time; engine drift detection compares current file state against
this frozen snapshot on every tick.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any


# Status values a strategy file can carry.
STATUS_PROPOSED = "proposed"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_RETIRED = "retired"
ALLOWED_STATUSES = frozenset(
    {STATUS_PROPOSED, STATUS_APPROVED, STATUS_REJECTED, STATUS_RETIRED}
)

# Strategy types we know how to evaluate. Phase 2 ships hand_crafted
# only; Phase 3 adds rule_based evaluators. Adding a new type is a
# Bundle 4+ concern.
STRATEGY_TYPE_HAND_CRAFTED = "hand_crafted"
ALLOWED_STRATEGY_TYPES = frozenset({STRATEGY_TYPE_HAND_CRAFTED})

# Order types the loader accepts. LMT requires a Decimal limit_price;
# MKT allows limit_price=None (or a non-null reference-price hint that
# downstream consumers may ignore). Adding a new order type is an
# explicit-handling change in `loader._parse_order_spec`; the loader
# refuses unknown order_type values rather than silently coercing.
ORDER_TYPE_LMT = "LMT"
ORDER_TYPE_MKT = "MKT"
ALLOWED_ORDER_TYPES = frozenset({ORDER_TYPE_LMT, ORDER_TYPE_MKT})


@dataclass(frozen=True)
class StrategyOrderSpec:
    """The order shape embedded in a hand_crafted strategy.

    For a rule_based strategy (Phase 3+), this field is absent and the
    runner instead evaluates a rule tree to build an order on demand.

    `order_type` defaults to LMT for backward compatibility -- pre-MKT
    strategies authored before 2026-05-08 omitted the field and the
    loader required `limit_price` as a Decimal, which is LMT semantics.
    `limit_price` is None when `order_type == MKT` and the YAML carried
    null; downstream validators that need a price anchor for MKT
    orders should consult last-tick / reference-price elsewhere
    (engine-side; not the loader's concern).
    """

    ticker: str
    side: str              # "buy" or "sell"
    qty: int
    limit_price: Decimal | None
    order_type: str = ORDER_TYPE_LMT
    stop_loss: Decimal | None = None
    time_in_force: str = "DAY"


@dataclass(frozen=True)
class StrategyDocument:
    """Full parse of a wiki/strategies/<name>.md file.

    Includes the pedagogical body ("how_this_works") that Bundle 4's
    bear-case skill leans on. Never directly executed -- the engine
    takes ApprovedStrategySnapshot only.
    """

    name: str
    status: str
    strategy_type: str
    risk_envelope_pct: Decimal
    order_spec: StrategyOrderSpec | None
    approved_at: datetime | None
    approved_commit_sha: str | None
    regime_filter: tuple[str, ...] = ()
    how_this_works: str = ""
    source_path: str = ""
    source_mtime: float = 0.0
    source_sha256: str = ""
    raw_frontmatter: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ApprovedStrategySnapshot:
    """Frozen runtime config. The engine mutates no field; the loader
    constructs one per approved strategy at startup and any subsequent
    proposed->approved transition.

    Drift detection: runner's caller (engine tick) re-stats the
    source_path each tick; if mtime > snapshot.source_mtime, a
    strategy_file_modified_post_approval event is journaled and the
    snapshot continues to drive decisions (file mutation is not a
    promotion path; /invest-ship is).
    """

    name: str
    strategy_type: str
    risk_envelope_pct: Decimal
    order_spec: StrategyOrderSpec
    approved_at: datetime
    approved_commit_sha: str
    regime_filter: tuple[str, ...]
    source_path: str
    source_mtime: float
    source_sha256: str


@dataclass(frozen=True)
class MarketSnapshot:
    """What the runner knows about the market at decision time.

    Minimal Phase 2 MVP: marks + account value. Phase 3+ adds
    historical bars for rule_based evaluators. The engine populates
    this from connector calls before invoking the runner.
    """

    ts: datetime
    marks: dict[str, Decimal]
    account_value: Decimal


@dataclass(frozen=True)
class CandidateOrder:
    """What the runner hands back to the engine.

    The engine converts this into an `execution.validators.types.Order`
    by stamping submitted_at (= engine tick clock). Runner does not
    know engine time -- that is the engine's concern.
    """

    strategy: str
    ticker: str
    side: str
    qty: int
    limit_price: Decimal | None
    stop_loss: Decimal | None
    time_in_force: str
    reason: str
    order_type: str = ORDER_TYPE_LMT
    trade_id: str | None = None


class StrategyLoaderError(ValueError):
    """Raised by loader when a .md file fails to parse or validate."""


class StrategyFileModifiedError(StrategyLoaderError):
    """Raised when a file's sha256 no longer matches the approved
    snapshot. The caller (engine) downgrades this to a journaled
    strategy_file_modified_post_approval event and keeps the original
    snapshot in effect."""

    def __init__(
        self,
        message: str,
        *,
        name: str,
        approved_sha256: str,
        current_sha256: str,
    ) -> None:
        super().__init__(message)
        self.name = name
        self.approved_sha256 = approved_sha256
        self.current_sha256 = current_sha256


__all__ = [
    "ALLOWED_ORDER_TYPES",
    "ALLOWED_STATUSES",
    "ALLOWED_STRATEGY_TYPES",
    "ApprovedStrategySnapshot",
    "CandidateOrder",
    "MarketSnapshot",
    "ORDER_TYPE_LMT",
    "ORDER_TYPE_MKT",
    "STATUS_APPROVED",
    "STATUS_PROPOSED",
    "STATUS_REJECTED",
    "STATUS_RETIRED",
    "STRATEGY_TYPE_HAND_CRAFTED",
    "StrategyDocument",
    "StrategyFileModifiedError",
    "StrategyLoaderError",
    "StrategyOrderSpec",
]
