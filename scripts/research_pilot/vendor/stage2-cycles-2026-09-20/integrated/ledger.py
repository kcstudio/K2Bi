from __future__ import annotations

import copy
import hashlib
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from contracts import UNIVERSE as _CONTRACTS_UNIVERSE

D = Decimal
CENT = D("0.01")
FOUR = D("0.0001")
FEE = D("0.50")
BPS = D("0.0005")
UNIVERSE = tuple(_CONTRACTS_UNIVERSE)
BOOKS = ("research", "intraday")


class LedgerError(ValueError):
    pass


def _book() -> dict[str, Any]:
    return {
        "initial_cash": "10000.00", "cash": "10000.00", "positions": {},
        "events": [], "pending": {}, "last_prices": {},
        "realized_pnl": "0.00",
        "fees": "0.00",
        "market_value": "0.00", "unrealized_pnl": "0.00",
        "equity": "10000.00", "net_pnl": "0.00",
    }


def initial_state() -> dict[str, Any]:
    return {"books": {"research": _book(), "intraday": _book()}, "research": {},
            "last_cycle": None, "resources": {"cycles": 0, "source_requests": 0,
                                               "model_usd": "0"}}


def _num(x: Any, *, price: bool = False) -> D:
    if isinstance(x, bool) or isinstance(x, float) or x is None:
        raise LedgerError("invalid numeric type")
    try:
        v = x if isinstance(x, Decimal) else D(str(x).strip())
    except (InvalidOperation, ValueError):
        raise LedgerError("invalid numeric value") from None
    if not v.is_finite() or (price and (v <= 0 or v > D("1000000"))):
        raise LedgerError("invalid numeric range")
    return v


def _c(x: D) -> D:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def _f4(x: D) -> D:
    return x.quantize(FOUR, rounding=ROUND_HALF_UP)


def _buy_fill(raw: D) -> D:
    return _f4(raw * (D(1) + BPS))


def _sell_fill(raw: D) -> D:
    return _f4(raw * (D(1) - BPS))


def _money(x: Any) -> D:
    return _c(_num(x))


def _evidence(analyses: Mapping[str, Any] | None, book: str, symbol: str) -> dict[str, Any]:
    a = analyses or {}
    e = a.get(symbol, {}) if isinstance(a, Mapping) else {}
    if not isinstance(e, Mapping):
        return {"support": False, "invalidate": False, "paused": True,
                "period_kind": None}
    facts = e.get("facts", {}) if isinstance(e.get("facts", {}), Mapping) else {}
    pk = e.get("period_kind", facts.get("period_kind"))
    if pk is not None and pk not in ("quarter", "annual"):
        raise LedgerError("facts.period_kind must be quarter|annual")
    status = e.get("status")
    if status is None:
        paused = True
    else:
        paused = str(status).lower() != "fresh"
    stance = str(e.get("stance", "paused")).lower()
    support = (not paused) and stance == "support"
    invalidate = stance == "invalidate"
    return {"support": support, "invalidate": invalidate, "paused": paused,
            "period_kind": pk}


def _mark(book: dict[str, Any], symbol: str) -> D | None:
    m = book["last_prices"].get(symbol)
    return None if not m else _num(m["price"], price=True)


def _risk_ok(raw: D, qty: int, initial: D) -> bool:
    entry, stop = _buy_fill(raw), _sell_fill(_f4(raw * D("0.97")))
    risk = (entry - stop) * qty + FEE * 2
    return risk <= initial * D("0.01")


def _buy_qty(raw: D, cash: D, initial: D) -> int:
    cap = min(cash, initial * D("0.20"))
    q = int(cap / (raw * (D(1) + BPS))) + 1
    q = min(q, 100000)
    while q > 0:
        fill = _buy_fill(raw); debit = _c(D(q) * fill) + FEE
        if debit <= cap and _risk_ok(raw, q, initial):
            return q
        q -= 1
    return 0


def _event(book: str, symbol: str, ts: str, side: str, qty: int, raw: D, cash_after: D, reason: str) -> dict[str, Any]:
    fill = _buy_fill(raw) if side == "BUY" else _sell_fill(raw)
    slip = _f4(abs(fill - raw) * qty)
    return {"id": f"{book}/{symbol}/{ts}/{side}", "timestamp": ts, "symbol": symbol,
            "side": side, "quantity": int(qty), "raw_price": str(raw),
            "fill_price": str(fill), "fee": "0.50", "slippage": str(slip),
            "cash_after": str(_c(cash_after)), "reason": reason}


