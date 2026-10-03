"""Strict offline parser for IBKR paper account readiness snapshots.

This module contains no broker connectivity and imports no execution, risk,
submission, or engine components.  It parses a single capture produced by the
sibling ``account_capture`` module (or an equivalent saved artefact) and makes
the resulting account/order observations explicit, including all of the
permissions and coverage facts that the snapshot does *not* prove.
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path

from scripts.stage3_paper.snapshot import _ABS_BOUND, _decimal, _ts
_CANONICAL_SEPARATORS = (",", ":")

CAPTURE_SOURCE_PATH = Path(__file__).with_name("account_capture.py")

SCHEMA = 1
KIND = "ibkr-paper-account-readiness"
PROOF_KIND = "ibkr-account-readiness-acquisition-proof"
SOURCE = "IBKR paper Gateway"
CONNECTION_MODE = "readonly"
GATEWAY_PORT = 4002
REQUESTED_ACCOUNT = "DUQ220152"

_MAX_RAW_BYTES = 1024 * 1024
_MAX_ACCOUNT_VALUES = 100
_MAX_ORDER_ROWS = 1000
_MAX_REASON = 500
_MAX_INT32 = 2147483647
_MAX_CAPTURE_SECONDS = 90
_STALE_SECONDS = 900

_ROOT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "origin",
        "source",
        "connection_mode",
        "gateway_port",
        "requested_account_id",
        "account_id",
        "started_at",
        "completed_at",
        "status",
        "reason",
        "request",
        "account",
        "orders",
        "restrictions",
    }
)
_REQUEST_KEYS = frozenset(
    {
        "account_updates",
        "open_orders",
        "startup_fetch",
        "timeout_seconds",
        "client_id",
        "bind_orders",
        "market_data",
    }
)
_ACCOUNT_KEYS = frozenset(
    {"requested_at", "ended_at", "end_received", "account_ready", "values"}
)
_ROW_KEYS = frozenset({"tag", "currency", "value"})
_ORDERS_KEYS = frozenset(
    {
        "requested_at",
        "ended_at",
        "end_received",
        "rows",
        "unattributed_count",
        "other_account_count",
    }
)
_ORDER_ROW_KEYS = frozenset(
    {
        "account_id",
        "perm_id",
        "client_id",
        "order_id",
        "symbol",
        "sec_type",
        "currency",
        "side",
        "order_type",
        "quantity",
        "limit_price",
        "status",
    }
)
_RESTRICTION_KEYS = frozenset({"state", "reason"})
_PROOF_KEYS = frozenset(
    {
        "schema",
        "kind",
        "raw_sha256",
        "source_sha256",
        "request_sha256",
        "returncode",
        "captured_at",
    }
)

_CASH_TAGS = frozenset({"CashBalance", "TotalCashBalance", "SettledCash"})
_LEDGER_TAGS = frozenset(
    {
        "$LEDGER-CashBalance",
        "$LEDGER-TotalCashBalance",
        "$LEDGER-SettledCash",
    }
)
_TAGS = _CASH_TAGS | _LEDGER_TAGS

_SEC_TYPES = frozenset({"STK", "ETF", "OPT", "FUT", "CASH", "BOND"})
_SIDES = frozenset({"BUY", "SELL"})

_LOWER_HEX = frozenset("0123456789abcdef")
_ASCII_ALNUM = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
)
_ASCII_UPPER = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
_SYMBOL_EXTRA = frozenset(" .-")
_ALNUM_SPACE = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 "
)


class _Reject(json.JSONDecodeError):
    """Internal marker used to convert JSON structural problems to ValueError."""


def _fail(label, detail):
    raise ValueError(label + ": " + detail)


def _loads(raw, label):
    if not isinstance(raw, (bytes, bytearray)):
        _fail(label, "must be bytes")
    data = bytes(raw)
    if len(data) > _MAX_RAW_BYTES:
        _fail(label, "exceeds the maximum capture size")
    if not data:
        _fail(label, "is empty")
    if data.startswith(b"\xef\xbb\xbf"):
        _fail(label, "must not carry a byte order mark")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail(label, "must be valid UTF-8")
    try:
        parsed = json.loads(text, parse_constant=_reject_constant)
    except json.JSONDecodeError as error:
        _fail(label, "is not well formed JSON (" + error.msg + ")")
    if not isinstance(parsed, dict):
        _fail(label, "must decode to a JSON object")
    return data, parsed


def _reject_constant(token):
    raise ValueError("non finite JSON constant is not allowed: " + str(token))


def _pairs_no_duplicates(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError("duplicate JSON key is not allowed: " + str(key))
        seen.add(key)
    return dict(pairs)


def _object_hook(pairs):
    return _pairs_no_duplicates(pairs)


def _loads_strict(raw, label):
    data = bytes(raw) if isinstance(raw, (bytes, bytearray)) else None
    if data is None:
        _fail(label, "must be bytes")
    if len(data) > _MAX_RAW_BYTES:
        _fail(label, "exceeds the maximum capture size")
    if not data:
        _fail(label, "is empty")
    try:
        text = data.decode("utf-8", "strict")
    except UnicodeDecodeError:
        _fail(label, "must be valid UTF-8")
    try:
        parsed = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_object_hook,
        )
    except (ValueError, RecursionError) as error:
        message = str(error)
        if "duplicate JSON key" in message or "non finite JSON constant" in message:
            _fail(label, message)
        _fail(label, "is not well formed JSON (" + message + ")")
    if not isinstance(parsed, dict):
        _fail(label, "must decode to a JSON object")
    return data, parsed


def _exact_keys(obj, keys, label):
    if not isinstance(obj, dict):
        _fail(label, "must be an object")
    actual = frozenset(obj.keys())
    if actual != keys:
        missing = sorted(keys - actual)
        extra = sorted(actual - keys)
        parts = []
        if missing:
            parts.append("missing " + ",".join(missing))
        if extra:
            parts.append("unexpected " + ",".join(extra))
        _fail(label, "has " + " and ".join(parts))


def _int_field(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(label, "must be an integer")
    if value < minimum or value > maximum:
        _fail(label, "is outside the permitted range")
    return value


def _bool_field(value, label):
    if not isinstance(value, bool):
        _fail(label, "must be a boolean")
    return value


def _plain_string(value, label, maximum, allow_empty=True):
    if not isinstance(value, str):
        _fail(label, "must be a string")
    if not allow_empty and not value:
        _fail(label, "must not be empty")
    if len(value) > maximum:
        _fail(label, "exceeds the maximum length")
    return value


def _ascii_string(value, label, maximum, alphabet, allow_empty=True):
    text = _plain_string(value, label, maximum, allow_empty=allow_empty)
    for char in text:
        if char not in alphabet:
            _fail(label, "contains a character outside the permitted alphabet")
    return text


def _sha256(value):
    return hashlib.sha256(value).hexdigest()


def _sha_field(value, label):
    if not isinstance(value, str):
        _fail(label, "must be a lowercase hex sha256 string")
    if len(value) != 64:
        _fail(label, "must be 64 characters")
    for char in value:
        if char not in _LOWER_HEX:
            _fail(label, "must be lowercase hexadecimal")
    return value


def _canonical_bytes(value):
    return json.dumps(
        value, sort_keys=True, separators=_CANONICAL_SEPARATORS
    ).encode("utf-8")


def _now_seconds(a, b):
    return (b - a).total_seconds()


def _parse_request(obj):
    _exact_keys(obj, _REQUEST_KEYS, "request")
    request = {
        "account_updates": _plain_string(
            obj["account_updates"], "request.account_updates", 64, allow_empty=False
        ),
        "open_orders": _plain_string(
            obj["open_orders"], "request.open_orders", 64, allow_empty=False
        ),
        "startup_fetch": _int_field(
            obj["startup_fetch"], "request.startup_fetch", 0, 0
        ),
        "timeout_seconds": _int_field(
            obj["timeout_seconds"], "request.timeout_seconds", 15, 15
        ),
        "client_id": obj["client_id"],
        "bind_orders": _bool_field(obj["bind_orders"], "request.bind_orders"),
        "market_data": _bool_field(obj["market_data"], "request.market_data"),
    }
    if request["account_updates"] != "reqAccountUpdates":
        _fail("request.account_updates", "must be reqAccountUpdates")
    if request["open_orders"] != "reqAllOpenOrders":
        _fail("request.open_orders", "must be reqAllOpenOrders")
    if request["bind_orders"] is not False:
        _fail("request.bind_orders", "must be false")
    if request["market_data"] is not False:
        _fail("request.market_data", "must be false")
    client_id = obj["client_id"]
    if client_id is not None:
        request["client_id"] = _int_field(client_id, "request.client_id", 90, 99)
    return request


def _parse_account_values(values):
    if not isinstance(values, list):
        _fail("account.values", "must be a list")
    if len(values) > _MAX_ACCOUNT_VALUES:
        _fail("account.values", "exceeds the maximum row count")
    rows = []
    seen = set()
    for index, raw_row in enumerate(values):
        label = "account.values[%d]" % index
        _exact_keys(raw_row, _ROW_KEYS, label)
        tag = _plain_string(raw_row["tag"], label + ".tag", 32, allow_empty=False)
        if tag not in _TAGS:
            _fail(label + ".tag", "is not a recognised cash or ledger tag")
        currency = _plain_string(
            raw_row["currency"], label + ".currency", 8, allow_empty=True
        )
        if currency not in ("", "BASE"):
            if len(currency) != 3:
                _fail(label + ".currency", "must be empty, BASE, or a three letter code")
            for char in currency:
                if char not in _ASCII_UPPER:
                    _fail(label + ".currency", "must be uppercase")
        value = _decimal(raw_row["value"], label + ".value")
        key = (tag, currency)
        if key in seen:
            _fail(label, "duplicates an earlier (tag, currency) observation")
        seen.add(key)
        rows.append({"tag": tag, "currency": currency, "value": value})
    return rows


def _parse_account(obj, status, root_completed, first_requested):
    if obj is None:
        if status != "unavailable":
            _fail("account", "may be null only when the status is unavailable")
        return None
    _exact_keys(obj, _ACCOUNT_KEYS, "account")
    requested_at = _ts(obj["requested_at"], "account.requested_at")
    end_received = _bool_field(obj["end_received"], "account.end_received")
    ended_at_value = obj["ended_at"]
    if end_received:
        if ended_at_value is None:
            _fail("account.ended_at", "is required when end_received is true")
        ended_at = _ts(ended_at_value, "account.ended_at")
    else:
        if ended_at_value is not None:
            _fail("account.ended_at", "must be null when end_received is false")
        ended_at = None
    values = obj["values"]
    account_ready = obj["account_ready"]
    if not end_received:
        if values is not None:
            _fail("account.values", "must be null when end_received is false")
        if account_ready is not None:
            _fail("account.account_ready", "must be null when end_received is false")
        parsed_values = None
    else:
        if values is None:
            _fail("account.values", "must be a list when end_received is true")
        parsed_values = _parse_account_values(values)
        if account_ready is not None and not isinstance(account_ready, bool):
            _fail("account.account_ready", "must be a boolean or null")
    if ended_at is not None:
        if ended_at < requested_at:
            _fail("account.ended_at", "precedes account.requested_at")
        if ended_at > root_completed:
            _fail("account.ended_at", "follows the root completed_at")
    if requested_at > root_completed:
        _fail("account.requested_at", "follows the root completed_at")
    if requested_at < first_requested:
        _fail("account.requested_at", "precedes the root started_at")
    return {
        "requested_at": obj["requested_at"],
        "ended_at": obj["ended_at"],
        "end_received": end_received,
        "account_ready": account_ready,
        "values": parsed_values,
    }


def _parse_symbol(value, label):
    text = _plain_string(value, label, 40, allow_empty=False)
    for char in text:
        if char not in _ASCII_ALNUM and char not in _SYMBOL_EXTRA:
            _fail(label, "contains an unsupported symbol character")
    return text


def _parse_currency_code(value, label):
    if not isinstance(value, str) or len(value) != 3:
        _fail(label, "must be a three letter uppercase code")
    for char in value:
        if char not in _ASCII_UPPER:
            _fail(label, "must be uppercase")
    return value


def _parse_order_rows(rows):
    if not isinstance(rows, list):
        _fail("orders.rows", "must be a list")
    if len(rows) > _MAX_ORDER_ROWS:
        _fail("orders.rows", "exceeds the maximum row count")
    parsed = []
    identities = set()
    pairs = set()
    for index, raw in enumerate(rows):
        label = "orders.rows[%d]" % index
        _exact_keys(raw, _ORDER_ROW_KEYS, label)
        account_id = _plain_string(
            raw["account_id"], label + ".account_id", 32, allow_empty=False
        )
        if account_id != REQUESTED_ACCOUNT:
            _fail(label + ".account_id", "does not match the requested account")
        perm_id = _int_field(raw["perm_id"], label + ".perm_id", 0, _MAX_INT32)
        client_id = _int_field(raw["client_id"], label + ".client_id", 0, _MAX_INT32)
        order_id = _int_field(raw["order_id"], label + ".order_id", 0, _MAX_INT32)
        symbol = _parse_symbol(raw["symbol"], label + ".symbol")
        sec_type = raw["sec_type"]
        if not isinstance(sec_type, str) or sec_type not in _SEC_TYPES:
            _fail(label + ".sec_type", "is not a supported security type")
        currency = _parse_currency_code(raw["currency"], label + ".currency")
        side = raw["side"]
        if not isinstance(side, str) or side not in _SIDES:
            _fail(label + ".side", "must be BUY or SELL")
        order_type = _ascii_string(
            raw["order_type"], label + ".order_type", 32, _ALNUM_SPACE, allow_empty=False
        )
        status = _ascii_string(
            raw["status"], label + ".status", 32, _ALNUM_SPACE, allow_empty=False
        )
        quantity = _decimal(
            raw["quantity"], label + ".quantity", nonnegative=True
        )
        limit_raw = raw["limit_price"]
        if limit_raw is None:
            limit_price = None
        else:
            limit_price = _decimal(
                limit_raw, label + ".limit_price", allow_zero=False
            )
            if limit_price.startswith("-"):
                _fail(label + ".limit_price", "must be positive")
        if perm_id > 0:
            identity = ("perm", perm_id)
        else:
            identity = ("pair", client_id, order_id)
        if identity in identities:
            _fail(label, "duplicates an earlier stable order identity")
        identities.add(identity)
        pair = (client_id, order_id)
        if pair in pairs:
            _fail(label, "duplicates an earlier (client_id, order_id) pair")
        pairs.add(pair)
        parsed.append(
            {
                "account_id": account_id,
                "perm_id": perm_id,
                "client_id": client_id,
                "order_id": order_id,
                "symbol": symbol,
                "sec_type": sec_type,
                "currency": currency,
                "side": side,
                "order_type": order_type,
                "quantity": quantity,
                "limit_price": limit_price,
                "status": status,
            }
        )
    return parsed


def _parse_orders(obj, status, root_completed, first_requested):
    if obj is None:
        if status != "unavailable":
            _fail("orders", "may be null only when the status is unavailable")
        return None
    _exact_keys(obj, _ORDERS_KEYS, "orders")
    requested_at = _ts(obj["requested_at"], "orders.requested_at")
    end_received = _bool_field(obj["end_received"], "orders.end_received")
    ended_at_value = obj["ended_at"]
    if end_received:
        if ended_at_value is None:
            _fail("orders.ended_at", "is required when end_received is true")
        ended_at = _ts(ended_at_value, "orders.ended_at")
    else:
        if ended_at_value is not None:
            _fail("orders.ended_at", "must be null when end_received is false")
        ended_at = None
    unattributed = _int_field(
        obj["unattributed_count"], "orders.unattributed_count", 0, _MAX_ORDER_ROWS
    )
    other_account = _int_field(
        obj["other_account_count"], "orders.other_account_count", 0, _MAX_ORDER_ROWS
    )
    rows = obj["rows"]
    if not end_received:
        if rows is not None:
            _fail("orders.rows", "must be null when end_received is false")
        if unattributed != 0 or other_account != 0:
            _fail("orders", "counts must be zero before the order end callback")
        parsed_rows = None
    else:
        if rows is None:
            _fail("orders.rows", "must be a list when end_received is true")
        parsed_rows = _parse_order_rows(rows)
    if requested_at > root_completed:
        _fail("orders.requested_at", "follows the root completed_at")
    if requested_at < first_requested:
        _fail("orders.requested_at", "precedes the root started_at")
    if ended_at is not None:
        if ended_at < requested_at:
            _fail("orders.ended_at", "precedes orders.requested_at")
        if ended_at > root_completed:
            _fail("orders.ended_at", "follows the root completed_at")
    return {
        "requested_at": obj["requested_at"],
        "ended_at": obj["ended_at"],
        "end_received": end_received,
        "rows": parsed_rows,
        "unattributed_count": unattributed,
        "other_account_count": other_account,
    }


def _parse_restrictions(obj):
    _exact_keys(obj, _RESTRICTION_KEYS, "restrictions")
    state = obj["state"]
    if state != "unknown":
        _fail("restrictions.state", "must be unknown")
    reason = _plain_string(
        obj["reason"], "restrictions.reason", _MAX_REASON, allow_empty=False
    )
    return {"state": "unknown", "reason": reason}


def _parse_proof(raw):
    data, parsed = _loads_strict(raw, "proof")
    _exact_keys(parsed, _PROOF_KEYS, "proof")
    _int_field(parsed["schema"], "proof.schema", 1, 1)
    if parsed["kind"] != PROOF_KIND:
        _fail("proof.kind", "is not the readiness acquisition proof kind")
    raw_sha = _sha_field(parsed["raw_sha256"], "proof.raw_sha256")
    source_sha = _sha_field(parsed["source_sha256"], "proof.source_sha256")
    request_sha = _sha_field(parsed["request_sha256"], "proof.request_sha256")
    _int_field(parsed["returncode"], "proof.returncode", 0, 0)
    _ts(parsed["captured_at"], "proof.captured_at")
    return {
        "raw_sha256": raw_sha,
        "source_sha256": source_sha,
        "request_sha256": request_sha,
        "returncode": 0,
        "captured_at": parsed["captured_at"],
        "_bytes": data,
    }


def _read_source_hash():
    try:
        source_bytes = CAPTURE_SOURCE_PATH.read_bytes()
    except OSError:
        return None
    return _sha256(source_bytes)


def _verify_proof(proof, raw_data, request_json, completed_at):
    if proof["raw_sha256"] != _sha256(raw_data):
        _fail("proof.raw_sha256", "does not match the captured bytes")
    if proof["request_sha256"] != _sha256(_canonical_bytes(request_json)):
        _fail("proof.request_sha256", "does not match the canonical request")
    if proof["captured_at"] != completed_at:
        _fail("proof.captured_at", "does not equal the capture completed_at")
    source_hash = _read_source_hash()
    if source_hash is None:
        _fail("proof.source_sha256", "could not read the sibling capture source")
    if proof["source_sha256"] != source_hash:
        _fail("proof.source_sha256", "does not match the sibling capture source")
    return source_hash


def _classify_orders(orders, status, freshness, proof_verified, account_ready):
    if status == "unavailable" or orders is None:
        return "unknown", None
    rows = orders["rows"]
    if rows is None:
        return "unknown", None
    covered = (
        proof_verified
        and account_ready is True
        and freshness == "fresh"
        and orders["end_received"]
        and orders["unattributed_count"] == 0
    )
    if covered and not rows:
        return "no_visible_api_orders", 0
    return ("visible_api_orders" if rows else "no_visible_api_orders") if covered else "unknown", len(rows)


def parse_account_readiness(raw, *, as_of, proof_raw=None):
    """Parse one IBKR paper account readiness snapshot strictly.

    All malformed input raises :class:`ValueError`.  The returned mapping
    retains the frozen schema fields plus the display-only derived fields.
    """

    if not isinstance(as_of, str):
        _fail("as_of", "must be an offset timestamp string")
    as_of_dt = _ts(as_of, "as_of")

    if proof_raw is not None:
        if not isinstance(proof_raw, (bytes, bytearray)):
            _fail("proof_raw", "must be bytes")
        proof_raw = bytes(proof_raw)

    data, root = _loads_strict(raw, "capture")
    _exact_keys(root, _ROOT_KEYS, "capture")

    _int_field(root["schema"], "schema", 1, 1)
    if root["kind"] != KIND:
        _fail("kind", "is not the readiness snapshot kind")

    origin = root["origin"]
    if origin not in ("recorded", "fixture"):
        _fail("origin", "must be recorded or fixture")
    if root["source"] != SOURCE:
        _fail("source", "must be the IBKR paper Gateway")
    if root["connection_mode"] != CONNECTION_MODE:
        _fail("connection_mode", "must be readonly")
    _int_field(root["gateway_port"], "gateway_port", GATEWAY_PORT, GATEWAY_PORT)
    if root["requested_account_id"] != REQUESTED_ACCOUNT:
        _fail("requested_account_id", "does not match the frozen account")

    status = root["status"]
    if status not in ("complete", "partial", "unavailable"):
        _fail("status", "must be complete, partial, or unavailable")

    reason = _plain_string(root["reason"], "reason", _MAX_REASON, allow_empty=True)
    if status == "complete":
        if reason != "":
            _fail("reason", "must be empty when the status is complete")
    else:
        if reason == "":
            _fail("reason", "must be a non empty description for this status")

    account_id = root["account_id"]
    if status == "unavailable":
        if account_id is not None:
            _fail("account_id", "must be null when the status is unavailable")
    else:
        if account_id != REQUESTED_ACCOUNT:
            _fail("account_id", "must be the requested account for this status")

    started_at = _ts(root["started_at"], "started_at")
    completed_at = _ts(root["completed_at"], "completed_at")
    if started_at > completed_at:
        _fail("started_at", "must not follow completed_at")
    if completed_at > as_of_dt:
        _fail("completed_at", "must not follow as_of")
    capture_seconds = _now_seconds(started_at, completed_at)
    if capture_seconds > _MAX_CAPTURE_SECONDS:
        _fail("completed_at", "capture interval exceeds ninety seconds")

    request = _parse_request(root["request"])
    if status != "unavailable":
        if request["client_id"] is None:
            _fail("request.client_id", "must be a lease value for this status")

    if status == "unavailable" and (root["account"] is not None or root["orders"] is not None):
        _fail("unavailable", "account and orders must be null")
    account = _parse_account(root["account"], status, completed_at, started_at)
    orders = _parse_orders(root["orders"], status, completed_at, started_at)
    restrictions = _parse_restrictions(root["restrictions"])

    account_end = account is not None and account["end_received"]
    orders_end = orders is not None and orders["end_received"]
    if status == "complete":
        if not (account_end and orders_end):
            _fail("status", "complete requires both end callbacks")
    elif status == "partial":
        if account_end and orders_end:
            _fail("status", "partial requires at least one missing end callback")

    proof_verified = False
    proof_sha = None
    proof = None
    if proof_raw is not None:
        proof = _parse_proof(proof_raw)
        proof_sha = _sha256(proof["_bytes"])
        if origin != "recorded":
            _fail("origin", "a proof is only meaningful for a recorded capture")
        _verify_proof(proof, data, request, root["completed_at"])
        proof_verified = True

    age = _now_seconds(completed_at, as_of_dt)
    if status == "unavailable":
        freshness = "unavailable"
    elif age > _STALE_SECONDS:
        freshness = "stale"
    else:
        freshness = "fresh"

    if origin == "recorded":
        if proof_verified:
            provenance = "recorded_request_verified"
        else:
            provenance = "unverified"
    else:
        provenance = "invented_fixture"

    orders_state, order_count = _classify_orders(
        orders, status, freshness, proof_verified, account["account_ready"] if account else None
    )

    result = {
        "schema": SCHEMA,
        "kind": KIND,
        "origin": origin,
        "source": SOURCE,
        "connection_mode": CONNECTION_MODE,
        "gateway_port": GATEWAY_PORT,
        "requested_account_id": REQUESTED_ACCOUNT,
        "account_id": account_id,
        "started_at": root["started_at"],
        "completed_at": root["completed_at"],
        "status": status,
        "reason": reason,
        "request": request,
        "account": account,
        "orders": orders,
        "restrictions": restrictions,
        "as_of": as_of,
        "freshness": freshness,
        "raw_sha256": _sha256(data),
        "proof_sha256": proof_sha,
        "provenance": provenance,
        "settled_usd": None,
        "settled_usd_state": "unknown",
        "restrictions_state": "unknown",
        "reserved_cash": None,
        "orders_state": orders_state,
        "order_count": order_count,
        "executable": False,
        "fill_eligible": False,
    }
    return result


__all__ = [
    "CAPTURE_SOURCE_PATH",
    "parse_account_readiness",
]
