"""Offline, fixture-only pre-trade risk preparation (Stage 3 paper).

This module NEVER approves, submits, or adopts a trade. It parses and
strictly validates an *offline risk context* document (recorded or
fixture), hashes the exact input bytes, and -- only for a complete fresh
fixture -- instantiates the existing production validator types and
invokes the existing runner against the real read-only validator
config. Every reported decision is a *proposal* with executable,
fill_eligible, and approved pinned to False.

Recorded inputs remain ``not_run`` even when they parse: the trusted
funding / current-quote / approved-rules / journal evidence needed to
authenticate a recorded capture is not present offline.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_BYTES = 1024 * 1024
MAX_DEPTH = 32
ACCOUNT_ID = "DUQ220152"
CURRENCY = "USD"
SCHEMA_VERSION = 1
MAX_WINDOW = timedelta(minutes=15)
CLOCK_SKEW = timedelta(seconds=15)

_TICKER_RE = re.compile(r"[A-Z]{1,6}\Z")
_MONEY_RE = re.compile(r"[+-]?(?:0|[1-9][0-9]{0,11})(?:\.[0-9]{1,8})?\Z")
_TS_RE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_TEXT_RE = re.compile(r"[ -~]{1,256}\Z")

_MARKUP = ("<", ">", "`", "${")

MODULE_ORDER = (
    ("instrument_whitelist", "instrument_whitelist"),
    ("market_hours", "market_hours"),
    ("position_size", "position_size"),
    ("trade_risk", "trade_risk"),
    ("leverage", "leverage"),
)

COVERAGE_KEYS = (
    "positions",
    "pending_orders",
    "settled_usd",
    "restrictions",
    "current_quote",
    "approved_rules",
)


class _NotRun(Exception):
    """Structured not_run signal (never a ValueError)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ----------------------------------------------------------------------
# Public parsing helpers (also serve the next strict recovery adapter).
# ----------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number rejected: {name}")


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key: {key!r}")
        out[key] = value
    return out


def load_json(raw: bytes) -> dict[str, Any]:
    """Decode strict UTF-8 JSON bytes into a top-level object."""
    if type(raw) is not bytes:
        raise ValueError("payload must be bytes")
    if len(raw) > MAX_BYTES:
        raise ValueError("payload exceeds 1 MiB")
    text = raw.decode("utf-8", errors="strict")
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_pairs,
        )
    except ValueError as exc:
        raise ValueError(f"invalid JSON: {exc}") from exc
    _check_flat_shape(value, 0)
    if not isinstance(value, dict):
        raise ValueError("top-level payload must be an object")
    return value


def _check_flat_shape(value: Any, depth: int) -> None:
    if isinstance(value, float):
        raise ValueError("JSON floats are not permitted; use decimal strings")
    if depth > MAX_DEPTH:
        raise ValueError("payload nesting too deep")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("object keys must be strings")
            _check_flat_shape(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_flat_shape(item, depth + 1)


def parse_timestamp(value: Any, label: str) -> datetime:
    """Parse an offset-aware ISO-8601 timestamp string, normalized to UTC."""
    if not isinstance(value, str):
        raise ValueError(f"{label}: timestamp must be a string")
    if _TS_RE.fullmatch(value) is None:
        raise ValueError(f"{label}: timestamp must be offset-qualified ISO-8601")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label}: unparseable/overflowing timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label}: timestamp must carry a UTC offset")
    try:
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{label}: timestamp offset conversion overflow") from exc


def parse_money(value: Any, label: str, positive: bool = False) -> Decimal:
    """Parse a canonical decimal *string* (never bool/number/scientific)."""
    if not isinstance(value, str):
        raise ValueError(f"{label}: money must be a plain string")
    if _MONEY_RE.fullmatch(value) is None:
        raise ValueError(f"{label}: money must be signed plain decimal, <=8 digits")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{label}: invalid decimal") from exc
    if not amount.is_finite():
        raise ValueError(f"{label}: non-finite decimal")
    if abs(amount) > Decimal("1e12"):
        raise ValueError(f"{label}: decimal magnitude out of range")
    if positive and amount <= 0:
        raise ValueError(f"{label}: amount must be strictly positive")
    return amount


