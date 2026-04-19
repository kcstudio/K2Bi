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
"""

from __future__ import annotations

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
    }
)

EVENT_TYPES = EVENT_TYPES_V1 | EVENT_TYPES_V2_ADDITIONS

KNOWN_SCHEMA_VERSIONS = frozenset({1, 2})

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
