"""Pure offline parser for recorded IBKR paper account snapshots."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from scripts.stage2_eod import inputs as _inputs

EXPECTED_ACCOUNT = "DUQ220152"
SCHEMA = 1
KIND = "ibkr-paper-account-snapshot"
SOURCE = "IBKR paper Gateway"
CONNECTION_MODE = "readonly"
GATEWAY_PORT = 4002
MAX_RAW_BYTES = 1024 * 1024
MAX_POSITIONS = 1000
FRESHNESS_SECONDS = 15 * 60
ORIGINS = ("recorded", "fixture")
SEC_TYPES = ("STK", "ETF", "OPT", "FUT", "CASH", "BOND")
ROOT_KEYS = ("schema", "kind", "origin", "source", "connection_mode",
             "gateway_port", "requested_account_id", "account_id",
             "started_at", "completed_at", "status", "reason",
             "positions", "account_values")
POSITION_KEYS = ("account_id", "contract_id", "symbol", "sec_type",
                 "currency", "exchange", "quantity", "average_cost")
ACCOUNT_VALUE_KEYS = ("TotalCashValue", "NetLiquidation", "AvailableFunds")

_SYMBOL_RE = re.compile(r"[A-Z0-9 .\-]{1,40}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_BOUNDED_RE = re.compile(r"-?[0-9]{1,15}(?:\.[0-9]{1,8})?")
_ABS_BOUND = Decimal("1e12")


def _check_no_duplicate_keys(raw):
    def hook(pairs):
        seen = set()
        for key, _v in pairs:
            if key in seen:
                raise ValueError("duplicate JSON key: " + str(key))
            seen.add(key)
        return dict(pairs)
    json.loads(raw.decode("utf-8"), object_pairs_hook=hook)


def _decode_raw(raw):
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("raw snapshot must be bytes")
    data = bytes(raw)
    if not data:
        raise ValueError("raw snapshot is empty")
    if len(data) > MAX_RAW_BYTES:
        raise ValueError("raw snapshot exceeds the 1 MiB cap")
    text = data.decode("utf-8")
    return json.loads(text)


def _ts(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(label + " must be a non empty offset timestamp")
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(label + " is not a valid ISO timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(label + " must carry a UTC offset")
    return parsed.astimezone(timezone.utc)


def _int(value, label):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(label + " must be an integer")
    return value


def _text(value, label):
    if not isinstance(value, str) or not value:
        raise ValueError(label + " must be non empty text")
    return value


def _decimal(value, label, nonnegative=False, allow_zero=True):
    if not isinstance(value, str):
        raise ValueError(label + " must be a decimal string")
    if not _BOUNDED_RE.fullmatch(value):
        raise ValueError(label + " is not a bounded decimal string")
    parsed = Decimal(value)
    if not parsed.is_finite():
        raise ValueError(label + " must be finite")
    if abs(parsed) > _ABS_BOUND:
        raise ValueError(label + " exceeds the absolute bound")
    if nonnegative and parsed < 0:
        raise ValueError(label + " must be nonnegative")
    if not allow_zero and parsed == 0:
        raise ValueError(label + " must not be zero")
    return str(parsed)


def _mapping(value, label):
    if not isinstance(value, dict):
        raise ValueError(label + " must be an object")
    return value


def _exact_keys(obj, allowed, label):
    allowed_set = set(allowed)
    actual = set(obj.keys())
    extra = actual - allowed_set
    missing = allowed_set - actual
    if extra:
        raise ValueError(label + " has unknown keys: " + repr(sorted(extra)))
    if missing:
        raise ValueError(label + " is missing keys: " + repr(sorted(missing)))


def _position(entry, expected, index):
    label = "positions[%d]" % index
    obj = _mapping(entry, label)
    _exact_keys(obj, POSITION_KEYS, label)
    if _text(obj["account_id"], label + ".account_id") != expected:
        raise ValueError(label + ".account_id does not match expected")
    cid = _int(obj["contract_id"], label + ".contract_id")
    if cid <= 0:
        raise ValueError(label + ".contract_id must be positive")
    symbol = _text(obj["symbol"], label + ".symbol")
    if not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError(label + ".symbol is not a valid code")
    sec = _text(obj["sec_type"], label + ".sec_type")
    if sec not in SEC_TYPES:
        raise ValueError(label + ".sec_type is not supported")
    currency = _text(obj["currency"], label + ".currency")
    if not _CURRENCY_RE.fullmatch(currency):
        raise ValueError(label + ".currency must be three uppercase letters")
    exchange = _text(obj["exchange"], label + ".exchange")
    quantity = _decimal(obj["quantity"], label + ".quantity", allow_zero=False)
    average = _decimal(obj["average_cost"], label + ".average_cost", nonnegative=True)
    return {"account_id": expected, "contract_id": cid, "symbol": symbol,
            "sec_type": sec, "currency": currency, "exchange": exchange,
            "quantity": quantity, "average_cost": average}


def _account_value(value, label):
    if value is None:
        return None
    obj = _mapping(value, label)
    _exact_keys(obj, ("value", "currency"), label)
    decimal_value = _decimal(obj["value"], label + ".value")
    currency = _text(obj["currency"], label + ".currency")
    if currency != "BASE" and not _CURRENCY_RE.fullmatch(currency):
        raise ValueError(label + ".currency must be BASE or three uppercase letters")
    return {"value": decimal_value, "currency": currency}


def parse_snapshot(raw, *, as_of, expected_account=EXPECTED_ACCOUNT):
    if not isinstance(expected_account, str) or expected_account != EXPECTED_ACCOUNT:
        raise ValueError("expected_account must be the literal DUQ220152")
    if not isinstance(as_of, str) or not as_of:
        raise ValueError("as_of must be an offset timestamp")
    as_of_dt = _ts(as_of, "as_of")
    _check_no_duplicate_keys(raw)
    root = _mapping(_decode_raw(raw), "snapshot")
    _exact_keys(root, ROOT_KEYS, "snapshot")
    if _int(root["schema"], "schema") != SCHEMA:
        raise ValueError("schema must be 1")
    if _text(root["kind"], "kind") != KIND:
        raise ValueError("kind is not supported")
    origin = _text(root["origin"], "origin")
    if origin not in ORIGINS:
        raise ValueError("origin is not supported")
    if _text(root["source"], "source") != SOURCE:
        raise ValueError("source is not supported")
    if _text(root["connection_mode"], "connection_mode") != CONNECTION_MODE:
        raise ValueError("connection_mode must be readonly")
    if _int(root["gateway_port"], "gateway_port") != GATEWAY_PORT:
        raise ValueError("gateway_port must be 4002")
    if _text(root["requested_account_id"], "requested_account_id") != EXPECTED_ACCOUNT:
        raise ValueError("requested_account_id must match paper account")
    account_id = root["account_id"]
    if account_id is not None:
        if not isinstance(account_id, str) or account_id != EXPECTED_ACCOUNT:
            raise ValueError("account_id must be null or the expected account")
    started = _ts(root["started_at"], "started_at")
    completed = _ts(root["completed_at"], "completed_at")
    if started > completed:
        raise ValueError("started_at must not follow completed_at")
    if completed > as_of_dt:
        raise ValueError("completed_at must not be in the future")
    status = _text(root["status"], "status")
    if status not in ("complete", "unavailable"):
        raise ValueError("status is not supported")
    reason = root["reason"]
    if not isinstance(reason, str):
        raise ValueError("reason must be a string")
    raw_positions = root["positions"]
    raw_values = root["account_values"]
    positions = None
    account_values = None
    if status == "complete":
        if account_id != EXPECTED_ACCOUNT:
            raise ValueError("complete snapshots need a matching account_id")
        if raw_positions is not None:
            if not isinstance(raw_positions, list):
                raise ValueError("positions must be a list or null")
            if len(raw_positions) > MAX_POSITIONS:
                raise ValueError("positions exceed the 1000 item cap")
            seen = set()
            parsed_positions = []
            for index, entry in enumerate(raw_positions):
                parsed_position = _position(entry, EXPECTED_ACCOUNT, index)
                cid = parsed_position["contract_id"]
                if cid in seen:
                    raise ValueError("duplicate contract_id in positions")
                seen.add(cid)
                parsed_positions.append(parsed_position)
            positions = parsed_positions
        if raw_values is not None:
            values = _mapping(raw_values, "account_values")
            _exact_keys(values, ACCOUNT_VALUE_KEYS, "account_values")
            account_values = {k: _account_value(values[k], "account_values." + k)
                              for k in ACCOUNT_VALUE_KEYS}
    else:
        if account_id is not None or raw_positions is not None or raw_values is not None:
            raise ValueError("unavailable snapshots require null identity and sections")
        if not reason.strip():
            raise ValueError("unavailable snapshots require a non empty reason")
    if status == "complete":
        freshness = "fresh" if (as_of_dt - completed) <= timedelta(seconds=FRESHNESS_SECONDS) else "stale"
    else:
        freshness = "unavailable"
    return {"schema": SCHEMA, "kind": KIND, "origin": origin, "source": SOURCE,
            "connection_mode": CONNECTION_MODE, "gateway_port": GATEWAY_PORT,
            "requested_account_id": EXPECTED_ACCOUNT, "account_id": account_id,
            "started_at": root["started_at"], "completed_at": root["completed_at"],
            "status": status, "reason": reason, "positions": positions,
            "account_values": account_values,
            "raw_sha256": hashlib.sha256(bytes(raw)).hexdigest(),
            "evaluated_at": as_of_dt.isoformat(), "freshness": freshness}