def _sell(book: dict[str, Any], symbol: str, raw: D, ts: str, reason: str) -> bool:
    pos = book["positions"].get(symbol)
    if not pos:
        return False
    qty = int(pos["quantity"]); credit = _c(D(qty) * _sell_fill(raw)) - FEE
    book["cash"] = str(_c(_money(book["cash"]) + credit))
    book["fees"] = str(_c(_money(book["fees"]) + FEE))
    book["realized_pnl"] = str(_c(_money(book["realized_pnl"]) + credit - _money(pos["basis"])))
    book["events"].append(_event(_book_name(book), symbol, ts, "SELL", qty, raw, _money(book["cash"]), reason))
    del book["positions"][symbol]
    return True


def _book_name(book: dict[str, Any]) -> str:
    return "intraday" if book is not None and book.get("initial_cash") == _book()["initial_cash"] and book.get("_name") == "intraday" else "research"


def _buy(book: dict[str, Any], symbol: str, raw: D, ts: str, reason: str) -> bool:
    if symbol in book["positions"] or len(book["positions"]) >= 2:
        return False
    initial, cash = _money(book["initial_cash"]), _money(book["cash"])
    qty = _buy_qty(raw, cash, initial)
    if qty <= 0:
        return False
    debit = _c(D(qty) * _buy_fill(raw)) + FEE
    book["cash"] = str(_c(cash - debit))
    book["fees"] = str(_c(_money(book["fees"]) + FEE))
    avg = _f4(debit / qty)
    book["positions"][symbol] = {"quantity": qty, "basis": str(debit), "average_cost": str(avg),
                                  "lastmark": str(raw), "mark_timestamp": ts}
    book["events"].append(_event(_book_name(book), symbol, ts, "BUY", qty, raw, _money(book["cash"]), reason))
    return True


def _note(notes: dict[tuple[str, str], tuple[str, str]], book: str, symbol: str, action: str, reason: str) -> None:
    notes[(book, symbol)] = (action, reason)


def _stops(avg: D, raw: D) -> str | None:
    if raw <= avg * D("0.97"):
        return "3% stop"
    if raw >= avg * D("1.05"):
        return "5% target"
    return None


