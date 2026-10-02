"""Validate a saved five-symbol IBKR daily-bar check for display only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from scripts.stage2_quotes.ibkr_adapter import parse_ibkr_daily_check


PILOT_SYMBOLS = ("G", "CALX", "CDNS", "SPY", "XLV")
MAX_BYTES = 1024 * 1024


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys at any nesting level."""
    output: dict[str, Any] = {}
    for key, value in pairs:
        if key in output:
            raise ValueError(f"duplicate JSON key: {key}")
        output[key] = value
    return output


def _aware(raw: object) -> datetime:
    """Parse an explicit-offset timestamp."""
    if not isinstance(raw, str):
        raise ValueError("batch timestamp is not a string")
    try:
        value = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError("invalid batch timestamp") from exc
    if value.utcoffset() is None:
        raise ValueError("batch timestamp has no timezone")
    return value


def parse_ibkr_daily_batch(raw: bytes) -> dict[str, object]:
    """Return dated bars or unavailable states from one saved operator check.

    No returned price may enter the current synthetic trading ledger. A failed
    symbol remains unavailable and never inherits a previous price.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_BYTES:
        raise ValueError("batch must be nonempty bytes under 1 MiB")
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid batch JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "kind", "connection_mode", "started_at", "completed_at", "symbols", "results",
    }:
        raise ValueError("batch schema changed")
    if payload["kind"] != "ibkr-paper-daily-five" or payload["connection_mode"] != "readonly":
        raise ValueError("batch is not a read-only paper check")
    if payload["symbols"] != list(PILOT_SYMBOLS):
        raise ValueError("batch symbol list changed")
    results = payload["results"]
    if not isinstance(results, dict) or set(results) != set(PILOT_SYMBOLS):
        raise ValueError("batch result set changed")
    started, completed = _aware(payload["started_at"]), _aware(payload["completed_at"])
    if completed < started:
        raise ValueError("batch completed before it started")

    output: dict[str, dict[str, object]] = {}
    for symbol in PILOT_SYMBOLS:
        row = results[symbol]
        if not isinstance(row, dict):
            raise ValueError("batch result is not an object")
        if row.get("status") == "unavailable":
            if set(row) != {"status", "reason"} or not isinstance(row["reason"], str):
                raise ValueError("unavailable result shape changed")
            output[symbol] = {"status": "unavailable", "reason": "IBKR daily bar unavailable"}
            continue
        if row.get("status") != "bars_returned":
            raise ValueError("unrecognized batch result")
        received = _aware(row.get("received_at"))
        if not started <= received <= completed:
            raise ValueError("result time outside batch interval")
        item_raw = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            bar = parse_ibkr_daily_check(item_raw, expected_symbol=symbol, as_of=payload["completed_at"])
        except ValueError as exc:
            output[symbol] = {"status": "rejected", "reason": str(exc)}
        else:
            output[symbol] = {"status": "available", "bar": bar}
    return {
        "kind": "ibkr-daily-price-evidence", "source": "IBKR paper Gateway",
        "received_at": payload["completed_at"], "raw_sha256": hashlib.sha256(raw).hexdigest(),
        "resolution": "daily", "use": "display-only", "trade_action": "none",
        "rows": output,
    }
