"""Stage 3 paper quote capture via ib_async. Stdlib + ib_async only."""

from __future__ import annotations

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

SCHEMA = 1
KIND = "ibkr-paper-quote-snapshot"
SOURCE = "IBKR paper Gateway"
ACCOUNT = "DUQ220152"
MODE = "readonly"
PORT = 4002
LEASE_RE = re.compile(r"9[0-9]")
MDT = 3
STARTUP_FETCH = 0
SYMBOLS = (("SPY", "ARCA"), ("G", "NYSE"), ("CDNS", "NASDAQ"))
CONTRACT_IDS = {"SPY": 756733, "G": 45411028, "CDNS": 5552}
CALL_TIMEOUT = 15.0
ROOT_LIMIT = 90.0
MAX_BYTES = 1024 * 1024
MAX_REASON = 500


def _now(now_fn: Callable[[], datetime] | None) -> datetime:
    value = (now_fn or (lambda: datetime.now(timezone.utc)))()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("now_fn must return an aware datetime")
    return value


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        raise ValueError("naive datetime")
    return value.isoformat()


def _lease(env: dict[str, str]) -> int | None:
    raw = env.get("K2BI_GATEWAY_CLIENT_ID")
    if not isinstance(raw, str) or LEASE_RE.fullmatch(raw) is None:
        return None
    return int(raw)


def _row(symbol: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "currency": "USD",
        "status": "unavailable",
        "reason": "not requested",
        "requested_at": None,
        "completed_at": None,
        "snapshot_end_observed": False,
        "observed_data_type": None,
        "observed_data_type_at": None,
        "bid": None,
        "ask": None,
        "last": None,
        "last_trade_at": None,
        "received_at": None,
    }


def _result(origin, status, *, client_id, matched, server_time, started, completed, reason, rows=None):
    return {
        "schema": SCHEMA,
        "kind": KIND,
        "source": SOURCE,
        "origin": origin,
        "status": status,
        "requested_account_id": ACCOUNT,
        "account_matched": matched,
        "connection_mode": MODE,
        "gateway_port": PORT,
        "client_id": client_id,
        "requested_market_data_type": MDT,
        "startup_fetch": STARTUP_FETCH,
        "generic_ticks": "",
        "snapshot": True,
        "regulatory_snapshot": False,
        "started_at": _iso(started),
        "completed_at": _iso(completed),
        "server_time": _iso(server_time),
        "rows": rows if rows is not None else [dict(_row(s), reason=reason) for s, _ in SYMBOLS],
    }