def advance(state: Mapping[str, Any], analyses: Mapping[str, Any] | None, prices: Mapping[str, Any] | None,
            as_of: str, session_close: bool = False) -> tuple[dict[str, Any], list[dict[str, str]]]:
    from contracts import aware as _aware
    from contracts import decimal_string as _decimal_string
    if type(session_close) is not bool:
        raise LedgerError("session_close must be bool")
    s = copy.deepcopy(dict(state))
    prev_lc = s.get("last_cycle")
    if prev_lc is not None:
        if not (_aware(as_of) > _aware(prev_lc)):
            raise LedgerError("as_of must be strictly after state.last_cycle")
    else:
        _aware(as_of)
    as_of = _aware(as_of).isoformat()
    close = session_close
    valid: dict[str, D] = {}
    if prices:
        if not set(prices).issubset(set(UNIVERSE)):
            raise LedgerError("prices must be subset of universe")
        for sym in prices:
            if _decimal_string(prices[sym]) > D("1000000"):
                raise LedgerError("price must be at most 1000000")
            valid[sym] = _num(prices[sym], price=True)
    notes: dict[tuple[str, str], tuple[str, str]] = {}
    for bname in BOOKS:
        book = s["books"][bname]; book["_name"] = bname
        prev_prices = copy.deepcopy(book.get("last_prices", {}))
        for sym in UNIVERSE:
            if sym in valid:
                book["last_prices"][sym] = {"price": str(valid[sym]), "timestamp": as_of}
                if sym in book["positions"]:
                    book["positions"][sym]["lastmark"] = str(valid[sym]); book["positions"][sym]["mark_timestamp"] = as_of
        prev = [(sym, book["pending"][sym]) for sym in UNIVERSE if sym in book["pending"]]
        for sym, pend in prev:
            ev, raw = _evidence(analyses, bname, sym), valid.get(sym)
            if pend.get("action") == "enter" and (close or not ev["support"]):
                book["pending"].pop(sym, None)
                _note(notes, bname, sym, "abstain", "canceled unsupported entry")
                continue
            if raw is None:
                _note(notes, bname, sym, "hold" if sym in book["positions"] else "abstain",
                      "stale carried mark; fills paused" if sym in book["positions"] else "missing quote; evidence paused")
                continue
            action = pend.get("action")
            created_at = pend.get("created_at")
            if created_at is not None and _aware(created_at) > _aware(as_of):
                raise LedgerError("pending created_at after as_of")
            if action == "enter":
                if close or not ev["support"]:
                    book["pending"].pop(sym, None); _note(notes, bname, sym, "abstain", "canceled unsupported entry")
                elif created_at is not None and not (_aware(created_at) < _aware(as_of)):
                    _note(notes, bname, sym, "abstain", "pending entry not before as_of")
                elif _buy(book, sym, raw, as_of, str(pend.get("reason", "entry"))):
                    book["pending"].pop(sym, None); _note(notes, bname, sym, "enter", "filled prior pending entry")
                else:
                    book["pending"].pop(sym, None); _note(notes, bname, sym, "abstain", "entry risk/cash rejected")
            elif action == "exit" and sym in book["positions"]:
                book["pending"].pop(sym, None)
                if _sell(book, sym, raw, as_of, str(pend.get("reason", "exit"))):
                    _note(notes, bname, sym, "exit", "filled prior pending exit")
        if close and bname == "intraday":
            for sym in list(book["pending"]):
                if book["pending"][sym].get("action") == "enter":
                    book["pending"].pop(sym); _note(notes, bname, sym, "abstain", "session close canceled entry; close assumption")
            for sym in UNIVERSE:
                raw = valid.get(sym)
                if raw is None:
                    continue
                if sym in book["positions"] and _sell(book, sym, raw, as_of, "predefined session_close liquidation"):
                    book["pending"].pop(sym, None)
                    _note(notes, bname, sym, "exit", "predefined session_close liquidation")
        for sym in UNIVERSE:
            if (bname, sym) in notes:
                continue
            ev = _evidence(analyses, bname, sym); raw = valid.get(sym); pos = book["positions"].get(sym)
            stop = _stops(_num(pos["average_cost"]), raw) if pos and raw else None
            if ev["paused"] and not stop and not ev["invalidate"]:
                _note(notes, bname, sym, "hold" if pos else "abstain", "paused_sources visible; new entries blocked")
                continue
            if pos:
                if ev["invalidate"] or stop:
                    reason = "invalidate" if ev["invalidate"] else str(stop)
                    book["pending"][sym] = {"action": "exit", "reason": reason, "created_at": as_of}
                    _note(notes, bname, sym, "exit", f"queued exit: {reason}")
                elif book["pending"].get(sym, {}).get("action") == "exit":
                    _note(notes, bname, sym, "exit", "queued exit pending")
                else:
                    _note(notes, bname, sym, "hold", "held; no exit trigger" if raw else "missing quote; carried dated mark")
                continue
            if raw is None:
                _note(notes, bname, sym, "abstain", "missing quote; carried dated mark"); continue
            if close:
                _note(notes, bname, sym, "abstain", "session_close; no new entries"); continue
            if ev["support"] and not book["pending"].get(sym):
                if bname == "research":
                    if len(book["positions"]) + sum(1 for p in book["pending"].values() if p.get("action") == "enter") >= 2:
                        _note(notes, bname, sym, "abstain", "max 2 positions")
                    else:
                        book["pending"][sym] = {"action": "enter", "reason": "support", "created_at": as_of}
                        _note(notes, bname, sym, "enter", "queued support entry")
                    continue
                if bname == "intraday":
                    pm = prev_prices.get(sym)
                    if pm is None:
                        _note(notes, bname, sym, "abstain", "intraday requires prior quote")
                    elif not (_num(raw, price=True) > _num(pm["price"], price=True) * D("1.01")):
                        _note(notes, bname, sym, "abstain", "intraday momentum absent")
                    elif len(book["positions"]) + sum(1 for p in book["pending"].values() if p.get("action") == "enter") >= 2:
                        _note(notes, bname, sym, "abstain", "max 2 positions")
                    else:
                        book["pending"][sym] = {"action": "enter", "reason": "support", "created_at": as_of}
                        _note(notes, bname, sym, "enter", "queued support entry")
                    continue
            _note(notes, bname, sym, "abstain", "no support" if not ev["support"] else "intraday momentum absent")
        book.pop("_name", None)
        book.update(_totals(book))
    s["research"] = copy.deepcopy(analyses)
    s["last_cycle"] = as_of
    reconcile(s)
    decisions = [{"book": b, "symbol": sym, "action": notes[(b, sym)][0], "reason": notes[(b, sym)][1],
                  "timestamp": as_of} for b in BOOKS for sym in UNIVERSE]
    return s, decisions


