"""Offline recovery diagnostics: pure fixture evaluation only.

No environment, broker, filesystem or network access.  The genuine
production ``reconcile`` runs only for a complete fixture context, always
with ``override_env=""`` and ``adopt_orphan_stop=None``.  Every returned
event/adopted object is DIAGNOSTIC ONLY (executable/applied/approved=False).
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from execution.connectors.types import (
    BrokerOpenOrder,
    BrokerOrderStatusEvent,
    BrokerPosition,
)
from execution.engine.recovery import reconcile
from execution.journal import schema as journal_schema
from scripts.stage3_paper.offline_risk import load_json, parse_money, parse_timestamp

__all__ = ["evaluate_recovery"]

MAX_LIST = 1000
MAX_STR = 256
MAX_BYTES = 1 << 20
ROOT_KIND = "offline-recovery-context"
ACCOUNT_ID = "DUQ220152"
WINDOW = timedelta(minutes=15)
NR, CLEAN, CATCH_UP, MISMATCH = (
    "not_run",
    "fixture_clean",
    "fixture_catch_up",
    "fixture_mismatch_refused",
)
COVERAGE_KEYS = ("journal", "positions", "open_orders", "order_status")
ROOT_KEYS = frozenset(
    {
        "schema",
        "kind",
        "origin",
        "account_id",
        "captured_at",
        "expires_at",
        "journal_tail",
        "broker_positions",
        "broker_open_orders",
        "broker_order_status",
        "coverage",
    }
)
POSITION_KEYS = frozenset({"ticker", "qty", "avg_price"})
OPEN_ORDER_KEYS = frozenset(
    {
        "broker_order_id",
        "broker_perm_id",
        "ticker",
        "side",
        "qty",
        "filled_qty",
        "limit_price",
        "status",
        "submitted_at",
        "tif",
        "client_tag",
        "aux_price",
        "order_type",
    }
)
STATUS_EVENT_KEYS = frozenset(
    {
        "broker_order_id",
        "broker_perm_id",
        "status",
        "filled_qty",
        "remaining_qty",
        "avg_fill_price",
        "last_update_at",
        "reason",
        "client_tag",
    }
)
TIF_VALUES = ("DAY", "GTC", "GTD")
ORDER_TYPE_VALUES = ("LMT", "STP", "STP LMT")
PROJECTED_EVENTS = ("engine_recovered", "order_submitted", "order_filled")
PAYLOAD_KEYS = {
    "engine_recovered": frozenset({"adopted_positions"}),
    "order_submitted": frozenset(
        {"ticker", "side", "qty", "limit_price", "stop_loss", "submitted_at"}
    ),
    "order_filled": frozenset({"ticker", "side", "qty", "fill_price", "remaining_qty"}),
}
KNOWN_EVENT_TYPES = frozenset(
    {
        "engine_recovered",
        "order_submitted",
        "order_filled",
        "order_proposed",
        "order_cancelled",
        "order_rejected",
        "position_snapshot",
        "stop_adopted",
    }
)
KNOWN_EVENT_TYPES = frozenset(journal_schema.EVENT_TYPES)
REQ_TOP = tuple(journal_schema.REQUIRED_TOP_LEVEL)
JOURNAL_KEYS = frozenset(REQ_TOP) | {"ticker", "side", "qty", "broker_order_id", "broker_perm_id"}
HEX = set("0123456789abcdef")


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _map(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _keys(obj: dict[str, Any], allowed: frozenset[str], required, label: str) -> None:
    extra = set(obj) - allowed
    if extra:
        raise ValueError(f"{label} unexpected keys {sorted(extra)}")
    miss = set(required) - set(obj)
    if miss:
        raise ValueError(f"{label} missing keys {sorted(miss)}")


def _int(value: Any, label: str, lo: int, hi: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} must be an int")
    if value < lo or (hi is not None and value > hi):
        raise ValueError(f"{label} out of range")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    if len(value) > MAX_STR:
        raise ValueError(f"{label} exceeds {MAX_STR} chars")
    return value


def _text_ne(value: Any, label: str) -> str:
    text = _text(value, label)
    if not text.strip():
        raise ValueError(f"{label} must be non-empty")
    return text


def _money(value: Any, label: str, positive: bool) -> Decimal:
    if not isinstance(value, str) or isinstance(value, bool):
        raise ValueError(f"{label} must be a plain Decimal string")
    dec = parse_money(value, label)
    if not dec.is_finite() or (dec <= 0 if positive else dec < 0):
        raise ValueError(f"{label} out of range")
    return dec


def _opt_money(value: Any, label: str, positive: bool) -> Decimal | None:
    return None if value is None else _money(value, label, positive)


def _perm(value: Any, label: str) -> str:
    text = _text_ne(value, label)
    if len(text) > 20 or not text.isascii() or not text.isdigit() or int(text) == 0:
        raise ValueError(f"{label} must be non-zero decimal digits <=20")
    return text


def _ts(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timestamp string")
    return parse_timestamp(value, label)


def _opt_ts(value: Any, label: str) -> datetime | None:
    return None if value is None else _ts(value, label)


def _ticker(value: Any, label: str) -> str:
    text = _text(value, label)
    if re.fullmatch(r"[A-Z]{1,6}", text) is None:
        raise ValueError(f"{label} must be 1..6 uppercase")
    return text


def _section(root: dict[str, Any], key: str) -> list[Any] | None:
    value = root.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > MAX_LIST:
        raise ValueError(f"{key} must be a list <= {MAX_LIST} or null")
    return value


def _position(entry: Any, label: str) -> BrokerPosition:
    obj = _map(entry, label)
    _keys(obj, POSITION_KEYS, POSITION_KEYS, label)
    return BrokerPosition(
        ticker=_ticker(obj["ticker"], f"{label}.ticker"),
        qty=_int(obj["qty"], f"{label}.qty", 1, 1_000_000),
        avg_price=_money(obj["avg_price"], f"{label}.avg_price", True),
    )


def _positions(raw: list[Any] | None) -> list[BrokerPosition] | None:
    if raw is None:
        return None
    out, seen = [], set()
    for i, entry in enumerate(raw):
        pos = _position(entry, f"broker_positions[{i}]")
        if pos.ticker in seen:
            raise ValueError(f"duplicate position ticker {pos.ticker!r}")
        seen.add(pos.ticker)
        out.append(pos)
    return out


def _open_order(entry: Any, label: str) -> BrokerOpenOrder:
    obj = _map(entry, label)
    _keys(obj, OPEN_ORDER_KEYS, OPEN_ORDER_KEYS, label)
    oid = _perm(obj["broker_order_id"], f"{label}.broker_order_id")
    pid = _perm(obj["broker_perm_id"], f"{label}.broker_perm_id")
    ticker = _ticker(obj["ticker"], f"{label}.ticker")
    side = obj["side"]
    if side not in ("buy", "sell"):
        raise ValueError(f"{label}.side must be buy|sell")
    qty = _int(obj["qty"], f"{label}.qty", 1, 1_000_000)
    filled = _int(obj["filled_qty"], f"{label}.filled_qty", 0, qty)
    limit = _money(obj["limit_price"], f"{label}.limit_price", False)
    status = _text_ne(obj["status"], f"{label}.status")
    submitted = _opt_ts(obj["submitted_at"], f"{label}.submitted_at")
    tif = obj["tif"]
    if tif not in TIF_VALUES:
        raise ValueError(f"{label}.tif invalid")
    tag = _text(obj["client_tag"], f"{label}.client_tag")
    aux = _money(obj["aux_price"], f"{label}.aux_price", False)
    otype = obj["order_type"]
    if otype not in ORDER_TYPE_VALUES:
        raise ValueError(f"{label}.order_type invalid")
    if otype == "LMT" and limit <= 0:
        raise ValueError(f"{label} LMT needs limit_price > 0")
    if otype == "STP" and aux <= 0:
        raise ValueError(f"{label} STP needs aux_price > 0")
    if otype == "STP LMT" and (limit <= 0 or aux <= 0):
        raise ValueError(f"{label} STP LMT needs both > 0")
    return BrokerOpenOrder(
        broker_order_id=oid,
        broker_perm_id=pid,
        ticker=ticker,
        side=side,
        qty=qty,
        filled_qty=filled,
        limit_price=limit,
        status=status,
        submitted_at=submitted,
        tif=tif,
        client_tag=tag,
        aux_price=aux,
        order_type=otype,
    )


def _open_orders(raw: list[Any] | None) -> list[BrokerOpenOrder] | None:
    if raw is None:
        return None
    out, ids, perms = [], set(), set()
    for i, entry in enumerate(raw):
        order = _open_order(entry, f"broker_open_orders[{i}]")
        if order.broker_order_id in ids:
            raise ValueError(f"duplicate broker_order_id {order.broker_order_id!r}")
        if order.broker_perm_id in perms:
            raise ValueError(f"duplicate broker_perm_id {order.broker_perm_id!r}")
        ids.add(order.broker_order_id)
        perms.add(order.broker_perm_id)
        out.append(order)
    return out


def _status_event(entry: Any, label: str) -> BrokerOrderStatusEvent:
    obj = _map(entry, label)
    _keys(obj, STATUS_EVENT_KEYS, STATUS_EVENT_KEYS, label)
    filled = _int(obj["filled_qty"], f"{label}.filled_qty", 0)
    avg = _opt_money(obj["avg_fill_price"], f"{label}.avg_fill_price", True)
    if filled > 0 and avg is None:
        raise ValueError(f"{label} filled_qty > 0 needs avg_fill_price")
    last = _ts(obj["last_update_at"], f"{label}.last_update_at")
    reason = obj["reason"]
    if reason is not None:
        reason = _text(reason, f"{label}.reason")
    return BrokerOrderStatusEvent(
        broker_order_id=_perm(obj["broker_order_id"], f"{label}.broker_order_id"),
        broker_perm_id=_perm(obj["broker_perm_id"], f"{label}.broker_perm_id"),
        status=_text_ne(obj["status"], f"{label}.status"),
        filled_qty=filled,
        remaining_qty=_int(obj["remaining_qty"], f"{label}.remaining_qty", 0),
        avg_fill_price=avg,
        last_update_at=last,
        reason=reason,
        client_tag=_text(obj["client_tag"], f"{label}.client_tag"),
    )


def _status_events(raw: list[Any] | None) -> list[BrokerOrderStatusEvent] | None:
    if raw is None:
        return None
    out, ids, perms = [], set(), set()
    for i, entry in enumerate(raw):
        event = _status_event(entry, f"broker_order_status[{i}]")
        if event.broker_order_id in ids:
            raise ValueError(f"duplicate status order id {event.broker_order_id!r}")
        if event.broker_perm_id in perms:
            raise ValueError(f"duplicate status perm id {event.broker_perm_id!r}")
        ids.add(event.broker_order_id)
        perms.add(event.broker_perm_id)
        out.append(event)
    return out


def _record(entry: Any, label: str) -> dict[str, Any]:
    obj = _map(entry, label)
    try:
        journal_schema.validate(obj)
    except Exception as exc:  # controlled: schema rejection
        raise ValueError(f"{label} schema: {exc}") from exc
    _keys(obj, JOURNAL_KEYS, JOURNAL_KEYS, label)
    ver = obj["schema_version"]
    if type(ver) is not int or ver != 2:
        raise ValueError(f"{label}.schema_version must be int 2")
    ts = _ts(obj["ts"], f"{label}.ts")
    eid = _text_ne(obj["journal_entry_id"], f"{label}.journal_entry_id")
    sha = _text(obj["git_sha"], f"{label}.git_sha")
    if len(sha) != 40 or set(sha.lower()) - HEX:
        raise ValueError(f"{label}.git_sha must be 40 hex")
    out = dict(obj)
    out["ts"] = obj["ts"]
    out["journal_entry_id"] = eid
    out["event_type"] = _text_ne(obj["event_type"], f"{label}.event_type")
    for key in ("trade_id", "strategy"):
        val = obj[key]
        out[key] = None if val is None else _text_ne(val, f"{label}.{key}")
    if obj["ticker"] is not None: _ticker(obj["ticker"], label)
    if obj["side"] not in (None, "buy", "sell"): raise ValueError("journal side invalid")
    if obj["qty"] is not None: _int(obj["qty"], label, 1, 1_000_000)
    return out


def _payload(record: dict[str, Any], label: str) -> tuple[dict[str, Any], bool]:
    etype = record["event_type"]
    if etype not in PROJECTED_EVENTS:
        if etype not in KNOWN_EVENT_TYPES:
            raise ValueError(f"{label} unknown event_type {etype!r}")
        return {}, False
    payload = _map(record["payload"], f"{label}.payload")
    _keys(payload, PAYLOAD_KEYS[etype], PAYLOAD_KEYS[etype], f"{label}.payload")
    if etype == "engine_recovered":
        adopted = payload["adopted_positions"]
        if not isinstance(adopted, list) or len(adopted) > MAX_LIST:
            raise ValueError(f"{label}.adopted_positions invalid")
        seen = set()
        for i, entry in enumerate(adopted):
            pos = _position(entry, f"{label}.adopted_positions[{i}]")
            if pos.ticker in seen:
                raise ValueError(f"{label} duplicate adopted ticker")
            seen.add(pos.ticker)
        return payload, True
    ticker = _ticker(payload["ticker"], f"{label}.ticker")
    side = payload["side"]
    if side not in ("buy", "sell"):
        raise ValueError(f"{label}.side invalid")
    qty = _int(payload["qty"], f"{label}.qty", 1, 1_000_000)
    if record["trade_id"] is None or record["strategy"] is None:
        raise ValueError(f"{label} needs trade_id and strategy")
    for key in ("ticker", "side", "qty"):
        top = record.get(key)
        if top != payload[key]:
            raise ValueError(f"{label}.{key} top/payload mismatch")
    _perm(record.get("broker_perm_id"), f"{label}.broker_perm_id")
    _perm(record.get("broker_order_id"), f"{label}.broker_order_id")
    if etype == "order_submitted":
        _money(payload["limit_price"], f"{label}.limit_price", True)
        _opt_money(payload["stop_loss"], f"{label}.stop_loss", True)
        if _ts(payload["submitted_at"], f"{label}.submitted_at") > _ts(record["ts"], "journal ts"):
            raise ValueError(f"{label}.submitted_at after ts")
    else:
        _money(payload["fill_price"], f"{label}.fill_price", True)
        _int(payload["remaining_qty"], f"{label}.remaining_qty", 0)
    return payload, True


def _journal_tail(raw: list[Any] | None) -> tuple[list[dict[str, Any]] | None, bool]:
    if raw is None:
        return None, True
    out, seen, prev, supported = [], set(), None, True
    for i, entry in enumerate(raw):
        record = _record(entry, f"journal_tail[{i}]")
        if record["journal_entry_id"] in seen:
            raise ValueError("duplicate journal_entry_id")
        seen.add(record["journal_entry_id"])
        stamp = _ts(record["ts"], "journal ts")
        if prev is not None and stamp < prev:
            raise ValueError("journal ts not non-decreasing")
        prev = stamp
        _, ok = _payload(record, f"journal_tail[{i}]")
        supported = supported and ok
        out.append(record)
    return out, supported


def _coverage(root: dict[str, Any]) -> dict[str, bool]:
    cov = root.get("coverage")
    obj = _map(cov, "coverage")
    _keys(obj, frozenset(COVERAGE_KEYS), COVERAGE_KEYS, "coverage")
    for key in COVERAGE_KEYS:
        if not isinstance(obj[key], bool):
            raise ValueError(f"coverage.{key} must be bool")
    return {key: obj[key] for key in COVERAGE_KEYS}


def _enum_status(value: Any) -> str:
    status = getattr(value, "status", value)
    name = getattr(status, "value", status)
    if name == "clean":
        return CLEAN
    if name == "catch_up":
        return CATCH_UP
    return MISMATCH


def _ev(event: Any) -> dict[str, Any]:
    payload = event.payload if isinstance(event.payload, dict) else {"value": event.payload}
    return {
        "event_type": event.event_type,
        "ticker": event.ticker,
        "broker_order_id": event.broker_order_id,
        "broker_perm_id": event.broker_perm_id,
        "trade_id": event.trade_id,
        "strategy": event.strategy,
        "payload": json.loads(json.dumps(payload, default=str)),
        "executable": False,
        "applied": False,
        "approved": False,
    }


def _pos(pos: Any) -> dict[str, Any]:
    return {
        "ticker": pos.ticker,
        "qty": pos.qty,
        "avg_price": str(pos.avg_price),
        "executable": False,
        "applied": False,
        "approved": False,
    }


def _ord(order: Any) -> dict[str, Any]:
    submitted = order.submitted_at
    return {
        "broker_order_id": order.broker_order_id,
        "broker_perm_id": order.broker_perm_id,
        "ticker": order.ticker,
        "side": order.side,
        "qty": order.qty,
        "filled_qty": order.filled_qty,
        "limit_price": str(order.limit_price),
        "status": order.status,
        "submitted_at": submitted.isoformat() if submitted is not None else None,
        "tif": order.tif,
        "client_tag": order.client_tag,
        "aux_price": str(order.aux_price),
        "order_type": order.order_type,
        "executable": False,
        "applied": False,
        "approved": False,
    }


def _baseline(status, origin, account, created, as_of, expires, raw_sha, journal_sha, reason):
    return {
        "status": status,
        "origin": origin,
        "account_id": account,
        "captured_at": created,
        "evaluated_at": as_of,
        "expires_at": expires,
        "raw_sha256": raw_sha,
        "journal_sha256": journal_sha,
        "reason": reason,
        "events": [],
        "mismatch_reasons": [],
        "diagnostic_adopted_positions": [],
        "diagnostic_adopted_open_orders": [],
        "executable": False,
        "applied": False,
        "approved": False,
    }


def evaluate_recovery(raw: bytes | None, *, as_of: str) -> dict[str, Any]:
    if not isinstance(as_of, str):
        raise ValueError("as_of must be a string")
    as_of_dt = parse_timestamp(as_of, "as_of")
    if raw is None:
        return _baseline(NR, None, None, None, as_of, None, None, None, "no raw context")
    if type(raw) is not bytes:
        raise ValueError("raw must be bytes or None")
    data = bytes(raw)
    if len(data) > MAX_BYTES:
        raise ValueError("raw exceeds 1 MiB")
    raw_sha = _sha(data)
    try:
        root_obj = load_json(data)
    except ValueError as exc:
        raise ValueError(f"malformed fixture JSON: {exc}") from exc

    root = _map(root_obj, "root")
    _keys(root, ROOT_KEYS, ROOT_KEYS, "root")
    ver = root["schema"]
    if type(ver) is not int or ver != 1:
        raise ValueError("schema must be exactly 1")
    if root["kind"] != ROOT_KIND:
        raise ValueError("bad kind")
    origin = root["origin"]
    if origin not in ("recorded", "fixture"):
        raise ValueError("origin invalid")
    account = _text_ne(root["account_id"], "account_id")
    if account != ACCOUNT_ID:
        raise ValueError("unexpected account_id")
    captured = _ts(root["captured_at"], "captured_at")
    expires = _ts(root["expires_at"], "expires_at")
    if captured > as_of_dt:
        raise ValueError("captured_at in the future")
    if expires <= captured:
        raise ValueError("expires_at must be > captured_at")
    if expires - captured > WINDOW:
        raise ValueError("expires_at exceeds capture+15min")

    cov = _coverage(root)
    jt_raw = _section(root, "journal_tail")
    pos_raw = _section(root, "broker_positions")
    ord_raw = _section(root, "broker_open_orders")
    st_raw = _section(root, "broker_order_status")

    jt, supported = _journal_tail(jt_raw)
    positions = _positions(pos_raw)
    orders = _open_orders(ord_raw)
    statuses = _status_events(st_raw)
    if any(_ts(r["ts"], "journal ts") > captured for r in jt or []): raise ValueError("future journal timestamp")
    if any(o.submitted_at is None or o.submitted_at > captured for o in orders or []): raise ValueError("unknown or future order timestamp")
    if any(e.last_update_at > captured for e in statuses or []): raise ValueError("future status timestamp")
    jt_sha = None
    if jt is not None:
        canonical = json.dumps(jt, sort_keys=True, default=str, separators=(",", ":"))
        jt_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    base = (origin, account, captured.isoformat(), as_of, expires.isoformat(), raw_sha, jt_sha)

    def nr(reason: str) -> dict[str, Any]:
        return _baseline(NR, *base, reason)

    if not (as_of_dt < expires):
        return nr("context expired")
    if origin != "fixture":
        return nr("recorded origin is never reconciled")
    if jt is None:
        return nr("journal_tail section missing")
    if positions is None:
        return nr("broker_positions section missing")
    if orders is None:
        return nr("broker_open_orders section missing")
    if statuses is None:
        return nr("broker_order_status section missing")
    if not all(cov.values()):
        return nr("coverage incomplete")
    if not supported:
        return nr("unsupported journal event projection")

    result = reconcile(
        journal_tail=jt,
        broker_positions=positions,
        broker_open_orders=orders,
        broker_order_status=statuses,
        now=captured,
        override_env="",
        adopt_orphan_stop=None,
    )
    status = _enum_status(result)
    out = _baseline(status, *base, "fixture reconciled")
    out["events"] = [_ev(e) for e in result.events]
    out["mismatch_reasons"] = [dict(m) for m in result.mismatch_reasons]
    out["diagnostic_adopted_positions"] = [_pos(p) for p in result.adopted_positions]
    out["diagnostic_adopted_open_orders"] = [_ord(o) for o in result.adopted_open_orders]
    return out