# ----------------------------------------------------------------------
# Field-level validation.
# ----------------------------------------------------------------------


def _require_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label}: expected object")
    return value


def _require_exact_keys(obj: dict[str, Any], keys: tuple[str, ...], label: str) -> None:
    extra = set(obj) - set(keys)
    missing = set(keys) - set(obj)
    if extra:
        raise ValueError(f"{label}: unexpected keys {sorted(extra)}")
    if missing:
        raise ValueError(f"{label}: missing keys {sorted(missing)}")


def _require_list(value: Any, label: str, limit: int = 1000) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{label}: expected list")
    if len(value) > limit:
        raise ValueError(f"{label}: exceeds {limit} entries")
    return value


def _require_ticker(value: Any, label: str) -> str:
    if not isinstance(value, str) or _TICKER_RE.fullmatch(value) is None:
        raise ValueError(f"{label}: ticker must be 1..6 uppercase letters")
    return value


def _require_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label}: expected string")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label}: must be non-empty")
    if len(value) > 256:
        raise ValueError(f"{label}: exceeds 256 characters")
    if _TEXT_RE.fullmatch(value) is None:
        raise ValueError(f"{label}: contains non-printable characters")
    for token in _MARKUP:
        if token in value:
            raise ValueError(f"{label}: markup is not permitted")
    return stripped


def _require_bool(value: Any, label: str, expected: bool | None = None) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label}: expected exact boolean")
    if expected is not None and value is not expected:
        raise ValueError(f"{label}: expected {expected!r}")
    return value


def _require_int(value: Any, label: str, low: int, high: int) -> int:
    if type(value) is not int:
        raise ValueError(f"{label}: expected exact integer")
    if value < low or value > high:
        raise ValueError(f"{label}: integer out of range")
    return value


def _parse_order(obj: Any, label: str) -> dict[str, Any]:
    fields = (
        "ticker",
        "side",
        "qty",
        "limit_price",
        "stop_loss",
        "strategy",
        "submitted_at",
        "extended_hours",
        "order_type",
    )
    mapping = _require_mapping(obj, label)
    _require_exact_keys(mapping, fields, label)
    ticker = _require_ticker(mapping["ticker"], f"{label}.ticker")
    side = mapping["side"]
    if side not in ("buy", "sell"):
        raise ValueError(f"{label}.side: must be 'buy' or 'sell'")
    qty = _require_int(mapping["qty"], f"{label}.qty", 1, 1_000_000)
    limit = parse_money(mapping["limit_price"], f"{label}.limit_price", positive=True)
    stop_raw = mapping["stop_loss"]
    stop = None
    if stop_raw is not None:
        stop = parse_money(stop_raw, f"{label}.stop_loss", positive=True)
    strategy = _require_text(mapping["strategy"], f"{label}.strategy")
    submitted = parse_timestamp(mapping["submitted_at"], f"{label}.submitted_at")
    _require_bool(mapping["extended_hours"], f"{label}.extended_hours", False)
    if mapping["order_type"] != "LMT":
        raise ValueError(f"{label}.order_type: only 'LMT' is supported")
    return {
        "ticker": ticker,
        "side": side,
        "qty": qty,
        "limit_price": limit,
        "stop_loss": stop,
        "strategy": strategy,
        "submitted_at": submitted,
    }