def _totals(book: dict[str, Any]) -> dict[str, str]:
    initial = _money(book["initial_cash"])
    cash = _money(book["cash"])
    mv_raw = D(0)
    basis_sum = D(0)
    for p in book["positions"].values():
        mv_raw += _num(p["lastmark"], price=True) * int(p["quantity"])
        basis_sum += _money(p["basis"])
    mv = _c(mv_raw)
    equity = _c(cash + mv)
    unrealized = _c(mv - basis_sum)
    realized = _money(book["realized_pnl"])
    net = _c(equity - initial)
    return {"market_value": str(mv), "unrealized_pnl": str(unrealized),
            "equity": str(equity), "net_pnl": str(net)}


def _stored_money(value: Any) -> D:
    """Reject malformed or rounded-away precision in persisted cent amounts."""
    amount = _num(value)
    if not isinstance(value, str) or value != f"{amount:.2f}":
        raise LedgerError("stored money must be a canonical cent string")
    return amount


def reconcile(state: Mapping[str, Any]) -> None:
    s = copy.deepcopy(dict(state))
    books = s.get("books")
    if not isinstance(books, Mapping) or set(books) != set(BOOKS):
        raise LedgerError("state must have exactly research and intraday books")
    for bname in BOOKS:
        b = books[bname]
        if not isinstance(b, Mapping):
            raise LedgerError("book must be a mapping")
        initial = _stored_money(b.get("initial_cash"))
        if initial != D("10000.00"):
            raise LedgerError("initial_cash must be 10000.00")
        events = b.get("events")
        if not isinstance(events, list):
            raise LedgerError("events must be a list")
        seen_ids: set[str] = set()
        fees_total = D(0)
        realized_total = D(0)
        cash = initial
        open_pos: dict[str, dict[str, D]] = {}
        last_ts = None
        for e in events:
            if not isinstance(e, Mapping):
                raise LedgerError("event must be a mapping")
            sym = e.get("symbol")
            side = e.get("side")
            ts = e.get("timestamp")
            qty_raw = e.get("quantity")
            if side not in ("BUY", "SELL"):
                raise LedgerError("event side must be BUY or SELL")
            if isinstance(qty_raw, bool) or not isinstance(qty_raw, int) or qty_raw <= 0:
                raise LedgerError("event quantity must be a positive integer")
            qty = qty_raw
            if not isinstance(sym, str) or sym not in UNIVERSE:
                raise LedgerError("event symbol outside universe")
            eid = f"{bname}/{sym}/{ts}/{side}"
            if e.get("id") != eid:
                raise LedgerError("event id mismatch")
            if eid in seen_ids:
                raise LedgerError("duplicate event id")
            seen_ids.add(eid)
            if last_ts is not None:
                from contracts import aware as _aware
                prev_dt = _aware(last_ts)
                cur_dt = _aware(ts)
                if cur_dt < prev_dt:
                    raise LedgerError("events not chronological")
            last_cycle = s.get("last_cycle")
            if last_cycle is not None:
                from contracts import aware as _aware
                if _aware(ts) > _aware(last_cycle):
                    raise LedgerError("event after last_cycle")
            last_ts = ts
            fee = _stored_money(e.get("fee"))
            if fee != FEE:
                raise LedgerError("event fee must be exactly 0.50")
            fees_total += fee
            raw_raw = e.get("raw_price")
            fill_raw = e.get("fill_price")
            if raw_raw is None or fill_raw is None:
                raise LedgerError("event must provide raw_price and fill_price")
            raw = _num(raw_raw, price=True)
            fill = _num(fill_raw, price=True)
            expected_fill = _buy_fill(raw) if side == "BUY" else _sell_fill(raw)
            if fill != expected_fill:
                raise LedgerError("tampered fill price or slippage")
            declared_slip = e.get("slippage")
            if declared_slip is None:
                raise LedgerError("event missing slippage")
            slip = _num(declared_slip)
            if slip != _f4(abs(fill - raw) * qty):
                raise LedgerError("slippage mismatch")
            if side == "BUY":
                if sym in open_pos:
                    raise LedgerError("buy into existing position")
                if len(open_pos) >= 2:
                    raise LedgerError("max 2 positions exceeded")
                debit = _c(D(qty) * fill) + FEE
                if debit > initial * D("0.20"):
                    raise LedgerError("position notional exceeds 20% initial cash")
                entry, stop = fill, _sell_fill(_f4(raw * D("0.97")))
                if (entry - stop) * qty + FEE * 2 > initial * D("0.01"):
                    raise LedgerError("planned risk exceeds 1% initial cash")
                cash = _c(cash - debit)
                if cash < 0:
                    raise LedgerError("cash would go negative")
                open_pos[sym] = {"qty": D(qty), "basis": debit}
            else:
                pos = open_pos.get(sym)
                if pos is None or pos["qty"] != D(qty):
                    raise LedgerError("sell without full held quantity")
                credit = _c(D(qty) * fill) - FEE
                cash = _c(cash + credit)
                realized_total += _c(credit - pos["basis"])
                del open_pos[sym]
            declared_cash = _stored_money(e.get("cash_after"))
            if declared_cash != cash:
                raise LedgerError("cash_after chain mismatch")
        if _stored_money(b.get("fees")) != _c(fees_total):
            raise LedgerError("fees mismatch")
        if _stored_money(b.get("cash")) != cash:
            raise LedgerError("final cash mismatch")
        if _stored_money(b.get("realized_pnl")) != _c(realized_total):
            raise LedgerError("realized pnl mismatch")
        positions = b.get("positions")
        if not isinstance(positions, Mapping):
            raise LedgerError("positions must be a mapping")
        if set(positions) != set(open_pos):
            raise LedgerError("positions keys mismatch")
        for sym, pos in positions.items():
            if not isinstance(pos, Mapping):
                raise LedgerError("position must be a mapping")
            q = pos.get("quantity")
            if isinstance(q, bool) or not isinstance(q, int) or q <= 0:
                raise LedgerError("position quantity must be a positive integer")
            if D(q) != open_pos[sym]["qty"]:
                raise LedgerError("position quantity mismatch")
            basis = _stored_money(pos.get("basis"))
            if basis != open_pos[sym]["basis"]:
                raise LedgerError("position basis mismatch")
            avg = _num(pos.get("average_cost"))
            expected_avg = _f4(basis / q)
            if avg != expected_avg:
                raise LedgerError("average_cost must be exact entry basis / quantity at 4dp")
            lastmark = _num(pos.get("lastmark"), price=True)
            mark_ts = pos.get("mark_timestamp")
            lp = b.get("last_prices", {}).get(sym)
            if not isinstance(lp, Mapping):
                raise LedgerError("missing last_prices for held position")
            lp_price = _num(lp.get("price"), price=True)
            lp_ts = lp.get("timestamp")
            if lastmark != lp_price or mark_ts != lp_ts:
                raise LedgerError("position mark must match last_prices")
        totals = _totals(b)
        mv_raw = D(0)
        basis_sum = D(0)
        for pos in positions.values():
            q = int(pos["quantity"])
            mv_raw += _num(pos["lastmark"], price=True) * q
            basis_sum += _stored_money(pos["basis"])
        mv = _c(mv_raw)
        equity = _c(cash + mv)
        unrealized = _c(mv - basis_sum)
        net = _c(equity - initial)
        if totals["market_value"] != str(mv) or totals["equity"] != str(equity):
            raise LedgerError("totals market value or equity mismatch")
        if totals["unrealized_pnl"] != str(unrealized):
            raise LedgerError("totals unrealized pnl mismatch")
        if totals["net_pnl"] != str(net):
            raise LedgerError("totals net pnl mismatch")
        if net != _c(realized_total + unrealized):
            raise LedgerError("money identity mismatch")
        if _stored_money(b.get("cash")) != cash:
            raise LedgerError("cash mismatch")
        for name in ("market_value", "unrealized_pnl", "equity", "net_pnl"):
            if _stored_money(b.get(name)) != _num(totals[name]):
                raise LedgerError("stored metric mismatch: " + name)
        pending = b.get("pending")
        if not isinstance(pending, Mapping):
            raise LedgerError("pending must be a mapping")
        for sym, pend in pending.items():
            if not isinstance(sym, str) or sym not in UNIVERSE:
                raise LedgerError("pending symbol outside universe")
            if not isinstance(pend, Mapping):
                raise LedgerError("pending entry must be a mapping")
            action = pend.get("action")
            if action not in ("enter", "exit"):
                raise LedgerError("pending action must be enter or exit")
            created_at = pend.get("created_at")
            if created_at is None:
                raise LedgerError("pending created_at required")
            from contracts import aware as _aware
            if _aware(created_at) > _aware(s.get("last_cycle")):
                raise LedgerError("pending created_at after last_cycle")
            if action == "enter" and sym in positions:
                raise LedgerError("pending enter for held symbol")
            if action == "exit" and sym not in positions:
                raise LedgerError("pending exit for unheld symbol")
    if not isinstance(s.get("resources", {}).get("cycles", 0), int):
        raise LedgerError("bad resources")