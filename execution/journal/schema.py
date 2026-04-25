"""Decision journal schema.

Versions:
    v1 (2026-04-18) shipped with Bundle 1 m2.7.
    v2 (2026-04-18) ships with Bundle 2 -- adds 16 event types for the
       engine state machine + recovery + reconnect + EOD + strategy
       drift. Also lifts `broker_order_id` and `broker_perm_id` into
       optional top-level fields so reconciliation can join by broker
       identity without parsing payload.

Schema evolution rules (K2B architect, doc'd in journal-schema.md):
    - Old records stay at their original schema_version. Writers always
      emit the current version. Readers MUST handle every version from
      v1 onward. No history rewrites.
    - Validator rejects records whose `schema_version` is not a known
      version -- this is a cheap tamper/typo guard on the writer side;
      it is NOT a migration contract.
    - Optional additions (new event types, new optional top-level fields)
      do NOT require a version bump. Required-field additions DO.

v2 diff from v1:
    - SCHEMA_VERSION: 1 -> 2
    - Added 16 event types (see EVENT_TYPES_V2_ADDITIONS)
    - Added optional top-level fields: broker_order_id, broker_perm_id

v2 additive (2026-04-20, m2.23 Phase 5 metric audit):
    - Added optional top-level fields for Phase 5 metric capture on day 1
      rather than back-patching at day 90:
        slippage_bps              -> Phase 5.5 slippage vs expectation
        commission_usd            -> Phase 5.6 fee erosion (broker commission)
        fees_total_usd            -> Phase 5.6 fee erosion (incl regulatory)
        correlation_vs_portfolio  -> Phase 5.7 correlation check
      Additive-only; SCHEMA_VERSION unchanged per the evolution rule.

v2 additive (2026-04-26, Q42 orphan-STOP adoption):
    - Added event type `orphan_stop_adopted` so operator-portal-submitted
      STOPs that pre-date the engine's awareness can be recorded as
      first-class journal events. Future recovery passes recognize them
      as KNOWN broker state instead of re-flagging on every cold-start.
      Required payload fields: permId (int), ticker (str), qty (int),
      stop_price (Decimal as str), source (enum), adopted_at (ISO8601 UTC),
      justification (non-empty str). Field-level validation lives in
      validate_orphan_stop_adopted_payload() (kept separate from
      validate() to preserve its cheap-checks-only contract).
      Additive-only; SCHEMA_VERSION unchanged per the evolution rule.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any


SCHEMA_VERSION = 2

# Event types present since v1. Kept as a separate frozenset so the
# journal-schema.md doc + tests can reference "what v1 shipped with"
# without pattern-matching on names.
EVENT_TYPES_V1 = frozenset(
    {
        "validator_pass",
        "validator_reject",
        "order_submitted",
        "order_filled",
        "breaker_triggered",
        "kill_switch_written",
        "kill_switch_cleared",
        "recovery_truncated",
    }
)

# v2 additions. Ordered by state-machine phase in the comment for
# readability; the frozenset itself is unordered.
EVENT_TYPES_V2_ADDITIONS = frozenset(
    {
        # order lifecycle
        "order_proposed",
        "order_rejected",
        "order_timeout",
        # engine lifecycle
        "engine_started",
        "engine_stopped",
        "engine_recovered",
        # recovery outcomes
        "recovery_state_mismatch",
        "recovery_reconciled",
        # strategy integrity
        "strategy_file_modified_post_approval",
        "avg_price_drift",
        # kill-switch lifecycle (clearing already in v1 as kill_switch_cleared)
        "kill_blocked",
        "kill_cleared",
        # connection lifecycle
        "auth_required",
        "auth_recovered",
        "reconnected",
        "disconnect_status",
        # end-of-day session boundary
        "eod_cancel",
        "eod_complete",
        # Q33 (2026-04-21): --once pre-exit barrier. Emitted when the
        # engine deliberately exits mid-wait after a submit body
        # without observing terminal broker status. Q39-B recovery
        # promotes evidence=barrier_timeout for matching trade_ids in
        # the pending_orders payload on subsequent restart.
        "once_exit_barrier_timeout",
    }
)

# Q42 (2026-04-26): orphan-STOP adoption workflow. Operator-portal-submitted
# STOPs that pre-date the engine's awareness can be adopted as first-class
# journal events so future recovery passes recognize them as KNOWN broker
# state. Kept as its own frozenset so the v2-additive history stays
# traceable session-by-session (mirrors the spirit of m2.23's separation,
# even though m2.23 added optional top-level fields rather than event
# types). Additive-only; SCHEMA_VERSION unchanged per the evolution rule.
EVENT_TYPES_V2_ADDITIVE_Q42 = frozenset({"orphan_stop_adopted"})

EVENT_TYPES = (
    EVENT_TYPES_V1
    | EVENT_TYPES_V2_ADDITIONS
    | EVENT_TYPES_V2_ADDITIVE_Q42
)

KNOWN_SCHEMA_VERSIONS = frozenset({1, 2})

# Q42: source enum for orphan_stop_adopted.source. Only operator-portal is
# wired in this ship (matches the Phase 3.6 Day 1 use case); operator-tws
# and external-api are reserved for Phase 4+ when arbitrary external STOP
# adoption gets generalized.
ORPHAN_STOP_ADOPTED_SOURCES = frozenset(
    {"operator-portal", "operator-tws", "external-api"}
)

REQUIRED_TOP_LEVEL = (
    "ts",
    "schema_version",
    "event_type",
    "trade_id",
    "journal_entry_id",
    "strategy",
    "git_sha",
    "payload",
)

# Optional top-level fields recognized by the writer. Not enforced --
# listed for documentation + consumer reference.
OPTIONAL_TOP_LEVEL = (
    "error",
    "metadata",
    "ticker",
    "side",
    "qty",
    "broker_order_id",            # v2: IB orderId (int serialized as str)
    "broker_perm_id",             # v2: IB permId -- stable across IB Gateway restarts
    # m2.23 Phase 5 metric capture (additive, 2026-04-20):
    "slippage_bps",               # 5.5: signed float, negative = fill worse than ref
    "commission_usd",             # 5.6: broker commission on the ticket (float)
    "fees_total_usd",             # 5.6: aggregate ticket fees incl regulatory (float)
    "correlation_vs_portfolio",   # 5.7: -1..1 snapshot at decision time
)


class JournalSchemaError(ValueError):
    pass


def reject_non_finite_json_constant(raw: str) -> Any:
    """`json.loads(..., parse_constant=...)` callback that rejects NaN / Infinity.

    The journal's audit contract is RFC-8259 JSON; NaN / Infinity /
    -Infinity tokens are a Python-only extension that strict
    downstream consumers (non-Python, different runtimes) cannot read.
    Every read/write path that touches journal JSON delegates to this
    callback so the contract is enforced identically across writer,
    reader, crash recovery, and out-of-process diagnose tooling.

    Codex m2.23 round-3 surface audit: keep this helper in schema.py
    (not writer.py) so cross-module callers -- engine diagnose reader,
    future replay helpers -- can import the contract without reaching
    into writer internals.
    """
    raise ValueError(f"non-finite JSON constant in journal: {raw!r}")


def validate(record: dict[str, Any]) -> None:
    """Raise JournalSchemaError if the record can't be journaled.

    Cheap checks only: required-field presence, event_type enum, and a
    schema_version the writer recognizes. Field-level payload shape is
    the caller's responsibility.

    The writer always emits records stamped with the current
    SCHEMA_VERSION (2). We accept KNOWN_SCHEMA_VERSIONS here so that a
    future migration helper that validates already-on-disk v1 records
    against the v2 codebase does not spuriously fail.
    """
    missing = [k for k in REQUIRED_TOP_LEVEL if k not in record]
    if missing:
        raise JournalSchemaError(f"missing required fields: {missing}")
    if record["event_type"] not in EVENT_TYPES:
        raise JournalSchemaError(f"unknown event_type: {record['event_type']}")
    if record["schema_version"] not in KNOWN_SCHEMA_VERSIONS:
        raise JournalSchemaError(
            f"unknown schema_version: {record['schema_version']}; "
            f"known={sorted(KNOWN_SCHEMA_VERSIONS)}"
        )


def validate_orphan_stop_adopted_payload(payload: dict[str, Any]) -> None:
    """Field-level validation for orphan_stop_adopted event payloads.

    Q42: called by recovery.py when constructing the event so a malformed
    payload never lands in the journal. NOT called from validate() above
    -- that helper stays cheap-checks-only per its existing contract.
    Raises JournalSchemaError on any violation.

    Required fields and their constraints:
        permId          positive int (bool excluded -- Python's
                        isinstance(True, int) trap)
        ticker          non-empty str
        qty             non-zero int (bool excluded; sign carries
                        broker-side direction)
        stop_price      parseable as finite Decimal > 0
        source          one of ORPHAN_STOP_ADOPTED_SOURCES
        adopted_at      ISO8601 string with explicit timezone
        justification   non-empty after whitespace strip
    """
    required = (
        "permId",
        "ticker",
        "qty",
        "stop_price",
        "source",
        "adopted_at",
        "justification",
    )
    missing = [k for k in required if k not in payload]
    if missing:
        raise JournalSchemaError(
            f"orphan_stop_adopted payload missing fields: {missing}"
        )
    perm = payload["permId"]
    # bool subclasses int in Python; reject explicitly so True/False
    # cannot sneak past the type guard.
    if isinstance(perm, bool) or not isinstance(perm, int) or perm <= 0:
        raise JournalSchemaError(
            f"orphan_stop_adopted permId must be positive int, got {perm!r}"
        )
    ticker = payload["ticker"]
    if not isinstance(ticker, str) or not ticker.strip():
        raise JournalSchemaError(
            f"orphan_stop_adopted ticker must be non-empty str, got {ticker!r}"
        )
    qty = payload["qty"]
    if isinstance(qty, bool) or not isinstance(qty, int) or qty == 0:
        raise JournalSchemaError(
            f"orphan_stop_adopted qty must be non-zero int, got {qty!r}"
        )
    raw_stop = payload["stop_price"]
    try:
        stop = Decimal(str(raw_stop))
    except (InvalidOperation, ValueError, TypeError):
        raise JournalSchemaError(
            f"orphan_stop_adopted stop_price not parseable as Decimal: "
            f"{raw_stop!r}"
        )
    if not stop.is_finite() or stop <= 0:
        raise JournalSchemaError(
            f"orphan_stop_adopted stop_price must be finite > 0, "
            f"got {raw_stop!r}"
        )
    src = payload["source"]
    if src not in ORPHAN_STOP_ADOPTED_SOURCES:
        raise JournalSchemaError(
            f"orphan_stop_adopted source must be one of "
            f"{sorted(ORPHAN_STOP_ADOPTED_SOURCES)}, got {src!r}"
        )
    adopted = payload["adopted_at"]
    if not isinstance(adopted, str):
        raise JournalSchemaError(
            f"orphan_stop_adopted adopted_at must be str, "
            f"got {type(adopted).__name__}"
        )
    try:
        parsed = datetime.fromisoformat(adopted)
    except ValueError:
        raise JournalSchemaError(
            f"orphan_stop_adopted adopted_at not ISO8601: {adopted!r}"
        )
    if parsed.tzinfo is None:
        raise JournalSchemaError(
            f"orphan_stop_adopted adopted_at must include timezone, "
            f"got {adopted!r}"
        )
    just = payload["justification"]
    if not isinstance(just, str) or not just.strip():
        raise JournalSchemaError(
            "orphan_stop_adopted justification must be non-empty string"
        )
