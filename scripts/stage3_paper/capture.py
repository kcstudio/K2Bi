"""Stage 3 paper snapshot capture snippet, read only."""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

EXPECTED_ACCOUNT = "DUQ220152"
CLIENT_MIN = 90
CLIENT_MAX = 99
SUMMARY_TAGS = ("TotalCashValue", "NetLiquidation", "AvailableFunds")
BASE = "BASE"
FALLBACK = "USD"
SEC_TYPES = ("STK", "ETF", "OPT", "FUT", "CASH", "BOND")
_SYMBOL_RE = re.compile(r"[A-Z0-9 .\-]{1,40}")
_CURRENCY_RE = re.compile(r"[A-Z]{3}")
_ABS = Decimal("1e12")


def _lease(env):
    raw = env.get("K2BI_GATEWAY_CLIENT_ID")
    if raw is None or str(raw).strip() == "":
        raise ValueError("lease missing")
    try:
        value = int(str(raw).strip())
    except ValueError:
        raise ValueError("lease not integer")
    if value == 1 or value < CLIENT_MIN or value > CLIENT_MAX:
        raise ValueError("lease out of range")
    return value


def _now():
    return datetime.now(timezone.utc).isoformat()


def _decimal_text(value, *, nonnegative=False, allow_zero=True):
    if isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (TypeError, ValueError, InvalidOperation):
        return None
    if not parsed.is_finite():
        return None
    if abs(parsed) > _ABS:
        return None
    if nonnegative and parsed < 0:
        return None
    if not allow_zero and parsed == 0:
        return None
    text = format(parsed, "f")
    if "." in text:
        whole, frac = text.split(".", 1)
        if len(frac) > 8:
            return None
    return text


def _unavailable(reason, started, completed):
    return {"schema": 1, "kind": "ibkr-paper-account-snapshot", "origin": "recorded",
            "source": "IBKR paper Gateway", "connection_mode": "readonly",
            "gateway_port": 4002, "requested_account_id": EXPECTED_ACCOUNT,
            "account_id": None, "started_at": started, "completed_at": completed,
            "status": "unavailable", "reason": reason, "positions": None,
            "account_values": None}


def _managed(ib):
    try:
        accounts = ib.managedAccounts()
    except Exception:
        return []
    if not accounts:
        return []
    try:
        return [str(item) for item in accounts if item is not None]
    except TypeError:
        return []


def _pick_currency(candidates):
    base = [c for c in candidates if c == BASE]
    if len(base) == 1:
        return base[0]
    if base:
        return None
    usd = [c for c in candidates if c == FALLBACK]
    if len(usd) == 1:
        return usd[0]
    if usd:
        return None
    unique = set(candidates)
    if len(unique) == 1:
        return next(iter(unique))
    return None


def _positions(ib, account):
    try:
        rows = ib.reqPositions()
    except Exception:
        return None
    if rows is None:
        return None
    try:
        rows = list(rows)
    except TypeError:
        return None
    if len(rows) > 1000:
        return None
    collected = []
    seen = set()
    for row in rows:
        row_account = getattr(row, "account", None)
        if row_account is None:
            return None
        if str(row_account) != account:
            continue
        contract = getattr(row, "contract", None)
        if contract is None:
            return None
        con_id = getattr(contract, "conId", None)
        if isinstance(con_id, bool) or not isinstance(con_id, int) or con_id <= 0:
            return None
        if con_id in seen:
            return None
        seen.add(con_id)
        quantity = _decimal_text(getattr(row, "position", None), allow_zero=False)
        if quantity is None:
            return None
        average = _decimal_text(getattr(row, "avgCost", None), nonnegative=True)
        if average is None:
            return None
        symbol = getattr(contract, "symbol", None)
        sec_type = getattr(contract, "secType", None)
        currency = getattr(contract, "currency", None)
        exchange = getattr(contract, "exchange", None)
        if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
            return None
        if sec_type not in SEC_TYPES:
            return None
        if not isinstance(currency, str) or not _CURRENCY_RE.fullmatch(currency):
            return None
        if not isinstance(exchange, str) or not exchange:
            return None
        collected.append({"account_id": account, "contract_id": con_id,
                          "symbol": symbol, "sec_type": sec_type,
                          "currency": currency, "exchange": exchange,
                          "quantity": quantity, "average_cost": average})
    return collected


def _account_values(ib, account):
    try:
        rows = ib.accountSummary(account)
    except Exception:
        return None
    if rows is None:
        return None
    try:
        rows = list(rows)
    except TypeError:
        return None
    by_tag = {tag: [] for tag in SUMMARY_TAGS}
    for row in rows:
        row_account = getattr(row, "account", None)
        tag = getattr(row, "tag", None)
        if row_account is None or str(row_account) != account:
            continue
        if tag not in by_tag:
            continue
        by_tag[tag].append((getattr(row, "value", None), getattr(row, "currency", None)))
    result = {}
    for tag in SUMMARY_TAGS:
        entries = by_tag[tag]
        if not entries:
            result[tag] = None
            continue
        currencies = []
        bad = False
        for _value, currency in entries:
            if not isinstance(currency, str) or (currency != BASE and not _CURRENCY_RE.fullmatch(currency)):
                bad = True
                break
            currencies.append(currency)
        if bad:
            result[tag] = None
            continue
        chosen = _pick_currency(currencies)
        if chosen is None:
            result[tag] = None
            continue
        matches = [value for value, currency in entries if currency == chosen]
        if len(matches) != 1:
            result[tag] = None
            continue
        bounded = _decimal_text(matches[0])
        result[tag] = None if bounded is None else {"value": bounded, "currency": chosen}
    return result


def main(ib=None, fetch_fields=None, env=None):
    env = os.environ if env is None else env
    started = _now()
    try:
        lease = _lease(env)
    except ValueError:
        sys.stdout.write(json.dumps(_unavailable("gateway client lease invalid", started, _now())))
        return 0
    injected = ib is not None
    if not injected:
        from ib_async import IB
        from ib_async.ib import StartupFetch
        ib = IB()
        fetch_fields = StartupFetch(0) if fetch_fields is None else fetch_fields
    try:
        try:
            ib.connect("127.0.0.1", 4002, clientId=lease, timeout=10,
                       readonly=True, fetchFields=fetch_fields)
        except Exception:
            sys.stdout.write(json.dumps(_unavailable("gateway connection failed", started, _now())))
            return 0
        try:
            if EXPECTED_ACCOUNT not in _managed(ib):
                sys.stdout.write(json.dumps(_unavailable("paper account not managed by this lease", started, _now())))
                return 0
            positions = _positions(ib, EXPECTED_ACCOUNT)
            values = _account_values(ib, EXPECTED_ACCOUNT)
            reasons = []
            if positions is None:
                reasons.append("positions unavailable")
            if values is None:
                reasons.append("account values unavailable")
            payload = {"schema": 1, "kind": "ibkr-paper-account-snapshot",
                       "origin": "recorded", "source": "IBKR paper Gateway",
                       "connection_mode": "readonly", "gateway_port": 4002,
                       "requested_account_id": EXPECTED_ACCOUNT,
                       "account_id": EXPECTED_ACCOUNT, "started_at": started,
                       "completed_at": _now(), "status": "complete",
                       "reason": "; ".join(reasons), "positions": positions,
                       "account_values": values}
            sys.stdout.write(json.dumps(payload))
            return 0
        finally:
            try:
                if ib.isConnected():
                    ib.disconnect()
            except Exception:
                pass
    finally:
        try:
            if ib.isConnected():
                ib.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
