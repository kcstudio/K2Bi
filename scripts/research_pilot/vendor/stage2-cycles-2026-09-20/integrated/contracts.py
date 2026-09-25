"""Shared validation contracts for the Stage 2 local simulation.

Pure local code: no broker, no network, no disk access. Every money value is a
Decimal; every timestamp is timezone-aware UTC. All validation helpers fail
closed by raising ValueError on any wrong type, null, malformed value, or
unknown key.
"""

from __future__ import annotations

import copy
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation

__all__ = [
    "UNIVERSE",
    "CIKS",
    "INDUSTRIES",
    "aware",
    "decimal_string",
    "validate_source",
    "validate_request",
]

UNIVERSE = ("G", "CALX", "CDNS", "SPY", "XLV")

CIKS = {"G": 1398659, "CALX": 1406666, "CDNS": 813672}

INDUSTRIES = {
    "G": "Business services",
    "CALX": "Telecom equipment",
    "CDNS": "Semiconductor software",
    "SPY": "Broad market ETF",
    "XLV": "Healthcare ETF",
}

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DECIMAL_RE = re.compile(r"^-?[0-9]{1,18}(\.[0-9]{1,8})?$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_PERIOD_KINDS = ("quarter", "annual")
_SOURCE_STATUS = ("ok", "missing", "failed")
_SOURCE_KEYS = (
    "status",
    "kind",
    "url",
    "retrieved_at",
    "sha256",
    "facts",
    "reason",
)
_FACT_KEYS = (
    "period_end",
    "filed",
    "period_kind",
    "revenue",
    "prior_revenue",
    "net_income",
)
_REQUEST_KEYS_FIXTURE = (
    "cycle_id",
    "source_mode",
    "synthetic_prices",
    "prices",
    "session_close",
    "fixture_sources",
)
_REQUEST_KEYS_SEC = (
    "cycle_id",
    "source_mode",
    "synthetic_prices",
    "prices",
    "session_close",
)
_MAX_PRICE = Decimal("1000000")
_QUARTER_MAX = 180
_ANNUAL_MAX = 450
_FILING_MAX = 120


def aware(value):
    """Return an aware UTC datetime.

    Accepts an ISO-8601 string or a datetime. Naive datetimes and strings
    without an explicit offset raise ValueError; no fallback timezone is
    assumed.
    """
    if isinstance(value, bool):
        raise ValueError("aware: boolean is not a timestamp")
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        if not value:
            raise ValueError("aware: empty timestamp string")
        text = value.strip()
        if text.endswith("Z") or text.endswith("z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except (TypeError, ValueError) as exc:
            raise ValueError("aware: unparseable ISO timestamp") from exc
    else:
        raise ValueError("aware: expected datetime or ISO string")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("aware: timestamp lacks timezone")
    return dt.astimezone(timezone.utc)


def decimal_string(value, positive=False):
    """Parse a strictly formatted plain decimal string into a Decimal.

    The string may carry an optional leading minus and at most 18 integer and
    8 fraction digits. No exponent, sign prefix other than minus, leading plus,
    whitespace, or booleans are accepted. When positive is true the value must
    be strictly greater than zero.
    """
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("decimal_string: expected string")
    if not _DECIMAL_RE.fullmatch(value):
        raise ValueError("decimal_string: malformed decimal string")
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("decimal_string: malformed decimal string") from exc
    if not result.is_finite():
        raise ValueError("decimal_string: non-finite decimal")
    if positive and result <= 0:
        raise ValueError("decimal_string: value must be positive")
    return result


def _check_exact_keys(mapping, expected, label):
    if not isinstance(mapping, dict):
        raise ValueError(f"{label}: expected mapping")
    keys = set(mapping.keys())
    if keys != set(expected):
        missing = set(expected) - keys
        extra = keys - set(expected)
        raise ValueError(
            f"{label}: key mismatch missing={sorted(str(k) for k in missing)} "
            f"extra={sorted(str(k) for k in extra)}"
        )


def _require_str(value, label):
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError(f"{label}: expected string")
    if not value:
        raise ValueError(f"{label}: empty string")
    return value


def _parse_date(value, label):
    text = _require_str(value, label)
    if not _DATE_RE.fullmatch(text):
        raise ValueError(f"{label}: expected YYYY-MM-DD")
    try:
        return date(int(text[0:4]), int(text[5:7]), int(text[8:10]))
    except ValueError as exc:
        raise ValueError(f"{label}: invalid calendar date") from exc


def _validate_facts(facts, symbol):
    _check_exact_keys(facts, _FACT_KEYS, f"facts[{symbol}]")
    period_end = _parse_date(facts["period_end"], f"facts[{symbol}].period_end")
    filed = _parse_date(facts["filed"], f"facts[{symbol}].filed")
    if period_end > filed:
        raise ValueError(f"facts[{symbol}]: period_end after filed")
    period_kind = facts["period_kind"]
    if period_kind not in _PERIOD_KINDS:
        raise ValueError(f"facts[{symbol}].period_kind: unknown value")
    revenue = decimal_string(facts["revenue"])
    prior_revenue = decimal_string(facts["prior_revenue"])
    net_income = decimal_string(facts["net_income"])
    if revenue < 0:
        raise ValueError(f"facts[{symbol}].revenue: negative")
    if prior_revenue <= 0:
        raise ValueError(f"facts[{symbol}].prior_revenue: must be positive")
    return period_end, filed, revenue, prior_revenue


def _source_url(symbol, kind):
    if kind == "fixture":
        return f"fixture://{symbol}"
    cik = CIKS.get(symbol)
    if cik is None:
        raise ValueError(f"url[{symbol}]: no SEC mapping for symbol")
    return f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


def validate_source(symbol, record, asof):
    """Validate a single source record and return flat evidence.

    Raises ValueError for any invalid, missing, or failed evidence; those
    conditions are surfaced so research can pause rather than invent data.
    """
    if symbol not in UNIVERSE:
        raise ValueError(f"symbol: {symbol!r} outside UNIVERSE")
    asof_dt = aware(asof)
    _check_exact_keys(record, _SOURCE_KEYS, f"source[{symbol}]")

    status = record["status"]
    if status not in _SOURCE_STATUS:
        raise ValueError(f"source[{symbol}].status: unknown status")
    kind = record["kind"]
    if kind not in ("fixture", "sec-companyfacts"):
        raise ValueError(f"source[{symbol}].kind: unknown kind")
    reason = _require_str(record["reason"], f"source[{symbol}].reason")

    if status != "ok":
        raise ValueError(f"source[{symbol}]: status={status} reason={reason}")

    url = _require_str(record["url"], f"source[{symbol}].url")
    expected_url = _source_url(symbol, kind)
    if url != expected_url:
        raise ValueError(f"source[{symbol}].url: expected {expected_url}")

    sha256 = record["sha256"]
    if isinstance(sha256, bool) or not isinstance(sha256, str):
        raise ValueError(f"source[{symbol}].sha256: expected string")
    if not _SHA256_RE.fullmatch(sha256):
        raise ValueError(f"source[{symbol}].sha256: expected 64 lowercase hex")

    retrieved_at = aware(record["retrieved_at"])
    if retrieved_at > asof_dt:
        raise ValueError(f"source[{symbol}].retrieved_at: after as-of")

    period_end, filed, revenue, prior_revenue = _validate_facts(record["facts"], symbol)

    if filed > asof_dt.date():
        raise ValueError(f"source[{symbol}].facts.filed: after as-of date")
    if period_end > asof_dt.date():
        raise ValueError(f"source[{symbol}].facts.period_end: after as-of date")

    age_filing = (asof_dt.date() - filed).days
    age_period = (asof_dt.date() - period_end).days
    if age_filing > _FILING_MAX:
        raise ValueError(f"source[{symbol}]: filing age {age_filing} exceeds limit")
    period_kind = record["facts"]["period_kind"]
    period_limit = _QUARTER_MAX if period_kind == "quarter" else _ANNUAL_MAX
    if age_period > period_limit:
        raise ValueError(f"source[{symbol}]: period age {age_period} exceeds limit")

    growth = revenue / prior_revenue - Decimal(1)

    evidence = {
        "symbol": symbol,
        "industry": INDUSTRIES[symbol],
        "status": status,
        "kind": kind,
        "url": url,
        "retrieved_at": retrieved_at.isoformat(),
        "sha256": sha256,
        "reason": reason,
        "period_kind": period_kind,
        "filing_age_days": age_filing,
        "period_age_days": age_period,
        "growth": str(growth),
    }
    evidence.update(record["facts"])
    return evidence


def _validate_prices(prices):
    if not isinstance(prices, dict):
        raise ValueError("prices: expected mapping")
    normalized = {}
    for symbol, raw in prices.items():
        if symbol not in UNIVERSE:
            raise ValueError(f"prices: symbol {symbol!r} outside UNIVERSE")
        value = decimal_string(raw, positive=True)
        if value > _MAX_PRICE:
            raise ValueError(f"prices[{symbol}]: exceeds maximum")
        normalized[symbol] = raw if isinstance(raw, str) else str(raw)
    return normalized


def _validate_fixture_sources(sources):
    if not isinstance(sources, dict):
        raise ValueError("fixture_sources: expected mapping")
    if set(sources.keys()) != set(UNIVERSE):
        raise ValueError("fixture_sources: must map exactly UNIVERSE")
    normalized = {}
    for symbol in UNIVERSE:
        row = sources[symbol]
        if not isinstance(row, dict):
            raise ValueError(f"fixture_sources[{symbol}]: expected mapping")
        normalized[symbol] = copy.deepcopy(row)
    return normalized


def validate_request(req):
    """Validate and normalize a cycle request without mutating the caller.

    Returns a deep-copied dict with a canonical UTC cycle_id. Content of
    source rows is intentionally not validated here; research is responsible
    for durable abstention on bad facts.
    """
    if not isinstance(req, dict):
        raise ValueError("request: expected mapping")

    source_mode = req.get("source_mode")
    if source_mode not in ("fixture", "sec"):
        raise ValueError("request.source_mode: expected fixture or sec")

    if isinstance(req.get("cycle_id"), bool) or not isinstance(req.get("cycle_id"), str):
        raise ValueError("request.cycle_id: expected ISO-8601 string")

    expected_keys = _REQUEST_KEYS_FIXTURE if source_mode == "fixture" else _REQUEST_KEYS_SEC
    _check_exact_keys(req, expected_keys, "request")

    cycle_id = aware(req["cycle_id"]).isoformat()

    if req["synthetic_prices"] is not True:
        raise ValueError("request.synthetic_prices: must be true")

    prices = _validate_prices(req["prices"])

    if not isinstance(req["session_close"], bool):
        raise ValueError("request.session_close: must be boolean")
    session_close = req["session_close"]

    normalized = {
        "cycle_id": cycle_id,
        "source_mode": source_mode,
        "synthetic_prices": True,
        "prices": prices,
        "session_close": session_close,
    }

    if source_mode == "sec":
        if prices:
            raise ValueError("request.prices: must be empty in sec mode")
    else:
        normalized["fixture_sources"] = _validate_fixture_sources(req["fixture_sources"])

    return normalized
