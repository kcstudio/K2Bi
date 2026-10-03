#!/usr/bin/env python3
"""
Read-only IBKR paper account readiness capture.

Standalone: imports only the standard library plus installed ib_async.
Does NOT import the account-readiness parser or any repo module.  Running
this module prints exactly one JSON record to stdout describing the
API-visible account readiness observed from a paper Gateway.

This is an observation snapshot, not a trading permission.  It never
places, cancels, or modifies orders and never enables market data.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import decimal
import hashlib
import json
import os
import sys
import time
import uuid


SCHEMA = 1
KIND = "ibkr-paper-account-readiness"
SOURCE = "IBKR paper Gateway"
CONNECTION_MODE = "readonly"
ACCOUNT = "DUQ220152"
HOST = "127.0.0.1"
PORT = 4002
REQUEST_TIMEOUT = 15
CONNECT_TIMEOUT = 10
MAX_CAPTURE_SECONDS = 90
STALE_AFTER_SECONDS = 900

ALLOWED_TAGS = {
    "CashBalance",
    "TotalCashBalance",
    "SettledCash",
    "$LEDGER-CashBalance",
    "$LEDGER-TotalCashBalance",
    "$LEDGER-SettledCash",
}

_SEC_TYPE_ALLOWED = {"STK", "ETF", "OPT", "FUT", "CASH", "BOND"}

_ORDER_FIELD_BOUND = 2147483647


class CaptureError(Exception):
    """Raised for local pre-connect / post-connect setup failures."""


# ---------------------------------------------------------------------------
# Small helpers (kept self-contained to avoid importing repo modules).
# ---------------------------------------------------------------------------


def _utcnow_iso():
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def _iso_to_dt(value):
    if not isinstance(value, str):
        raise CaptureError("timestamp is not a string")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = _dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise CaptureError("bad timestamp: %s" % value) from exc
    if parsed.tzinfo is None:
        raise CaptureError("timestamp missing offset: %s" % value)
    return parsed


def _bounded_reason(parts):
    text = "; ".join(str(p) for p in parts if p)
    return text[:500]


def _canonical_decimal(value):
    """Return a canonical finite decimal string. Reject bool, NaN, inf, and
    non-decimal inputs. No silent coercion of missing values."""
    if isinstance(value, bool):
        raise CaptureError("bool is not a decimal")
    if isinstance(value, decimal.Decimal):
        dec = value
    elif isinstance(value, int):
        dec = decimal.Decimal(value)
    elif isinstance(value, float):
        dec = decimal.Decimal(str(value))
    elif isinstance(value, str):
        try:
            dec = decimal.Decimal(value.strip())
        except (decimal.InvalidOperation, ValueError) as exc:
            raise CaptureError("bad decimal string") from exc
    else:
        raise CaptureError("unsupported decimal type")
    if not dec.is_finite():
        raise CaptureError("non-finite decimal")
    import re
    text = format(dec, "f")
    if abs(dec) > decimal.Decimal("1e12") or not re.fullmatch(r"-?[0-9]{1,15}(?:\.[0-9]{1,8})?", text):
        raise CaptureError("decimal precision or bound")
    return text



def _bounded_int(value):
    """Strict bounded nonnegative int. Rejects bool and missing values."""
    if isinstance(value, bool) or value is None:
        raise CaptureError("invalid int")
    if not isinstance(value, int):
        raise CaptureError("non-int")
    if value < 0 or value > _ORDER_FIELD_BOUND:
        raise CaptureError("int out of bounds")
    return value



def _sha256_hex(payload):
    if isinstance(payload, str):
        payload = payload.encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# IB instance wrapper (callbacks) -- scoped strictly to one IB object.
# ---------------------------------------------------------------------------


class _EndCallbackRecorder:
    """Wrap ONE IB instance's ib.wrapper.accountDownloadEnd / openOrderEnd to
    record the ACTUAL callback time and gated phase, delegating to the original
    wrapper bound methods. Instance-scoped attribute placement; restored by
    caller in finally."""

    def __init__(self, ib, account):
        self.ib = ib
        self.account = account
        self.account_end_times = []
        self.account_end_accounts = []
        self.order_end_times = []
        self._saved = {}
        self._account_phase_active = False
        self._orders_phase_active = False

    def install(self):
        wrapper = self.ib.wrapper
        self._saved["accountDownloadEnd"] = wrapper.accountDownloadEnd
        self._saved["openOrderEnd"] = wrapper.openOrderEnd
        original_account = self._saved["accountDownloadEnd"]
        original_order = self._saved["openOrderEnd"]
        recorder = self

        def _account_download_end(account, *args, **kwargs):
            if recorder._account_phase_active:
                if account == recorder.account:
                    recorder.account_end_accounts.append(account)
                    recorder.account_end_times.append(_utcnow_iso())
            return original_account(account, *args, **kwargs)

        def _open_order_end(*args, **kwargs):
            if recorder._orders_phase_active:
                recorder.order_end_times.append(_utcnow_iso())
            return original_order(*args, **kwargs)

        wrapper.accountDownloadEnd = _account_download_end
        wrapper.openOrderEnd = _open_order_end

    def restore(self):
        wrapper = self.ib.wrapper
        for name, original in self._saved.items():
            try:
                setattr(wrapper, name, original)
            except Exception:
                pass
        self._saved = {}
        self._account_phase_active = False
        self._orders_phase_active = False

    def begin_account_phase(self):
        self._account_phase_active = True

    def end_account_phase(self):
        self._account_phase_active = False

    def begin_orders_phase(self):
        self._orders_phase_active = True

    def end_orders_phase(self):
        self._orders_phase_active = False

    @property
    def account_end_received(self):
        return len(self.account_end_times) > 0

    @property
    def account_end_time(self):
        return self.account_end_times[-1] if self.account_end_times else None

    @property
    def order_end_received(self):
        return len(self.order_end_times) > 0

    @property
    def order_end_time(self):
        return self.order_end_times[-1] if self.order_end_times else None



# ---------------------------------------------------------------------------
# Capture primitives.
# ---------------------------------------------------------------------------


def _account_row_is_allowed(tag, currency):
    if tag not in ALLOWED_TAGS:
        return False
    if tag.startswith("$LEDGER-"):
        return True
    if currency:
        return True
    # Bare native CashBalance/TotalCashBalance with empty currency is display
    # only; the contract says native CashBalance is never settlement, but it
    # is still a permitted display row.  We keep the tag but the settled
    # interpretation remains unknown regardless.
    return True


def _normalize_account_value(tag, currency, value):
    """Strictly validate a native cash/settled row. Preserve the currency
    exactly as returned (no silent uppercasing)."""
    if tag not in ALLOWED_TAGS:
        raise CaptureError("tag not permitted")
    if currency is None:
        raise CaptureError("missing currency")
    if not isinstance(currency, str):
        raise CaptureError("bad currency type")
    if currency == "" or currency == "BASE":
        cur = currency
    elif len(currency) == 3 and currency.isascii() and currency.isalpha() and currency.isupper():
        cur = currency
    else:
        raise CaptureError("bad currency")
    dec = _canonical_decimal(value)
    return {"tag": tag, "currency": cur, "value": dec}



def _normalize_order(row):
    """Validate returned-API-visible order identity and fields. Returns the
    row dict or None for unattributed; raises for bad matched rows."""
    account_id = row.get("account_id")
    if account_id is None or (isinstance(account_id, str) and account_id.strip() == ""):
        return None
    if account_id != ACCOUNT:
        return "foreign"
    row = dict(row)
    if row.get("order_type") != "LMT": row["limit_price"] = None
    validated = _validate_trade_row(row)
    if validated is None or validated == "foreign":
        return validated
    perm_id = validated["perm_id"]
    client_id = validated["client_id"]
    order_id = validated["order_id"]
    order_type = validated["order_type"]
    limit_price = validated["limit_price"]
    if order_type == "LMT":
        if limit_price is None:
            raise CaptureError("LMT missing lmtPrice")
        lp = decimal.Decimal(limit_price)
        if not lp.is_finite() or lp <= 0:
            raise CaptureError("LMT lmtPrice must be positive")
        validated["limit_price"] = format(lp, "f")
    else:
        validated["limit_price"] = None
    qty = decimal.Decimal(validated["quantity"])
    if qty < 0:
        raise CaptureError("negative quantity")
    return validated



def _extract_trade(row):
    """Turn a Trade (or compatible object) into a flat dict.  Never trusts
    default remaining/fills values for settlement claims."""
    order = getattr(row, "order", None)
    contract = getattr(row, "contract", None)
    status = getattr(row, "orderStatus", None)
    account = None
    if order is not None:
        account = getattr(order, "account", None)
    return {
        "account_id": account,
        "perm_id": getattr(order, "permId", None) if order is not None else None,
        "client_id": getattr(order, "clientId", None) if order is not None else None,
        "order_id": getattr(order, "orderId", None) if order is not None else None,
        "symbol": getattr(contract, "symbol", None) if contract is not None else None,
        "sec_type": getattr(contract, "secType", None) if contract is not None else None,
        "currency": getattr(contract, "currency", None) if contract is not None else None,
        "side": getattr(order, "action", None) if order is not None else None,
        "order_type": getattr(order, "orderType", None) if order is not None else None,
        "quantity": getattr(order, "totalQuantity", None) if order is not None else None,
        "limit_price": getattr(order, "lmtPrice", None) if order is not None else None,
        "status": getattr(status, "status", None) if status is not None else None,
    }


def _validate_trade_row(d):
    if d["account_id"] is None:
        return None
    if d["account_id"] != ACCOUNT:
        return "foreign"
    symbol = d["symbol"]
    if not isinstance(symbol, str) or not (1 <= len(symbol) <= 40):
        raise CaptureError("bad symbol")
    for ch in symbol:
        if not (ch.isascii() and (ch.isalnum() or ch in " .-")):
            raise CaptureError("bad symbol char")
    sec_type = d["sec_type"]
    if sec_type not in _SEC_TYPE_ALLOWED:
        raise CaptureError("bad sec_type")
    currency = d["currency"]
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isascii() or not currency.isalpha() or not currency.isupper():
        raise CaptureError("bad currency")
    side = d["side"]
    if side not in ("BUY", "SELL"):
        raise CaptureError("bad side")
    for field in ("order_type", "status"):
        text = d[field]
        if not isinstance(text, str) or not (1 <= len(text) <= 32):
            raise CaptureError("bad %s" % field)
        for ch in text:
            if not (ch.isascii() and (ch.isalnum() or ch == " ")):
                raise CaptureError("bad %s char" % field)
    d["perm_id"] = _bounded_int(d["perm_id"])
    d["client_id"] = _bounded_int(d["client_id"])
    d["order_id"] = _bounded_int(d["order_id"])
    d["quantity"] = _canonical_decimal(d["quantity"])
    if d["limit_price"] is not None:
        d["limit_price"] = _canonical_decimal(d["limit_price"])
    return d


# ---------------------------------------------------------------------------
# Core capture (sync IB API).
# ---------------------------------------------------------------------------


def _capture(ib, fetch_fields=None, started_at=None):
    """Perform already-connected, matched requests, wrapping callbacks and
    restoring them in finally. Does NOT connect/disconnect/unsubscribe."""
    started = started_at or _utcnow_iso()
    recorder = _EndCallbackRecorder(ib, ACCOUNT)
    installed = False
    account_section = None
    orders_section = None
    account_failed = False
    orders_failed = False
    reason_parts = []
    try:
        recorder.install()
        installed = True

        recorder.begin_account_phase()
        account_requested_at = _utcnow_iso()
        try:
            ib.reqAccountUpdates(ACCOUNT)
            account_requested_ok = True
        except Exception:
            account_requested_ok = False
        account_failed = not account_requested_ok
        recorder.end_account_phase()
        account_end_received = recorder.account_end_received and account_requested_ok
        account_ended_at = recorder.account_end_time if account_end_received else None
        if not account_end_received:
            account_failed = True

        recorder.begin_orders_phase()
        orders_requested_at = _utcnow_iso()
        try:
            trades = ib.reqAllOpenOrders()
            orders_requested_ok = True
        except Exception:
            trades = None
            orders_requested_ok = False
        orders_failed = not orders_requested_ok
        recorder.end_orders_phase()
        order_end_received = recorder.order_end_received and orders_requested_ok
        orders_ended_at = recorder.order_end_time if order_end_received else None
        if not order_end_received:
            orders_failed = True

        if account_failed:
            account_section = {
                "requested_at": account_requested_at,
                "ended_at": None,
                "end_received": False,
                "account_ready": None,
                "values": None,
            }
            reason_parts.append("account-updates")
        else:
            try:
                account_section = _build_account_section(
                    ib,
                    account_requested_at=account_requested_at,
                    account_ended_at=account_ended_at,
                )
            except CaptureError as exc:
                account_section = {
                    "requested_at": account_requested_at,
                    "ended_at": None,
                    "end_received": False,
                    "account_ready": None,
                    "values": None,
                }
                reason_parts.append("account-values")

        if orders_failed:
            orders_section = {
                "requested_at": orders_requested_at,
                "ended_at": None,
                "end_received": False,
                "rows": None,
                "unattributed_count": 0,
                "other_account_count": 0,
            }
            reason_parts.append("open-orders")
        else:
            try:
                orders_section = _build_orders_section(
                    trades,
                    orders_requested_at=orders_requested_at,
                    orders_ended_at=orders_ended_at,
                )
            except CaptureError as exc:
                orders_section = {
                    "requested_at": orders_requested_at,
                    "ended_at": None,
                    "end_received": False,
                    "rows": None,
                    "unattributed_count": 0,
                    "other_account_count": 0,
                }
                reason_parts.append("order-rows")
    finally:
        if installed:
            recorder.restore()

    status, reason = _finalize_status(account_section, orders_section, reason_parts)
    completed_at = _utcnow_iso()
    root = {
        "schema": SCHEMA,
        "kind": KIND,
        "origin": "recorded",
        "source": SOURCE,
        "connection_mode": CONNECTION_MODE,
        "gateway_port": PORT,
        "requested_account_id": ACCOUNT,
        "account_id": ACCOUNT if status in ("complete", "partial") else None,
        "started_at": started,
        "completed_at": completed_at,
        "status": status,
        "reason": reason,
        "request": {
            "account_updates": "reqAccountUpdates",
            "open_orders": "reqAllOpenOrders",
            "startup_fetch": 0,
            "timeout_seconds": REQUEST_TIMEOUT,
            "client_id": None,
            "bind_orders": False,
            "market_data": False,
        },
        "account": account_section,
        "orders": orders_section,
        "restrictions": {
            "state": "unknown",
            "reason": "permissions not captured by this observation snapshot",
        },
    }
    return root



def _unavailable_account_section():
    return None


def _unavailable_orders_section():
    return None


def _build_account_section(ib, *, account_requested_at, account_ended_at):
    """Build account section for a connected/matched end-received account.
    Rejects the WHOLE section on any bad matched value, malformed duplicate,
    or duplicate AccountReady flag."""
    rows = []
    seen = set()
    account_ready = None
    for attr in _dump_account_values(ib):
        tag = attr.get("tag")
        currency = attr.get("currency")
        value = attr.get("value")
        if tag == "AccountReady":
            if account_ready is not None:
                raise CaptureError("duplicate AccountReady")
            if isinstance(value, bool):
                account_ready = value
            elif isinstance(value, str) and value.strip() in ("true", "false"):
                account_ready = value.strip() == "true"
            else:
                raise CaptureError("invalid AccountReady")
            continue
        if tag not in ALLOWED_TAGS:
            raise CaptureError("unexpected account tag: %s" % tag)
        row = _normalize_account_value(tag, currency, value)
        key = (row["tag"], row["currency"])
        if key in seen:
            raise CaptureError("duplicate account value")
        seen.add(key)
        rows.append(row)
    if len(rows) > 100:
        raise CaptureError("too many account values")
    return {
        "requested_at": account_requested_at,
        "ended_at": account_ended_at,
        "end_received": True,
        "account_ready": account_ready,
        "values": rows,
    }



def _dump_account_values(ib):
    """Extract account value attributes from the IB instance. Each entry is a
    mapping of tag/currency/value; malformed access still returns the raw
    attribute if present."""
    try:
        raw = ib.accountValues(ACCOUNT)
    except Exception as exc:
        raise CaptureError("accountValues failed") from exc
    out = []
    for row in raw or []:
        if getattr(row, "account", None) != ACCOUNT: continue
        if getattr(row, "tag", None) not in ALLOWED_TAGS | {"AccountReady"}: continue
        out.append(
            {
                "tag": getattr(row, "tag", None),
                "currency": getattr(row, "currency", None),
                "value": getattr(row, "value", None),
            }
        )
    return out



def _build_orders_section(trades, *, orders_requested_at, orders_ended_at):
    """Build orders section for connected/matched end-received open orders.
    Foreign/unattributed counts are tallied; matched malformed rows reject the
    WHOLE orders payload (raising), never silently dropped. No default
    remaining/fills derivation."""
    unattributed = 0
    other = 0
    rows = []
    seen_identity = set()
    seen_client_order = set()
    for trade in list(trades or []):
        flat = _extract_trade(trade)
        account_id = flat.get("account_id")
        if account_id is None or (isinstance(account_id, str) and account_id.strip() == ""):
            unattributed += 1
            continue
        if account_id != ACCOUNT:
            other += 1
            continue
        normalized = _normalize_order(flat)
        if normalized is None or normalized == "foreign":
            continue
        perm_id = normalized["perm_id"]
        client_id = normalized["client_id"]
        order_id = normalized["order_id"]
        if perm_id > 0:
            identity = ("p", perm_id)
        else:
            identity = ("c", client_id, order_id)
        if identity in seen_identity:
            raise CaptureError("duplicate order identity")
        seen_identity.add(identity)
        pair = (client_id, order_id)
        if pair in seen_client_order:
            raise CaptureError("duplicate client/order id")
        seen_client_order.add(pair)
        rows.append(normalized)
    if unattributed > 1000 or other > 1000 or len(rows) > 1000:
        raise CaptureError("order counts out of bounds")
    return {
        "requested_at": orders_requested_at,
        "ended_at": orders_ended_at,
        "end_received": True,
        "rows": rows,
        "unattributed_count": unattributed,
        "other_account_count": other,
    }



def _finalize_status(account_section, orders_section, reason_parts):
    """Complete only when BOTH sections exhibit observed end (end_received
    True). Otherwise partial with nonempty reason. Both-None is unavailable."""
    account_ok = isinstance(account_section, dict) and account_section.get("end_received") is True
    orders_ok = isinstance(orders_section, dict) and orders_section.get("end_received") is True
    if account_section is None and orders_section is None:
        parts = list(reason_parts) or ["no-data"]
        return "unavailable", _bounded_reason(parts)
    if account_ok and orders_ok:
        return "complete", ""
    parts = list(reason_parts)
    if not account_ok:
        parts.append("account-incomplete")
    if not orders_ok:
        parts.append("orders-incomplete")
    return "partial", _bounded_reason(parts) or "partial"



# ---------------------------------------------------------------------------
# Entry helpers.
# ---------------------------------------------------------------------------


def _validate_fetch_fields(fetch_fields):
    """Accept None or int 0 or StartupFetch(0). Reject bool/nonzero."""
    if fetch_fields is None:
        return 0
    if isinstance(fetch_fields, bool):
        raise CaptureError("fetch_fields must be 0")
    if isinstance(fetch_fields, int):
        if fetch_fields != 0:
            raise CaptureError("fetch_fields must be 0")
        return 0
    from ib_async.ib import StartupFetch
    value = getattr(fetch_fields, "value", None)
    if isinstance(fetch_fields, StartupFetch) and value == 0:
        return 0
    raise CaptureError("fetch_fields must be 0")



def _validate_env(env):
    """Read the client-id lease from K2BI_GATEWAY_CLIENT_ID."""
    if env is None:
        env = os.environ
    value = env.get("K2BI_GATEWAY_CLIENT_ID")
    if not isinstance(value, str) or value not in tuple(str(n) for n in range(90, 100)):
        raise CaptureError("missing or invalid client-id lease")
    try:
        cid = int(value)
    except (TypeError, ValueError) as exc:
        raise CaptureError("K2BI_GATEWAY_CLIENT_ID not integer") from exc
    if not (90 <= cid <= 99):
        raise CaptureError("K2BI_GATEWAY_CLIENT_ID out of lease range")
    return cid



def _load_startup_fetch():
    try:
        from ib_async.ib import StartupFetch  # type: ignore
    except Exception:
        return None
    return StartupFetch


def _connect(ib, client_id, fetch_fields, timeout=CONNECT_TIMEOUT):
    from ib_async.ib import StartupFetch  # type: ignore
    ib.connect(
        HOST,
        PORT,
        clientId=client_id,
        readonly=True,
        timeout=timeout,
        fetchFields=StartupFetch(0),
    )



# ---------------------------------------------------------------------------
# Public API.
# ---------------------------------------------------------------------------


def main(ib=None, fetch_fields=None, env=None):
    started_at = _utcnow_iso()
    try:
        ff = _validate_fetch_fields(fetch_fields)
    except CaptureError as exc:
        record = _unavailable_record(str(exc))
        record["started_at"] = started_at
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
        return 0
    try:
        client_id = _validate_env(env)
    except CaptureError as exc:
        record = _unavailable_record(str(exc))
        record["started_at"] = started_at
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
        return 0

    real = ib is None
    ib_obj = ib
    record = None
    try:
        if real:
            from ib_async import IB  # type: ignore
            ib_obj = IB()
            ib_obj.RequestTimeout = REQUEST_TIMEOUT
            ib_obj.RaiseRequestErrors = True
        ib_obj.RequestTimeout = REQUEST_TIMEOUT
        ib_obj.RaiseRequestErrors = True
        _connect(ib_obj, client_id, ff)
        if ib_obj.RequestTimeout != REQUEST_TIMEOUT:
            ib_obj.RequestTimeout = REQUEST_TIMEOUT
        if ib_obj.RaiseRequestErrors is not True:
            ib_obj.RaiseRequestErrors = True
        managed = None
        try:
            managed = ib_obj.managedAccounts()
        except Exception:
            managed = None
        if not managed or ACCOUNT not in list(managed):
            raise CaptureError("managed-account-mismatch")
        record = _capture(ib_obj, fetch_fields=ff, started_at=started_at)
        record["request"]["client_id"] = client_id
    except CaptureError as exc:
        record = _unavailable_record(str(exc))
        record["request"]["client_id"] = client_id
        record["started_at"] = started_at
    except Exception as exc:
        record = _unavailable_record("capture-error:%s" % type(exc).__name__)
        record["request"]["client_id"] = client_id
        record["started_at"] = started_at
    finally:
        if ib_obj is not None:
            try:
                ib_obj.client.reqAccountUpdates(False, ACCOUNT)
            except Exception:
                pass
            try:
                ib_obj.disconnect()
            except Exception:
                pass
    record["completed_at"] = _utcnow_iso()
    try:
        sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    except Exception:
        return 1
    return 0



def _unavailable_record(reason):
    now = _utcnow_iso()
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "origin": "recorded",
        "source": SOURCE,
        "connection_mode": CONNECTION_MODE,
        "gateway_port": PORT,
        "requested_account_id": ACCOUNT,
        "account_id": None,
        "started_at": now,
        "completed_at": now,
        "status": "unavailable",
        "reason": _bounded_reason([reason]) or "unavailable",
        "request": {
            "account_updates": "reqAccountUpdates",
            "open_orders": "reqAllOpenOrders",
            "startup_fetch": 0,
            "timeout_seconds": REQUEST_TIMEOUT,
            "client_id": None,
            "bind_orders": False,
            "market_data": False,
        },
        "account": None,
        "orders": None,
        "restrictions": {
            "state": "unknown",
            "reason": "permissions not captured by this observation snapshot",
        },
    }


def _cli():
    parser = argparse.ArgumentParser(description="IBKR paper account readiness capture")
    parser.parse_args()
    return main()



if __name__ == "__main__":
    sys.exit(_cli())
