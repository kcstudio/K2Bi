"""Offline parser for recorded IBKR paper quote snapshots.

This module provides OBSERVED-FEED / ACCESS / SOURCE-TIME evidence ONLY.
It is not an executable quote, not a fill-eligibility decision, and not an
approval or recommendation. No network, filesystem, broker or engine access.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from scripts.stage3_paper.research_evidence import _parse_timestamp

__all__ = ["parse_quote_evidence"]

_MAX_BYTES = 1024 * 1024
_MAX_ROW_INTERVAL_SEC = 16.0
_MAX_ROOT_INTERVAL_SEC = 90.0
_SERVER_BACKWARD_TOL_SEC = 2.0
_FRESHNESS_WINDOW_SEC = 15 * 60.0
_MAX_REASON_LEN = 500

_ACCOUNT_ID = "DUQ220152"
_CONNECTION_MODE = "readonly"
_GATEWAY_PORT = 4002
_CLIENT_IDS = frozenset(range(90, 100))
_MARKET_DATA_TYPE = 3
_STARTUP_FETCH = 0
_GENERIC_TICKS = ""
_SNAPSHOT = True
_REGULATORY_SNAPSHOT = False

_ROOT_KEYS = (
    "schema",
    "kind",
    "source",
    "origin",
    "status",
    "requested_account_id",
    "account_matched",
    "connection_mode",
    "gateway_port",
    "client_id",
    "requested_market_data_type",
    "startup_fetch",
    "generic_ticks",
    "snapshot",
    "regulatory_snapshot",
    "started_at",
    "completed_at",
    "server_time",
    "rows",
)
_ROW_KEYS = (
    "symbol",
    "currency",
    "status",
    "reason",
    "requested_at",
    "completed_at",
    "snapshot_end_observed",
    "observed_data_type",
    "observed_data_type_at",
    "bid",
    "ask",
    "last",
    "last_trade_at",
    "received_at",
)
_EXPECTED_SYMBOLS = ("SPY", "G", "CDNS")
_REQUEST_KEYS = tuple("requested_account_id connection_mode gateway_port client_id requested_market_data_type startup_fetch generic_ticks snapshot regulatory_snapshot".split())

_DEC_RE = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")


def _fail(msg: str) -> None:
    raise ValueError(msg)


def _require_dict(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        _fail(f"{label} must be a JSON object")
    return value

def _require_exact_keys(obj: Dict[str, Any], keys: Tuple[str, ...], label: str) -> None:
    have = set(obj.keys())
    want = set(keys)
    if have != want:
        missing = sorted(want - have)
        extra = sorted(have - want)
        _fail(f"{label} key mismatch missing={missing} extra={extra}")


def _require_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        _fail(f"{label} must be a boolean")
    return value

def _require_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _fail(f"{label} must be an integer")
    return value

def _require_str(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _fail(f"{label} must be a string")
    return value


def _require_iso(value: Any, label: str) -> str:
    text = _require_str(value, label)
    ts = _parse_timestamp(text, label)
    if ts is None:
        _fail(f"{label} is not a controlled UTC timestamp")
    return text

def _parse_iso(value: str, label: str) -> Any:
    ts = _parse_timestamp(value, label)
    if ts is None:
        _fail(f"{label} is not a controlled UTC timestamp")
    return ts


def _optional_iso(value: Any, label: str) -> Tuple[Optional[str], Optional[Any]]:
    if value is None:
        return None, None
    text = _require_str(value, label)
    ts = _parse_timestamp(text, label)
    if ts is None:
        _fail(f"{label} is not a controlled UTC timestamp")
    return text, ts


def _decode_raw(raw: bytes) -> Dict[str, Any]:
    if not isinstance(raw, (bytes, bytearray)):
        _fail("raw must be bytes")
    data = bytes(raw)
    if len(data) > _MAX_BYTES:
        _fail("raw exceeds 1 MiB")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        _fail(f"raw is not valid UTF-8: {exc}")
    try:
        obj = json.loads(
            text,
            parse_constant=lambda s: _fail(f"non-finite JSON constant {s}"),
        )
    except ValueError as exc:
        _fail(f"raw is not valid JSON: {exc}")
    if not isinstance(obj, dict):
        _fail("raw JSON root must be an object")
    return obj


def _check_no_duplicates(text: str) -> None:
    seen_paths: List[Any] = []

    def hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            _fail("duplicate object keys in raw JSON")
        return {k: v for k, v in pairs}

    try:
        json.loads(
            text,
            object_pairs_hook=hook,
            parse_constant=lambda s: _fail(f"non-finite JSON constant {s}"),
        )
    except ValueError:
        raise
    finally:
        seen_paths.clear()


def _parse_decimal(
    value: Any, label: str
) -> Optional[Tuple[str, Decimal]]:
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{label} must be a plain decimal string or null")
    if value in ("", "NaN", "nan", "Infinity", "-Infinity", "inf", "-inf"):
        _fail(f"{label} uses a sentinel/unknown value")
    if not _DEC_RE.fullmatch(value):
        _fail(f"{label} is not a plain non-negative decimal string")
    if "." in value:
        frac = value.split(".", 1)[1]
        if len(frac) > 8:
            _fail(f"{label} exceeds 8 fraction digits")
    try:
        dec = Decimal(value)
    except InvalidOperation:
        _fail(f"{label} is not parseable as Decimal")
    if not dec.is_finite():
        _fail(f"{label} is not finite")
    if dec <= 0:
        _fail(f"{label} must be positive")
    if dec > Decimal("1000000000"):
        _fail(f"{label} exceeds 1e9 bound")
    if dec == 0:
        _fail(f"{label} must not be zero")
    return value, dec


def _classify(observed_type: Optional[int]) -> str:
    if observed_type == 1:
        return "live"
    if observed_type == 2:
        return "frozen"
    if observed_type == 3:
        return "delayed"
    if observed_type == 4:
        return "delayed_frozen"
    return "unknown"


def _seconds_between(a: Any, b: Any) -> float:
    return (b - a).total_seconds()


def parse_quote_evidence(raw, *, as_of, proof_raw=None, expected_program_sha256=None):
    if type(raw) is not bytes:
        raise ValueError("raw must be bytes")
    evaluated_at_str, evaluated_at = _optional_iso(as_of, "as_of")
    if evaluated_at is None:
        raise ValueError("as_of must be a controlled UTC timestamp")
    if len(raw) > _MAX_BYTES:
        raise ValueError("raw exceeds 1 MiB")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"raw is not valid UTF-8: {exc}")

    def _const(s):
        raise ValueError(f"non-finite JSON constant {s}")

    def _dedupe(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate object keys in raw JSON")
        return {k: v for k, v in pairs}

    try:
        root = json.loads(text, parse_constant=_const, object_pairs_hook=_dedupe)
    except RecursionError as exc:
        raise ValueError(f"raw JSON too deeply nested: {exc}")
    except ValueError as exc:
        raise ValueError(f"raw is not valid JSON: {exc}")
    if not isinstance(root, dict):
        raise ValueError("raw JSON root must be an object")

    _require_exact_keys(root, _ROOT_KEYS, "root")

    if root["schema"] != 1:
        raise ValueError("root.schema must be 1")
    if isinstance(root["schema"], bool) or not isinstance(root["schema"], int):
        raise ValueError("root.schema must be an integer")
    if root["kind"] != "ibkr-paper-quote-snapshot":
        raise ValueError("root.kind mismatch")
    if root["source"] != "IBKR paper Gateway":
        raise ValueError("root.source mismatch")
    if root["origin"] not in ("recorded", "fixture"):
        raise ValueError("root.origin must be recorded or fixture")
    status = root["status"]
    if status not in ("complete", "partial", "unavailable"):
        raise ValueError("root.status invalid")
    if root["requested_account_id"] != _ACCOUNT_ID:
        raise ValueError("root.requested_account_id mismatch")
    account_matched = _require_bool(root["account_matched"], "root.account_matched")
    if root["connection_mode"] != _CONNECTION_MODE:
        raise ValueError("root.connection_mode mismatch")
    if type(root["gateway_port"]) is not int or root["gateway_port"] != _GATEWAY_PORT:
        raise ValueError("root.gateway_port mismatch")
    if type(root["requested_market_data_type"]) is not int or root["requested_market_data_type"] != _MARKET_DATA_TYPE:
        raise ValueError("root.requested_market_data_type mismatch")
    if type(root["startup_fetch"]) is not int or root["startup_fetch"] != _STARTUP_FETCH:
        raise ValueError("root.startup_fetch mismatch")
    if root["generic_ticks"] != _GENERIC_TICKS:
        raise ValueError("root.generic_ticks mismatch")
    if root["snapshot"] is not True:
        raise ValueError("root.snapshot mismatch")
    if root["regulatory_snapshot"] is not False:
        raise ValueError("root.regulatory_snapshot mismatch")

    client_id_value = root["client_id"]
    client_id = None
    if client_id_value is not None:
        if isinstance(client_id_value, bool) or not isinstance(client_id_value, int):
            raise ValueError("root.client_id must be null or integer")
        client_id = client_id_value

    started_at_str, started_at = _optional_iso(root["started_at"], "root.started_at")
    completed_at_str, completed_at = _optional_iso(root["completed_at"], "root.completed_at")
    if started_at is None or completed_at is None:
        raise ValueError("root started/completed timestamps required")
    if completed_at < started_at:
        raise ValueError("root.completed_at precedes root.started_at")
    root_interval = (completed_at - started_at).total_seconds()
    if root_interval > _MAX_ROOT_INTERVAL_SEC:
        raise ValueError("root interval exceeds 90 seconds")
    if completed_at > evaluated_at:
        raise ValueError("root.completed_at is in the future relative to as_of")

    server_time_str, server_time = _optional_iso(root["server_time"], "root.server_time")
    if server_time is not None:
        if server_time > completed_at:
            raise ValueError("server_time is after root.completed_at")
        if (started_at - server_time).total_seconds() > _SERVER_BACKWARD_TOL_SEC:
            raise ValueError("server_time precedes root.started_at beyond tolerance")

    rows_value = root["rows"]
    if not isinstance(rows_value, list):
        raise ValueError("root.rows must be a list")
    if len(rows_value) != 3:
        raise ValueError("root.rows must contain exactly 3 rows")

    for index, raw_row in enumerate(rows_value):
        if not isinstance(raw_row, dict):
            raise ValueError(f"rows[{index}] must be an object")
        _require_exact_keys(raw_row, _ROW_KEYS, f"rows[{index}]")

    if client_id is None:
        if account_matched:
            raise ValueError("client_id required unless unavailable/invalid lease")
    elif client_id not in _CLIENT_IDS:
        raise ValueError("root.client_id must be a controlled lease 90..99")

    if not account_matched:
        if server_time is not None:
            raise ValueError("account mismatch must carry no server_time")
        if status != "unavailable":
            raise ValueError("account mismatch forces root.status unavailable")
        if client_id is not None and client_id not in _CLIENT_IDS:
            pass

    out_rows = []
    received_count = 0
    prev_row_end = None
    for index, raw_row in enumerate(rows_value):
        normalized = _parse_row(
            row=raw_row,
            index=index,
            root_start=started_at,
            root_end=completed_at,
            account_matched=account_matched,
        )
        if normalized["status"] == "received":
            received_count += 1
        req = normalized["_requested_at"][1]
        cmp_ = normalized["_completed_at"][1]
        if req is not None:
            if req < started_at or req > completed_at:
                raise ValueError(f"rows[{index}].requested_at outside root")
        if cmp_ is not None:
            if cmp_ < started_at or cmp_ > completed_at:
                raise ValueError(f"rows[{index}].completed_at outside root")
        if req is not None and cmp_ is not None:
            if (cmp_ - req).total_seconds() > _MAX_ROW_INTERVAL_SEC:
                raise ValueError(f"rows[{index}] interval exceeds 16 seconds")
            if prev_row_end is not None and req < prev_row_end:
                raise ValueError(f"rows[{index}] overlaps previous row interval")
            prev_row_end = cmp_
        out_rows.append(normalized)

    if not account_matched:
        if received_count != 0:
            raise ValueError("account mismatch must carry no observations")
        if any(r["observed_data_type"] is not None for r in out_rows):
            raise ValueError("account mismatch must carry no observed callbacks")
    elif status == "complete":
        if received_count != 3:
            raise ValueError("root.status complete requires all 3 rows received")
    elif status == "unavailable":
        if received_count != 0:
            raise ValueError("root.status unavailable requires zero received rows")
    else:
        if received_count == 0 or received_count == 3:
            raise ValueError("root.status partial requires 1 or 2 received rows")

    symbols = tuple(r["symbol"] for r in out_rows)
    if symbols != _EXPECTED_SYMBOLS:
        raise ValueError("rows must be SPY/G/CDNS in order")

    for r in out_rows:
        del r["_requested_at"]
        del r["_completed_at"]

    capture_verified = _verify_capture_proof(
        raw, root, proof_raw, expected_program_sha256, as_of
    )

    result = _build_result(
        raw=raw,
        root=root,
        status=status,
        rows=out_rows,
        evaluated_at_str=evaluated_at_str,
        evaluated_at=evaluated_at,
        completed_at=completed_at,
        server_time=server_time,
        server_time_str=server_time_str,
    )

    if capture_verified:
        capture_provenance = "matched_capture_receipt"
    elif root["origin"] == "fixture":
        capture_provenance = "invented_fixture"
    else:
        capture_provenance = "unverified"

    for r in out_rows:
        if r["observed_data_type"] == 1 and capture_verified:
            r["permission_detail"] = "Live callback observed for this instrument/request"
        elif r["observed_data_type"] == 1:
            r["permission_detail"] = (
                "Reported live callback; acquisition unverified"
            )

    result["capture_verified"] = capture_verified
    result["capture_provenance"] = capture_provenance
    result["quote_eligibility"] = False
    result["fill_eligible"] = False
    result["recommendation_verified"] = False
    return result


def _parse_row(*, row, index, root_start, root_end, account_matched):
    label = f"rows[{index}]"
    _require_exact_keys(row, _ROW_KEYS, label)

    symbol = _require_str(row["symbol"], f"{label}.symbol")
    currency = _require_str(row["currency"], f"{label}.currency")
    if currency != "USD":
        raise ValueError(f"{label}.currency must be USD")
    status = row["status"]
    if status not in ("received", "unavailable"):
        raise ValueError(f"{label}.status invalid")

    reason_value = row["reason"]
    if reason_value is None:
        reason = None
    else:
        reason = _require_str(reason_value, f"{label}.reason")
        if reason == "":
            raise ValueError(f"{label}.reason must be nonempty when present")
        if len(reason) > _MAX_REASON_LEN:
            raise ValueError(f"{label}.reason exceeds 500 characters")
    if status == "received" and reason is not None:
        raise ValueError(f"{label}.reason must be null for received rows")

    requested_at_str, requested_at = _optional_iso(row["requested_at"], f"{label}.requested_at")
    completed_at_str, completed_at = _optional_iso(row["completed_at"], f"{label}.completed_at")

    if status == "received":
        if requested_at is None or completed_at is None:
            raise ValueError(f"{label} received rows require requested/completed timestamps")
        if completed_at < requested_at:
            raise ValueError(f"{label}.completed_at precedes requested_at")
        if (completed_at - requested_at).total_seconds() > _MAX_ROW_INTERVAL_SEC:
            raise ValueError(f"{label} interval exceeds 16 seconds")
    else:
        if (requested_at is None) != (completed_at is None):
            raise ValueError(f"{label} attempted unavailable rows pair times")
        if requested_at is not None:
            if completed_at < requested_at:
                raise ValueError(f"{label}.completed_at precedes requested_at")
            if (completed_at - requested_at).total_seconds() > _MAX_ROW_INTERVAL_SEC:
                raise ValueError(f"{label} interval exceeds 16 seconds")

    snapshot_end_observed = _require_bool(row["snapshot_end_observed"], f"{label}.snapshot_end_observed")
    if status == "unavailable" and snapshot_end_observed:
        raise ValueError(f"{label}.snapshot_end_observed must be false when unavailable")
    if status == "received" and snapshot_end_observed is not True:
        raise ValueError(f"{label}.snapshot_end_observed must be true for received")

    observed_type_value = row["observed_data_type"]
    if observed_type_value is None:
        observed_type = None
    else:
        if isinstance(observed_type_value, bool) or not isinstance(observed_type_value, int):
            raise ValueError(f"{label}.observed_data_type must be null or int")
        if observed_type_value < 1 or observed_type_value > 4:
            raise ValueError(f"{label}.observed_data_type out of range")
        observed_type = observed_type_value

    observed_at_str, observed_at = _optional_iso(row["observed_data_type_at"], f"{label}.observed_data_type_at")
    if (observed_type is None) != (observed_at is None):
        raise ValueError(f"{label} observed type/time must co-vary")
    if observed_at is not None:
        if requested_at is not None and completed_at is not None:
            if observed_at < requested_at or observed_at > completed_at:
                raise ValueError(f"{label}.observed_data_type_at outside row interval")

    bid = _parse_decimal(row["bid"], f"{label}.bid")
    ask = _parse_decimal(row["ask"], f"{label}.ask")
    last = _parse_decimal(row["last"], f"{label}.last")

    last_trade_at_str, last_trade_at = _optional_iso(row["last_trade_at"], f"{label}.last_trade_at")
    if last_trade_at is not None and last_trade_at > root_end:
        raise ValueError(f"{label}.last_trade_at after root end")

    received_at_str, received_at = _optional_iso(row["received_at"], f"{label}.received_at")
    if received_at is not None:
        if requested_at is not None and completed_at is not None:
            if received_at < requested_at or received_at > completed_at:
                raise ValueError(f"{label}.received_at outside request interval")

    if not account_matched and (requested_at is not None or completed_at is not None):
        raise ValueError("account mismatch cannot carry attempted quote requests")
    if status == "unavailable":
        if requested_at is None and completed_at is None:
            if any(v is not None for v in (bid, ask, last)):
                raise ValueError(f"{label} unrequested rows carry no observations")
            if observed_type is not None or observed_at is not None:
                raise ValueError(f"{label} unrequested rows carry no callback")
            if last_trade_at is not None:
                raise ValueError(f"{label} unrequested rows carry no last-trade time")
            if received_at is not None:
                raise ValueError(f"{label} unrequested rows carry no local tick time")
        if account_matched is False:
            if observed_type is not None or observed_at is not None:
                raise ValueError(f"{label} account mismatch carries no callback")

    feed_classification = _classify(observed_type)
    access_outcome = "received_snapshot" if status == "received" else "unavailable"
    permission_detail = (
        "Live callback observed for this instrument/request"
        if observed_type == 1
        else "No live-feed permission established"
    )

    return {
        "symbol": symbol,
        "currency": currency,
        "status": status,
        "reason": reason,
        "requested_at": requested_at_str,
        "completed_at": completed_at_str,
        "snapshot_end_observed": snapshot_end_observed,
        "observed_data_type": observed_type,
        "observed_data_type_at": observed_at_str,
        "bid": bid[0] if bid else None,
        "ask": ask[0] if ask else None,
        "last": last[0] if last else None,
        "last_trade_at": last_trade_at_str,
        "received_at": received_at_str,
        "feed_classification": feed_classification,
        "source_freshness": "unknown_bid_ask_time",
        "access_outcome": access_outcome,
        "permission_detail": permission_detail,
        "executable": False,
        "_requested_at": (requested_at_str, requested_at),
        "_completed_at": (completed_at_str, completed_at),
    }


def _build_result(
    *,
    raw: bytes,
    root: Dict[str, Any],
    status: str,
    rows: List[Dict[str, Any]],
    evaluated_at_str: str,
    evaluated_at: Any,
    completed_at: Any,
    server_time: Optional[Any],
    server_time_str: Optional[str],
) -> Dict[str, Any]:
    raw_sha256 = hashlib.sha256(raw).hexdigest()

    if completed_at <= evaluated_at <= completed_at + _seconds_to_delta(
        _FRESHNESS_WINDOW_SEC
    ):
        freshness = "fresh"
    else:
        freshness = "stale"

    if server_time is None:
        regular_session_status = "unknown"
    else:
        weekday = server_time.weekday()
        if weekday >= 5:
            regular_session_status = "closed"
        else:
            regular_session_status = "not_proven"

    return {
        "schema": root["schema"],
        "kind": root["kind"],
        "source": root["source"],
        "origin": root["origin"],
        "status": status,
        "requested_account_id": root["requested_account_id"],
        "account_matched": root["account_matched"],
        "connection_mode": root["connection_mode"],
        "gateway_port": root["gateway_port"],
        "client_id": root["client_id"],
        "requested_market_data_type": root["requested_market_data_type"],
        "startup_fetch": root["startup_fetch"],
        "generic_ticks": root["generic_ticks"],
        "snapshot": root["snapshot"],
        "regulatory_snapshot": root["regulatory_snapshot"],
        "started_at": root["started_at"],
        "completed_at": root["completed_at"],
        "server_time": server_time_str,
        "rows": rows,
        "raw_sha256": raw_sha256,
        "evaluated_at": evaluated_at_str,
        "local_capture_freshness": freshness,
        "quote_eligibility": False,
        "fill_eligible": False,
        "recommendation_verified": False,
        "regular_session_status": regular_session_status,
    }


def _seconds_to_delta(seconds: float) -> Any:
    from datetime import timedelta

    return timedelta(seconds=seconds)

def _verify_capture_proof(raw, root, proof_raw, expected_program_sha256, as_of):
    if proof_raw is None:
        return False
    if type(proof_raw) is not bytes:
        raise ValueError("proof_raw must be bytes")
    if len(proof_raw) > _MAX_BYTES:
        raise ValueError("proof_raw exceeds 1 MiB")
    if root["origin"] == "fixture":
        if len(proof_raw) == 0:
            return False
    if expected_program_sha256 is None:
        raise ValueError("expected_program_sha256 required with proof")
    if not isinstance(expected_program_sha256, str):
        raise ValueError("expected_program_sha256 must be a string")
    if len(expected_program_sha256) != 64:
        raise ValueError("expected_program_sha256 must be 64 hex chars")
    for ch in expected_program_sha256:
        if ch not in "0123456789abcdef":
            raise ValueError("expected_program_sha256 must be lowercase hex")
    if len(proof_raw) == 0:
        raise ValueError("proof_raw is empty")
    try:
        text = proof_raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"proof_raw is not valid UTF-8: {exc}")

    def _const(s):
        raise ValueError(f"non-finite JSON constant {s}")

    def _dedupe(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate object keys in proof JSON")
        return {k: v for k, v in pairs}

    try:
        proof = json.loads(text, parse_constant=_const, object_pairs_hook=_dedupe)
    except RecursionError as exc:
        raise ValueError(f"proof JSON too deeply nested: {exc}")
    except ValueError as exc:
        raise ValueError(f"proof_raw is not valid JSON: {exc}")
    if not isinstance(proof, dict):
        raise ValueError("proof JSON root must be an object")

    _require_exact_keys(
        proof,
        (
            "schema",
            "kind",
            "source_sha256",
            "response_sha256",
            "request_sha256",
            "started_at",
            "completed_at",
            "returncode",
        ),
        "proof",
    )

    if isinstance(proof["schema"], bool) or not isinstance(proof["schema"], int):
        raise ValueError("proof.schema must be an integer")
    if proof["schema"] != 1:
        raise ValueError("proof.schema must be 1")
    if proof["kind"] != "ibkr-paper-quote-capture-proof":
        raise ValueError("proof.kind mismatch")

    def _hashfield(value, label):
        if not isinstance(value, str):
            raise ValueError(f"{label} must be a string")
        if len(value) != 64:
            raise ValueError(f"{label} must be 64 hex chars")
        for ch in value:
            if ch not in "0123456789abcdef":
                raise ValueError(f"{label} must be lowercase hex")
        return value

    source_sha = _hashfield(proof["source_sha256"], "proof.source_sha256")
    response_sha = _hashfield(proof["response_sha256"], "proof.response_sha256")
    request_sha = _hashfield(proof["request_sha256"], "proof.request_sha256")
    if source_sha != expected_program_sha256:
        raise ValueError("proof.source_sha256 does not match expected program hash")
    actual_response_sha = hashlib.sha256(raw).hexdigest()
    if response_sha != actual_response_sha:
        raise ValueError("proof.response_sha256 does not match raw response")
    request_payload = {key: root[key] for key in _REQUEST_KEYS}
    request_payload["symbols"] = ["SPY", "G", "CDNS"]
    actual_request_sha = hashlib.sha256(
        json.dumps(request_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if request_sha != actual_request_sha:
        raise ValueError("proof.request_sha256 does not match request keys")

    if isinstance(proof["returncode"], bool) or not isinstance(proof["returncode"], int):
        raise ValueError("proof.returncode must be an integer")
    if proof["returncode"] != 0:
        raise ValueError("proof.returncode must be 0")

    proof_start_str, proof_start = _optional_iso(proof["started_at"], "proof.started_at")
    proof_end_str, proof_end = _optional_iso(proof["completed_at"], "proof.completed_at")
    if proof_start is None or proof_end is None:
        raise ValueError("proof started/completed timestamps required")
    if proof_end < proof_start:
        raise ValueError("proof.completed_at precedes proof.started_at")
    if (proof_end - proof_start).total_seconds() > _MAX_ROOT_INTERVAL_SEC:
        raise ValueError("proof interval exceeds 90 seconds")

    evaluated_at_str, evaluated_at = _optional_iso(as_of, "as_of")
    if evaluated_at is None:
        raise ValueError("as_of must be a controlled UTC timestamp")

    root_start = _parse_iso(root["started_at"], "root.started_at")
    root_end = _parse_iso(root["completed_at"], "root.completed_at")

    if proof_start > root_start:
        raise ValueError("proof.started_at after root.started_at")
    if proof_end < root_end:
        raise ValueError("proof.completed_at before root.completed_at")
    if proof_end > evaluated_at:
        raise ValueError("proof.completed_at after as_of")

    if root["origin"] != "recorded":
        return False
    return True