def _parse_position(obj: Any, label: str) -> dict[str, Any]:
    fields = ("ticker", "qty", "avg_price", "stop_loss")
    mapping = _require_mapping(obj, label)
    _require_exact_keys(mapping, fields, label)
    ticker = _require_ticker(mapping["ticker"], f"{label}.ticker")
    qty = _require_int(mapping["qty"], f"{label}.qty", 1, 1_000_000)
    avg = parse_money(mapping["avg_price"], f"{label}.avg_price", positive=True)
    stop_raw = mapping["stop_loss"]
    stop = None
    if stop_raw is not None:
        stop = parse_money(stop_raw, f"{label}.stop_loss", positive=True)
    return {"ticker": ticker, "qty": qty, "avg_price": avg, "stop_loss": stop}


def _parse_marks(obj: Any) -> dict[str, Decimal]:
    mapping = _require_mapping(obj, "context.current_marks")
    marks: dict[str, Decimal] = {}
    for key, value in mapping.items():
        ticker = _require_ticker(key, "context.current_marks key")
        marks[ticker] = parse_money(value, f"context.current_marks[{ticker}]", positive=True)
    return marks


def _check_stops(
    proposal: dict[str, Any],
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> str | None:
    """Return a structured not_run reason, or None when stops are present."""
    ticker = proposal["ticker"]
    del ticker
    if proposal["side"] == "buy":
        stop = proposal["stop_loss"]
        if stop is None:
            return "proposal: missing known stop-loss (no zero-risk fallback)"
        if stop >= proposal["limit_price"]:
            raise ValueError("proposal: buy stop must sit strictly below limit")
    for index, held in enumerate(positions):
        if held["stop_loss"] is None:
            return f"context.positions[{index}]: held position requires a known stop"
    for index, order in enumerate(pending):
        if order["side"] != "buy":
            continue
        stop = order["stop_loss"]
        if stop is None:
            return f"context.pending_orders[{index}]: pending buy requires a known stop"
        if stop >= order["limit_price"]:
            raise ValueError(
                f"context.pending_orders[{index}]: pending buy stop must sit below limit"
            )
    return None


def _require_marks(
    marks: dict[str, Decimal],
    proposal: dict[str, Any],
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> str | None:
    """Return a structured not_run reason when any needed mark is absent."""
    needed = [proposal["ticker"]]
    needed.extend(item["ticker"] for item in positions)
    needed.extend(order["ticker"] for order in pending)
    missing = sorted({name for name in needed if name not in marks})
    if missing:
        return f"context.current_marks: missing marks for {missing}"
    return None


# ----------------------------------------------------------------------
# Orchestration.
# ----------------------------------------------------------------------

def _hash(raw: bytes | None) -> str:
    if raw is None:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _base_result(
    origin: str | None,
    account_id: str | None,
    captured: str | None,
    evaluated: str,
    expires: str | None,
    raw_sha: str,
    config_sha: str,
) -> dict[str, Any]:
    return {
        "status": "not_run",
        "origin": origin,
        "account_id": account_id,
        "captured_at": captured,
        "evaluated_at": evaluated,
        "expires_at": expires,
        "raw_sha256": raw_sha,
        "config_sha256": config_sha,
        "reason": "",
        "proposal": None,
        "results": [
            {"module": module, "status": "not_run", "reason": "not reached"}
            for module, _ in MODULE_ORDER
        ],
        "executable": False,
        "fill_eligible": False,
        "approved": False,
        "risk_ok": False,
    }


def _run_fixture(
    record: dict[str, Any],
    proposal: dict[str, Any],
    marks: dict[str, Decimal],
    positions: list[dict[str, Any]],
    pending: list[dict[str, Any]],
    context: dict[str, Any],
    server_time: datetime,
    config_raw: bytes,
) -> tuple[str, str, list[dict[str, Any]]]:
    from execution.validators import runner
    from execution.validators.types import Order, Position, RiskContext

    config = _yaml_config(config_raw)
    order = Order(
        ticker=proposal["ticker"],
        side=proposal["side"],
        qty=proposal["qty"],
        limit_price=proposal["limit_price"],
        stop_loss=proposal["stop_loss"],
        strategy=proposal["strategy"],
        submitted_at=proposal["submitted_at"],
        extended_hours=False,
        order_type="LMT",
    )
    ctx = RiskContext(
        account_value=parse_money(context["account_value"], "context.account_value", positive=True),
        cash=parse_money(context["cash"], "context.cash"),
        positions=[
            Position(
                ticker=item["ticker"],
                qty=item["qty"],
                avg_price=item["avg_price"],
                stop_loss=item["stop_loss"],
            )
            for item in positions
        ],
        pending_orders=[
            Order(
                ticker=item["ticker"],
                side=item["side"],
                qty=item["qty"],
                limit_price=item["limit_price"],
                stop_loss=item["stop_loss"],
                strategy=item["strategy"],
                submitted_at=item["submitted_at"],
                extended_hours=False,
                order_type="LMT",
            )
            for item in pending
        ],
        now=server_time,
        current_marks=dict(marks),
    )
    passed, results = runner.run_all(order, ctx, config)
    by_rule = {res.rule: res for res in results}
    rows: list[dict[str, Any]] = []
    stopped = False
    for module, rule in MODULE_ORDER:
        if stopped:
            rows.append({"module": module, "status": "not_run", "reason": "not reached"})
            continue
        res = by_rule.get(rule)
        if res is None:
            stopped = True
            rows.append({"module": module, "status": "not_run", "reason": "not reached"})
            continue
        rows.append(
            {
                "module": module,
                "status": "approved" if res.approved else "rejected",
                "reason": res.reason,
            }
        )
        if not res.approved:
            stopped = True
    if passed and all(row["status"] == "approved" for row in rows):
        return "fixture_pass", "fixture: all validators approved (proposal only)", rows
    first = next((row for row in rows if row["status"] == "rejected"), rows[-1])
    return "fixture_rejected", f"fixture rejected by {first['module']}", rows


def _yaml_config(config_raw: bytes) -> dict[str, Any]:
    import yaml

    try:
        loaded = yaml.safe_load(config_raw.decode("utf-8"))
    except (UnicodeError, yaml.YAMLError, RecursionError) as exc:
        raise ValueError("config: invalid YAML") from exc
    if not isinstance(loaded, dict):
        raise ValueError("config: expected mapping")
    return loaded


def evaluate_risk(raw: bytes | None, *, as_of: str, config_raw: bytes) -> dict[str, Any]:
    """Evaluate an offline risk context. Never approves a real trade."""
    if type(config_raw) is not bytes or not config_raw:
        raise ValueError("config_raw must be non-empty bytes")
    if len(config_raw) > MAX_BYTES:
        raise ValueError("config_raw exceeds 1 MiB")
    try:
        config_text = config_raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ValueError("config_raw must be valid UTF-8") from exc
    del config_text
    config_sha = hashlib.sha256(config_raw).hexdigest()
    if not isinstance(as_of, str):
        raise ValueError("as_of must be a string")
    as_of_dt = parse_timestamp(as_of, "as_of")
    evaluated = as_of_dt.isoformat()

    result = _base_result(None, None, None, evaluated, None, _hash(raw), config_sha)

    if raw is None:
        result["reason"] = "no offline risk context supplied"
        return result
    if type(raw) is not bytes:
        raise ValueError("raw must be bytes")

    record = load_json(raw)

    _require_exact_keys(
        record,
        (
            "schema",
            "kind",
            "origin",
            "account_id",
            "currency",
            "captured_at",
            "expires_at",
            "proposal",
            "context",
            "coverage",
        ),
        "root",
    )
    origin = record["origin"]
    if origin not in ("recorded", "fixture"):
        raise ValueError("root.origin: must be 'recorded' or 'fixture'")
    result["origin"] = origin

    if type(record["schema"]) is not int or record["schema"] != SCHEMA_VERSION:
        raise ValueError("root.schema: unsupported schema version")
    if record["kind"] != "offline-risk-context":
        raise ValueError("root.kind: unexpected document kind")
    if record["account_id"] != ACCOUNT_ID:
        raise ValueError("root.account_id: unexpected account")
    result["account_id"] = record["account_id"]
    if record["currency"] != CURRENCY:
        raise ValueError("root.currency: only USD is permitted")

    captured = parse_timestamp(record["captured_at"], "root.captured_at")
    expires = parse_timestamp(record["expires_at"], "root.expires_at")
    result["captured_at"] = captured.isoformat()
    result["expires_at"] = expires.isoformat()
    if expires <= captured:
        raise ValueError("root: expires_at must be strictly after captured_at")
    if expires - captured > MAX_WINDOW:
        raise ValueError("root: expiry window exceeds 15 minutes")
    if captured > as_of_dt:
        raise ValueError("root: captured_at is in the future relative to as_of")

    proposal = _parse_order(record["proposal"], "proposal")
    if proposal["submitted_at"] > captured:
        raise ValueError("proposal.submitted_at: must not postdate captured_at")

    context = _require_mapping(record["context"], "context")
    _require_exact_keys(
        context,
        (
            "account_value",
            "cash",
            "positions",
            "pending_orders",
            "current_marks",
            "server_time",
        ),
        "context",
    )
    parse_money(context["account_value"], "context.account_value", positive=True)
    parse_money(context["cash"], "context.cash")
    positions = [
        _parse_position(item, f"context.positions[{index}]")
        for index, item in enumerate(_require_list(context["positions"], "context.positions"))
    ]
    pending = [
        _parse_order(item, f"context.pending_orders[{index}]")
        for index, item in enumerate(
            _require_list(context["pending_orders"], "context.pending_orders")
        )
    ]
    for index, order in enumerate(pending):
        if order["submitted_at"] > captured:
            raise ValueError(
                f"context.pending_orders[{index}].submitted_at: must not postdate captured_at"
            )
    marks = _parse_marks(context["current_marks"])
    server_time = parse_timestamp(context["server_time"], "context.server_time")
    if server_time > captured:
        raise ValueError("context.server_time: server clock ahead of capture")
    if captured - server_time > CLOCK_SKEW:
        raise ValueError("context.server_time: server clock behind capture by >15s")

    coverage = _require_mapping(record["coverage"], "coverage")
    _require_exact_keys(coverage, COVERAGE_KEYS, "coverage")
    for key in COVERAGE_KEYS:
        _require_bool(coverage[key], f"coverage.{key}")

    if captured <= as_of_dt < expires:
        pass
    else:
        result["reason"] = "context is stale relative to as_of"
        return result

    absent = [key for key in COVERAGE_KEYS if coverage[key] is not True]
    if absent:
        result["reason"] = f"coverage incomplete: {absent}"
        return result

    missing_stop = _check_stops(proposal, positions, pending)
    if missing_stop is not None:
        result["reason"] = missing_stop
        return result

    missing_mark = _require_marks(marks, proposal, positions, pending)
    if missing_mark is not None:
        result["reason"] = missing_mark
        return result

    normalized = {
        "ticker": proposal["ticker"],
        "side": proposal["side"],
        "qty": proposal["qty"],
        "limit_price": format(proposal["limit_price"], "f"),
        "stop_loss": None
        if proposal["stop_loss"] is None
        else format(proposal["stop_loss"], "f"),
        "strategy": proposal["strategy"],
        "submitted_at": proposal["submitted_at"].isoformat(),
    }

    if origin == "recorded":
        result["reason"] = (
            "recorded origin is never authenticated offline: trusted funding, "
            "current-quote, approved-rules, and journal evidence is absent"
        )
        return result

    status, reason, rows = _run_fixture(
        record, proposal, marks, positions, pending, context, server_time, config_raw
    )

    result["status"] = status
    result["reason"] = reason
    result["proposal"] = normalized
    result["results"] = rows
    result["risk_ok"] = status == "fixture_pass"
    return result
