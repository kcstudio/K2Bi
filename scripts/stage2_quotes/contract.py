"""Pure offline validation for Stage 2 daily bar records.

The public entry point is :func:`validate_daily_bar`, which validates a
mapping-shaped daily bar against a fixed contract.  It performs no network,
file, clock, or broker I/O and raises :class:`ValueError` on any contract
violation.
"""

from __future__ import annotations

import datetime as _dt
import re
from decimal import Decimal, InvalidOperation
from typing import Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import exchange_calendars as _xc

_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,9}\Z")
_SOURCE_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_CLOSE_RE = re.compile(r"[0-9]{1,7}(?:\.[0-9]{1,8})?\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_REQUIRED_KEYS = frozenset(
    {
        "symbol",
        "source",
        "source_url",
        "session_date",
        "retrieved_at",
        "currency",
        "close",
        "adjusted",
        "raw_sha256",
    }
)


def validate_daily_bar(
    record: Mapping[str, object],
    *,
    expected_symbol: str,
    as_of: str,
    allowed_hosts: set[str],
    max_session_lag: int = 1,
) -> dict[str, str | bool]:
    """Validate a Stage 2 daily bar and return canonical values.

    The input mapping must have exactly the required keys.  The function
    validates symbol, source, URL and host allow-list, session date against the
    XNYS calendar, UTC retrieval time relative to ``as_of`` and the session
    close, currency, adjustment flag, close decimal string, and raw SHA-256.

    Args:
        record: Mapping containing exactly the daily bar contract keys.
        expected_symbol: Uppercase market symbol expected in the record.
        as_of: ISO timestamp with explicit offset used as the evaluation time.
        allowed_hosts: Nonempty set of exact HTTPS hostnames allowed for
            ``source_url``.
        max_session_lag: Maximum allowed completed XNYS session lag, 0 to 5.

    Returns:
        Canonical string and bool values for the validated fields.

    Raises:
        ValueError: If any field or relationship violates the contract.
    """
    if not isinstance(record, Mapping):
        raise ValueError("record must be a mapping")

    keys = set(record.keys())
    if keys != _REQUIRED_KEYS:
        missing = sorted(_REQUIRED_KEYS - keys)
        extra = sorted(keys - _REQUIRED_KEYS)
        raise ValueError(f"record keys invalid; missing={missing!r} extra={extra!r}")

    symbol = record["symbol"]
    if not isinstance(symbol, str) or not _SYMBOL_RE.fullmatch(symbol):
        raise ValueError("symbol must match [A-Z][A-Z0-9.]{0,9}")
    if not isinstance(expected_symbol, str) or not _SYMBOL_RE.fullmatch(expected_symbol):
        raise ValueError("expected_symbol must match [A-Z][A-Z0-9.]{0,9}")
    if symbol != expected_symbol:
        raise ValueError("symbol does not match expected_symbol")

    source = record["source"]
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise ValueError("source must be a lowercase ASCII slug")

    if not isinstance(allowed_hosts, set) or not allowed_hosts:
        raise ValueError("allowed_hosts must be a nonempty set")
    for host in allowed_hosts:
        if not isinstance(host, str) or not host or host != host.lower():
            raise ValueError("allowed_hosts entries must be nonempty lowercase strings")

    source_url = record["source_url"]
    if not isinstance(source_url, str) or not source_url:
        raise ValueError("source_url must be a nonempty string")
    if (source_url != source_url.strip() or "?" in source_url or "#" in source_url
            or any(ord(char) < 33 or ord(char) == 127 for char in source_url)):
        raise ValueError("source_url must not contain whitespace, control characters, query or fragment")
    try:
        parts = urlsplit(source_url)
    except Exception as exc:  # pragma: no cover - urlsplit is normally total
        raise ValueError("source_url is not parseable") from exc
    if parts.scheme != "https":
        raise ValueError("source_url scheme must be https")
    if parts.username is not None or parts.password is not None:
        raise ValueError("source_url must not contain userinfo")
    if parts.port is not None:
        raise ValueError("source_url must not contain a port")
    if parts.query or parts.fragment:
        raise ValueError("source_url must not contain query or fragment")
    host = parts.hostname
    if host is None or host not in allowed_hosts:
        raise ValueError("source_url hostname is not allowed")

    session_date = record["session_date"]
    if not isinstance(session_date, str) or not _DATE_RE.fullmatch(session_date):
        raise ValueError("session_date must be ISO YYYY-MM-DD")
    try:
        session_day = _dt.date.fromisoformat(session_date)
    except ValueError as exc:
        raise ValueError("session_date must be a real ISO date") from exc

    try:
        calendar = _xc.get_calendar("XNYS")
        if not calendar.is_session(session_day):
            raise ValueError("session_date is not an XNYS session")
        session = calendar.date_to_session(session_day, direction="none")
        close_utc = calendar.session_close(session).to_pydatetime().astimezone(_dt.timezone.utc)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("XNYS calendar validation failed") from exc

    if not isinstance(max_session_lag, int) or isinstance(max_session_lag, bool):
        raise ValueError("max_session_lag must be an integer")
    if max_session_lag < 0 or max_session_lag > 5:
        raise ValueError("max_session_lag must be between 0 and 5")

    retrieved_at = _parse_iso_with_offset(record["retrieved_at"], "retrieved_at")
    as_of_dt = _parse_iso_with_offset(as_of, "as_of")

    if retrieved_at > as_of_dt:
        raise ValueError("retrieved_at may not be after as_of")
    if retrieved_at < close_utc:
        raise ValueError("retrieved_at may not be before the XNYS session close")

    try:
        as_of_ny_day = as_of_dt.astimezone(ZoneInfo("America/New_York")).date()
        as_of_session = calendar.date_to_session(as_of_ny_day, direction="previous")
        as_of_close = calendar.session_close(as_of_session).to_pydatetime().astimezone(
            _dt.timezone.utc
        )
        if as_of_dt < as_of_close:
            as_of_session = calendar.previous_session(as_of_session)
        if session > as_of_session:
            raise ValueError("session_date is a future XNYS session relative to as_of")
        lag = int(calendar.sessions_in_range(session, as_of_session).size) - 1
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("XNYS session lag validation failed") from exc

    if lag < 0:
        raise ValueError("session_date is in the future relative to as_of")
    if lag > max_session_lag:
        raise ValueError("session_date is stale relative to as_of")

    currency = record["currency"]
    if currency != "USD":
        raise ValueError("currency must be exactly USD")

    adjusted = record["adjusted"]
    if not isinstance(adjusted, bool):
        raise ValueError("adjusted must be a bool")

    close = record["close"]
    if not isinstance(close, str) or not _CLOSE_RE.fullmatch(close):
        raise ValueError("close must be a decimal string without exponent")
    try:
        close_value = Decimal(close)
    except InvalidOperation as exc:
        raise ValueError("close must be a valid decimal string") from exc
    if not close_value.is_finite():
        raise ValueError("close must be finite")
    if close_value <= 0:
        raise ValueError("close must be greater than zero")
    if close_value > Decimal("1000000"):
        raise ValueError("close must not exceed 1000000")

    raw_sha256 = record["raw_sha256"]
    if not isinstance(raw_sha256, str) or not _SHA256_RE.fullmatch(raw_sha256):
        raise ValueError("raw_sha256 must be 64 lowercase hex characters")

    return {
        "symbol": symbol,
        "source": source,
        "source_url": source_url,
        "session_date": session_date,
        "retrieved_at": retrieved_at.isoformat(),
        "currency": "USD",
        "close": format(close_value, "f"),
        "adjusted": adjusted,
        "raw_sha256": raw_sha256,
    }


def _parse_iso_with_offset(value: object, name: str) -> _dt.datetime:
    """Parse an ISO timestamp that has an explicit UTC offset."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be an ISO timestamp string")
    try:
        parsed = _dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid ISO timestamp") from exc
    if parsed.utcoffset() is None:
        raise ValueError(f"{name} must have an explicit UTC offset")
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must have timezone information")
    return parsed.astimezone(_dt.timezone.utc)