def _bounded(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number <= 0 or number > Decimal("1e9"):
        return None
    if -number.as_tuple().exponent > 8:
        return None
    return format(number, "f")


def _stamp(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) and value.tzinfo else None


def _reason(exc: BaseException) -> str:
    code = getattr(exc, "code", None)
    name = type(exc).__name__
    text = name if not isinstance(code, int) else f"{name}:{code}"
    return text[:MAX_REASON] or "request failed"


class _Capture:
    """Owns temporary wrapper hooks and own snapshot request IDs."""

    def __init__(self, ib: Any, now_fn) -> None:
        self.ib = ib
        self.now_fn = now_fn
        self.active = None
        self.wrapper = ib.wrapper
        self.ids: dict[int, str] = {}
        self.feeds: dict[int, int] = {}
        self.feed_at: dict[int, datetime] = {}
        self.ends: set[int] = set()
        self._orig: list[tuple[str, Any]] = []

    def install(self) -> None:
        w = self.wrapper
        self._orig.append(("startTicker", w.startTicker))
        self._orig.append(("marketDataType", w.marketDataType))
        self._orig.append(("tickSnapshotEnd", w.tickSnapshotEnd))
        orig_start = w.startTicker
        orig_mdt = w.marketDataType
        orig_end = w.tickSnapshotEnd

        def startTicker(reqId, contract, tickType):
            ticker = orig_start(reqId, contract, tickType)
            symbol = getattr(contract, "symbol", None)
            own = {s for s, _ in SYMBOLS}
            if tickType == "snapshot" and symbol in own and contract is self.active:
                self.ids[reqId] = symbol
            return ticker

        def marketDataType(reqId, marketDataId):
            if reqId in self.ids and isinstance(marketDataId, int) and not isinstance(marketDataId, bool):
                if 1 <= marketDataId <= 4:
                    self.feeds[reqId] = marketDataId
                    self.feed_at[reqId] = _now(self.now_fn)
            return orig_mdt(reqId, marketDataId)

        def tickSnapshotEnd(reqId):
            if reqId in self.ids:
                self.ends.add(reqId)
            return orig_end(reqId)

        w.startTicker = startTicker
        w.marketDataType = marketDataType
        w.tickSnapshotEnd = tickSnapshotEnd

    def restore(self) -> None:
        for name, fn in reversed(self._orig):
            setattr(self.wrapper, name, fn)
        self._orig.clear()

    def cancel_unfinished(self) -> None:
        client = getattr(self.ib, "client", None)
        if client is None:
            return
        for reqId in list(self.ids):
            if reqId in self.ends:
                continue
            try:
                client.cancelMktData(reqId)
            except Exception:
                pass


async def _capture_rows(ib: Any, started, now_fn, origin, lease, matched) -> dict[str, Any]:
    from ib_async import Stock

    cap = _Capture(ib, now_fn)
    cap.install()
    server_time = None
    rows: list[dict[str, Any]] = []
    try:
        try:
            raw = await asyncio.wait_for(ib.reqCurrentTimeAsync(), CALL_TIMEOUT)
            if isinstance(raw, datetime) and raw.tzinfo:
                server_time = raw
        except Exception:
            server_time = None
        ib.reqMarketDataType(MDT)
        for symbol, exchange in SYMBOLS:
            row = _row(symbol)
            requested = _now(now_fn)
            row["requested_at"] = _iso(requested)
            req_id = None
            try:
                contract = Stock(symbol, "SMART", "USD", primaryExchange=exchange, conId=CONTRACT_IDS[symbol])
                cap.active = contract
                tickers = await asyncio.wait_for(
                    ib.reqTickersAsync(contract, regulatorySnapshot=False), CALL_TIMEOUT
                )
                ticker = tickers[0] if tickers else None
                req_id = next((k for k, v in cap.ids.items() if v == symbol), None)
            except Exception as exc:
                row["completed_at"] = _iso(_now(now_fn))
                row["reason"] = _reason(exc)
                req_id = next((k for k, v in cap.ids.items() if v == symbol), None)
                if req_id in cap.feeds:
                    row["observed_data_type"] = cap.feeds[req_id]
                    row["observed_data_type_at"] = _iso(cap.feed_at[req_id])
                cap.active = None
                rows.append(row)
                continue
            cap.active = None
            completed = _now(now_fn)
            row["completed_at"] = _iso(completed)
            observed = cap.feeds.get(req_id) if req_id is not None else None
            if observed is not None:
                row["observed_data_type"] = observed
                row["observed_data_type_at"] = _iso(cap.feed_at.get(req_id))
            ended = req_id is not None and req_id in cap.ends
            row["snapshot_end_observed"] = bool(ended)
            if ticker is not None:
                stamp = getattr(ticker, "lastTimestamp", None)
                if observed in (3, 4):
                    stamp = getattr(ticker, "delayedLastTimestamp", None)
                row["bid"] = _bounded(getattr(ticker, "bid", None))
                row["ask"] = _bounded(getattr(ticker, "ask", None))
                row["last"] = _bounded(getattr(ticker, "last", None))
                row["last_trade_at"] = _stamp(stamp)
                row["received_at"] = _stamp(getattr(ticker, "time", None))
            if ended and ticker is not None:
                row["status"] = "received"
                row["reason"] = None
            else:
                row["reason"] = "snapshot end not observed" if ticker is not None else "no ticker returned"
            rows.append(row)
    finally:
        cap.cancel_unfinished()
        cap.restore()
    received = sum(1 for r in rows if r["status"] == "received")
    if not matched:
        status = "unavailable"
    elif received == 3:
        status = "complete"
    elif received:
        status = "partial"
    else:
        status = "unavailable"
    if not matched:
        rows = [_row(s) for s, _ in SYMBOLS]
    return _result(
        origin, status, client_id=lease, matched=matched, server_time=server_time,
        started=started, completed=_now(now_fn), reason="ok", rows=rows,
    )


async def _capture(ib: Any, lease, origin, now_fn) -> dict[str, Any]:
    from ib_async.ib import StartupFetch

    started = _now(now_fn)
    if ib is None:
        return _result(origin, "unavailable", client_id=lease, matched=False, server_time=None, started=started, completed=started, reason="no ib client")
    if lease is None:
        return _result(origin, "unavailable", client_id=None, matched=False, server_time=None, started=started, completed=started, reason="invalid client lease")
    ib.RequestTimeout = CALL_TIMEOUT
    ib.RaiseRequestErrors = True
    matched = False
    try:
        await ib.connectAsync(
            "127.0.0.1", PORT, clientId=lease, readonly=True, timeout=10,
            fetchFields=StartupFetch(STARTUP_FETCH),
        )
        accounts = ib.managedAccounts()
        matched = ACCOUNT in accounts if isinstance(accounts, (list, tuple, set)) else False
        if not matched:
            return _result(origin, "unavailable", client_id=lease, matched=False, server_time=None, started=started, completed=_now(now_fn), reason="account not managed")
        return await asyncio.wait_for(_capture_rows(ib, started, now_fn, origin, lease, True), ROOT_LIMIT)
    except Exception as exc:
        return _result(origin, "unavailable", client_id=lease, matched=False, server_time=None, started=started, completed=_now(now_fn), reason=_reason(exc))
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _make_ib():
    from ib_async import IB
    return IB()


def main(ib: Any = None, *, env=None, now_fn=None) -> int:
    env = os.environ if env is None else env
    lease = _lease(env)
    injected = ib is not None
    origin = "fixture" if injected else "recorded"

    async def entry() -> dict[str, Any]:
        client = ib if injected else _make_ib()
        return await _capture(client, lease, origin, now_fn)

    payload = asyncio.run(entry())
    text = json.dumps(payload, allow_nan=False, separators=(",", ":"), ensure_ascii=True)
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise ValueError("payload too large")
    json.loads(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
