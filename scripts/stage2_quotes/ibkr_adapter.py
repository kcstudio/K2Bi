"""Offline, read-only parser for one saved IBKR paper Gateway daily-bar response.

Display-only. No network, broker connection, filesystem, environment, logging,
order APIs, or trading. The caller is responsible for obtaining the raw bytes
separately (see ``scripts/gateway-query.sh``) using a leased operator client ID
with ``readonly=True``.

Synthetic ledger note: the returned record must NOT be injected into the current
synthetic ledger. It exists purely for human inspection.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

import exchange_calendars as xcals

MAX_BYTES = 1 * 1024 * 1024

_TOP_KEYS = frozenset(
    {
        "status",
        "symbol",
        "source",
        "contract_id",
        "currency",
        "resolution",
        "market_data_type_requested",
        "market_data_type_observed",
        "connection_mode",
        "received_at",
        "bars",
    }
)
_BAR_KEYS = frozenset({"session_date", "close", "currency"})

_EXPECTED_STATUS = "bars_returned"
_EXPECTED_SOURCE = "IBKR paper Gateway"
_EXPECTED_RESOLUTION = "historical daily bar"
_EXPECTED_MDT_REQUESTED = "delayed"
_EXPECTED_MDT_OBSERVED = "unverified"
_EXPECTED_CONNECTION_MODE = "readonly"
_EXPECTED_CURRENCY = "USD"

_MAX_PRICE = Decimal("1000000")
_MIN_BARS = 1
_MAX_BARS = 5
_CLOSE_RE = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,8})?\Z")


def _reject_duplicate_keys(pairs):
    seen: set[str] = set()
    for key, _value in pairs:
        if key in seen:
            raise ValueError(f"duplicate JSON key: {key!r}")
        seen.add(key)
    return dict(pairs)


def _parse_received_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("received_at must be a string")
    text = value.strip()
    if not text:
        raise ValueError("received_at must be non-empty")
    # ``datetime.fromisoformat`` accepts '+00:00' and 'Z' (3.11+); be explicit.
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"received_at is not a valid ISO timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("received_at must carry an explicit timezone offset")
    return parsed


def _parse_as_of(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("as_of must be a string")
    text = value.strip()
    if not text:
        raise ValueError("as_of must be non-empty")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"as_of is not a valid ISO timestamp: {exc}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("as_of must carry an explicit timezone offset")
    return parsed


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_session_date(value: object) -> date:
    if not isinstance(value, str):
        raise ValueError("session_date must be a string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"session_date is not YYYY-MM-DD: {exc}") from exc
    if parsed.isoformat() != value:
        raise ValueError("session_date must use canonical YYYY-MM-DD")
    return parsed


def _parse_close(value: object) -> str:
    if not isinstance(value, str) or not _CLOSE_RE.fullmatch(value):
        raise ValueError("close must be a plain decimal string")
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"close is not a valid decimal: {exc}") from exc
    if not dec.is_finite():
        raise ValueError("close must be finite")
    if dec <= 0:
        raise ValueError("close must be positive")
    if dec > _MAX_PRICE:
        raise ValueError("close exceeds 1,000,000")
    return value


def _completed_sessions_as_of(
    calendar, as_of_dt: datetime
) -> list[date]:
    """Return XNYS session dates whose close is strictly before ``as_of_dt``."""
    as_of_utc = as_of_dt.astimezone(timezone.utc)
    as_of_date = as_of_utc.date()
    # Look back a bounded window; the caller only needs up to a few sessions.
    start = as_of_date - timedelta(days=21)
    sessions = calendar.sessions_in_range(
        start.isoformat(), as_of_date.isoformat()
    )
    completed: list[date] = []
    for session in sessions:
        session_date = session.date()
        close_ts = calendar.session_close(session).tz_convert("UTC")
        if close_ts.to_pydatetime() <= as_of_utc:
            completed.append(session_date)
    return completed


def _latest_completed_session(
    calendar, as_of_dt: datetime
) -> date:
    completed = _completed_sessions_as_of(calendar, as_of_dt)
    if not completed:
        raise ValueError("no completed XNYS sessions before as_of")
    return completed[-1]


def _count_completed_sessions_between(
    calendar, start: date, end: date
) -> int:
    """Number of XNYS sessions strictly after ``start`` up to and including ``end``."""
    if end < start:
        return -1
    sessions = calendar.sessions_in_range(start.isoformat(), end.isoformat())
    dates = [s.date() for s in sessions]
    if start in dates:
        dates = [d for d in dates if d > start]
    return len([d for d in dates if d <= end])


def parse_ibkr_daily_check(
    raw: bytes,
    *,
    expected_symbol: str,
    as_of: str,
    max_session_lag: int = 1,
) -> dict[str, str | bool]:
    """Parse one saved IBKR paper Gateway daily-bar response (read-only).

    Returns a canonical, display-only record for the latest bar. Raises
    ``ValueError`` on any malformed or stale input. Does NOT touch the
    synthetic ledger.
    """
    if not isinstance(raw, bytes):
        raise ValueError("raw must be bytes")
    if len(raw) == 0:
        raise ValueError("raw must be non-empty")
    if len(raw) > MAX_BYTES:
        raise ValueError("raw exceeds 1 MiB")

    if not isinstance(expected_symbol, str) or not expected_symbol:
        raise ValueError("expected_symbol must be a non-empty string")
    if expected_symbol != expected_symbol.strip():
        raise ValueError("expected_symbol must not contain surrounding whitespace")
    if expected_symbol.upper() != expected_symbol or not expected_symbol.isalpha():
        raise ValueError("expected_symbol must be an uppercase US ticker")
    if len(expected_symbol) > 6:
        raise ValueError("expected_symbol is too long")

    if not isinstance(max_session_lag, int) or isinstance(max_session_lag, bool):
        raise ValueError("max_session_lag must be a non-negative int")
    if not 0 <= max_session_lag <= 5:
        raise ValueError("max_session_lag must be between 0 and 5")

    as_of_dt = _parse_as_of(as_of)

    try:
        text = bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"raw is not UTF-8: {exc}") from exc

    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except ValueError as exc:
        raise ValueError(f"malformed JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError("top-level JSON must be an object")
    if set(data.keys()) != _TOP_KEYS:
        raise ValueError("top-level keys do not match expected schema")

    if data["status"] != _EXPECTED_STATUS:
        raise ValueError(f"unexpected status: {data['status']!r}")
    if data["symbol"] != expected_symbol:
        raise ValueError(
            f"symbol mismatch: response={data['symbol']!r} expected={expected_symbol!r}"
        )
    if data["source"] != _EXPECTED_SOURCE:
        raise ValueError(f"unexpected source: {data['source']!r}")
    if data["resolution"] != _EXPECTED_RESOLUTION:
        raise ValueError(f"unexpected resolution: {data['resolution']!r}")
    if data["market_data_type_requested"] != _EXPECTED_MDT_REQUESTED:
        raise ValueError("market_data_type_requested must be 'delayed'")
    if data["market_data_type_observed"] != _EXPECTED_MDT_OBSERVED:
        raise ValueError("market_data_type_observed must be 'unverified'")
    if data["connection_mode"] != _EXPECTED_CONNECTION_MODE:
        raise ValueError("connection_mode must be 'readonly'")
    if data["currency"] != _EXPECTED_CURRENCY:
        raise ValueError("top-level currency must be 'USD'")

    contract_id = data["contract_id"]
    if not isinstance(contract_id, int) or isinstance(contract_id, bool):
        raise ValueError("contract_id must be an integer, not a bool")
    if contract_id <= 0:
        raise ValueError("contract_id must be positive")

    received_at_dt = _parse_received_at(data["received_at"])
    if received_at_dt > as_of_dt:
        raise ValueError("received_at must not be after as_of")

    bars = data["bars"]
    if not isinstance(bars, list):
        raise ValueError("bars must be a list")
    if len(bars) < _MIN_BARS or len(bars) > _MAX_BARS:
        raise ValueError(f"bars count must be {_MIN_BARS}-{_MAX_BARS}")

    parsed_bars: list[tuple[date, str]] = []
    seen_dates: set[date] = set()
    previous: date | None = None
    for index, bar in enumerate(bars):
        if not isinstance(bar, dict):
            raise ValueError(f"bar {index} must be an object")
        if set(bar.keys()) != _BAR_KEYS:
            raise ValueError(f"bar {index} keys do not match expected schema")
        if bar["currency"] != _EXPECTED_CURRENCY:
            raise ValueError(f"bar {index} currency must be 'USD'")
        session_date = _parse_session_date(bar["session_date"])
        if session_date in seen_dates:
            raise ValueError(f"duplicate session_date: {session_date}")
        if previous is not None and session_date <= previous:
            raise ValueError("session dates must be strictly increasing")
        seen_dates.add(session_date)
        previous = session_date
        close = _parse_close(bar["close"])
        parsed_bars.append((session_date, close))

    calendar = xcals.get_calendar("XNYS")

    as_of_utc = as_of_dt.astimezone(timezone.utc)
    as_of_date = as_of_utc.date()
    received_utc = received_at_dt.astimezone(timezone.utc)

    latest_session = parsed_bars[-1][0]
    if latest_session > as_of_date:
        raise ValueError("bar session_date is in the future")

    # Every bar must be completed (session close <= received/as_of window).
    for session_date, _close in parsed_bars:
        try:
            session_ts = calendar.date_to_session(session_date.isoformat())
            close_ts = calendar.session_close(session_ts).tz_convert("UTC")
        except Exception as exc:  # exchange_calendars raises several types
            raise ValueError(f"session_date not an XNYS session: {session_date}") from exc
        if close_ts.to_pydatetime() > received_utc:
            raise ValueError(
                f"bar session {session_date} had not closed when received"
            )

    try:
        last_completed = _latest_completed_session(calendar, as_of_dt)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    lag = _count_completed_sessions_between(
        calendar, latest_session, last_completed
    )
    if lag < 0:
        raise ValueError("latest bar session is after latest completed session")
    if lag > max_session_lag:
        raise ValueError(
            f"latest bar is {lag} completed XNYS sessions stale "
            f"(max_session_lag={max_session_lag})"
        )

    raw_sha256 = hashlib.sha256(bytes(raw)).hexdigest()

    return {
        "symbol": expected_symbol,
        "source": "ibkr_gateway",
        "source_ref": f"ibkr:contract/{contract_id}",
        "session_date": latest_session.isoformat(),
        "retrieved_at": _utc_iso(received_at_dt),
        "currency": _EXPECTED_CURRENCY,
        "close": parsed_bars[-1][1],
        "adjustment_policy": "unverified",
        "market_data_type": "unverified",
        "raw_sha256": raw_sha256,
        "resolution": "daily",
        "trade_eligible": False,
    }
