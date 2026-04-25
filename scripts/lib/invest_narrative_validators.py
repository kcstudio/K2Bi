"""Four hardcoded validators for invest-narrative Ship 2.

- ticker_exists (against canonical registry)
- market_cap >= $2B
- liquidity (30-day average daily dollar volume) >= $10M
- priced-in flag (>90% gain in 90 days, flag only, no auto-reject)
- citation_url HTTP-HEAD validation
"""

from __future__ import annotations

import urllib.request
from typing import Any

import yfinance as yf

from scripts.lib.canonical_ticker_registry import load_registry


class ValidatorSkipped(Exception):
    """Raised when a validator cannot reach its external data source.

    The pipeline catches this and records ``validator_skipped`` in the
    theme file rather than auto-rejecting the candidate.
    """

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def validate_ticker_exists(symbol: str, registry: dict[str, Any] | None = None) -> bool:
    """Return True if ``symbol`` is present in the canonical NASDAQ+NYSE registry.

    Lookup is case-insensitive.
    """
    if registry is None:
        registry = load_registry()
    sym_upper = symbol.upper()
    return any(k.upper() == sym_upper for k in registry)


def validate_market_cap(symbol: str, threshold: int = 2_000_000_000) -> bool:
    """Return True if market cap >= ``threshold``.

    Raises ``ValidatorSkipped`` when yfinance is unreachable or returns
    empty data so the pipeline can record the skip instead of rejecting.
    """
    try:
        ticker = yf.Ticker(symbol)
        mc = None
        try:
            info = ticker.info
            mc = info.get("marketCap") if isinstance(info, dict) else None
        except Exception:
            mc = None
        if mc is None:
            # fast_info is a lightweight fallback on newer yfinance builds
            try:
                fi = ticker.fast_info
                mc = getattr(fi, "market_cap", None)
            except Exception:
                mc = None
        if mc is None:
            raise ValidatorSkipped("market cap unavailable from yfinance")
        return bool(mc >= threshold)
    except ValidatorSkipped:
        raise
    except Exception as exc:
        raise ValidatorSkipped(f"yfinance error: {exc}") from exc


def validate_liquidity(symbol: str, adv_threshold: int = 10_000_000) -> bool:
    """Return True if 30-day average daily dollar volume >= ``adv_threshold``.

    Dollar volume = close price * share volume, averaged over the last
    30 trading days. Raises ``ValidatorSkipped`` on yfinance failure.
    """
    try:
        ticker = yf.Ticker(symbol)
        # Request ~45 calendar days to ensure at least 30 trading days
        hist = ticker.history(period="45d")
        if hist is None or hist.empty:
            raise ValidatorSkipped("volume history unavailable from yfinance")
        if "Close" not in hist.columns or "Volume" not in hist.columns:
            raise ValidatorSkipped("volume history missing required columns")
        # Use the most recent 30 trading days
        hist = hist.tail(30)
        dollar_volume = hist["Close"] * hist["Volume"]
        avg_dv = float(dollar_volume.mean())
        return bool(avg_dv >= adv_threshold)
    except ValidatorSkipped:
        raise
    except Exception as exc:
        raise ValidatorSkipped(f"yfinance error: {exc}") from exc


def validate_priced_in(
    symbol: str, gain_threshold: float = 0.9, lookback_days: int = 90
) -> dict[str, Any]:
    """Flag (but do NOT auto-reject) if price is up > ``gain_threshold``
    over ``lookback_days``.

    Returns a dict:
        flagged  -> bool
        gain     -> float | None
        skipped  -> bool
        reason   -> str
    """
    try:
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period=f"{lookback_days}d")
        if hist is None or hist.empty or len(hist) < 2:
            return {"flagged": False, "gain": None, "skipped": True, "reason": "price history unavailable"}
        if "Close" not in hist.columns:
            return {"flagged": False, "gain": None, "skipped": True, "reason": "price history missing Close column"}
        start = float(hist["Close"].iloc[0])
        end = float(hist["Close"].iloc[-1])
        if start == 0:
            return {"flagged": False, "gain": None, "skipped": True, "reason": "zero start price"}
        gain = (end - start) / start
        return {"flagged": bool(gain >= gain_threshold), "gain": gain, "skipped": False, "reason": ""}
    except Exception as exc:
        return {"flagged": False, "gain": None, "skipped": True, "reason": str(exc)}


def validate_citation_url(url: str, timeout: int = 5) -> bool:
    """HTTP HEAD request with redirect following.

    Accepts HTTP 200-299. Falls back to GET on any HEAD failure,
    since many hosts block HEAD but serve GET normally.
    Drops on persistent 4xx/5xx/timeout/network error.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36"
        )
    }
    try:
        req = urllib.request.Request(url, method="HEAD", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:
        # Fallback to GET on any HEAD failure (HTTPError, URLError, timeout, TLS, etc.)
        try:
            req = urllib.request.Request(url, method="GET", headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return 200 <= resp.status < 300
        except Exception:
            return False
