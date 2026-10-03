"""Offline action card presentation for stage 3.

Pure presentation only: never validates accounts, never executes, never
imports the engine or places orders. Inputs are already strictly parsed by
reference.verify_reference / cash_readiness / snapshot.parse_snapshot.
"""
from __future__ import annotations

import hashlib
import re

import yaml

from scripts.stage3_paper import snapshot

MAX_CONFIG_BYTES = 1024 * 1024
_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}$")
_EXAMPLE_KEYS = {
    "origin",
    "symbol",
    "side",
    "quantity",
    "reference_price",
    "stop_price",
    "reason",
}


def _limit_map(raw):
    if raw is None:
        return None
    if not isinstance(raw, (bytes, bytearray)):
        raise ValueError("config_raw must be bytes")
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError("config_raw exceeds 1 MiB")
    try:
        loaded = yaml.safe_load(bytes(raw))
    except yaml.YAMLError as exc:
        raise ValueError("invalid config YAML") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ValueError("config must be a YAML mapping")
    return {str(key): loaded[key] for key in loaded}


def _config(raw):
    if raw is None:
        return None
    limits = _limit_map(raw)
    digest = hashlib.sha256(bytes(raw)).hexdigest()
    return {"sha256": digest, "limits": limits}


def _usd_rows(cash):
    if not isinstance(cash, dict):
        return {} if cash is None else None
    rows = cash.get("rows")
    if rows is None:
        return None
    if not isinstance(rows, list):
        return None
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        currency = row.get("currency")
        if isinstance(currency, str):
            out[currency.upper()] = row.get("value")
    return out


def _match_account(reference, cash, holdings):
    ids = []
    for source in (cash, holdings):
        if isinstance(source, dict):
            account = source.get("account_id")
            if isinstance(account, str) and account:
                ids.append(account)
    if not ids:
        return None, None
    if any(account != snapshot.EXPECTED_ACCOUNT for account in ids):
        raise ValueError("unexpected paper account")
    if len(set(ids)) != 1:
        raise ValueError("account mismatch between cash and holdings")
    return ids[0], ids[0]


def _cash_check(cash):
    if cash is None:
        return ("unknown", "Cash snapshot not supplied; settled funding is unverified.")
    if not isinstance(cash, dict):
        return ("unknown", "Cash snapshot shape is unrecognized.")
    rows = _usd_rows(cash)
    if rows is None or "USD" not in rows:
        return ("unknown", "BASE currency is not USD; funding cannot be assumed.")
    raw = rows["USD"]
    try:
        value = snapshot._decimal(raw, "USD", allow_zero=True)
    except ValueError:
        return ("unknown", "USD row is not a plain positive-or-zero decimal string.")
    if snapshot.Decimal(value) < 0:
        return (
            "negative",
            "USD cash is a debit balance, not profit or loss. "
            "HKD is not automatically USD. Captured cash is not settled funding proof now.",
        )
    freshness = cash.get("freshness")
    if freshness == "fresh":
        return ("ok", "USD cash captured fresh; settled balance still unconfirmed.")
    if freshness == "stale":
        return ("stale", "USD cash known at capture time but stale, not funding proof now.")
    return ("unknown", "USD cash freshness is unknown.")


def _history_check(reference):
    if reference is None:
        return ("unknown", "No historical reference supplied.")
    if not isinstance(reference, dict):
        return ("unknown", "Historical reference shape is unrecognized.")
    status = reference.get("reference_status")
    if status == "verified_historical_reference" and reference.get("reference_eligible"):
        return ("ok", "Historical reference verified; not executable and not current.")
    return ("stale", "Unverified or stale reference; never executable.")


def _holdings_check(holdings):
    if holdings is None:
        return ("unknown", "Holdings snapshot not supplied.")
    if not isinstance(holdings, dict):
        return ("unknown", "Holdings snapshot shape is unrecognized.")
    freshness = holdings.get("freshness")
    if freshness == "fresh":
        return ("ok", "Holdings snapshot is fresh.")
    if freshness == "stale":
        return ("stale", "Holdings snapshot is stale.")
    return ("unknown", "Holdings freshness is unknown.")


def _checks(reference, cash, holdings, config, account_readiness=None):
    cash_status, cash_detail = _cash_check(cash)
    history_status, history_detail = _history_check(reference)
    holdings_status, holdings_detail = _holdings_check(holdings)
    risk_detail = "Risk checks not run; actual validators have not executed."
    if config is not None:
        risk_detail += " Config limits were hashed, not evaluated."
    checks = [
        {"label": "historical_reference", "status": history_status, "detail": history_detail},
        {"label": "holdings_freshness", "status": holdings_status, "detail": holdings_detail},
        {"label": "usd_cash", "status": cash_status, "detail": cash_detail},
        {
            "label": "account_ready",
            "status": "unknown",
            "detail": "Broker reliability flag at capture: " + str(cash.get("account_ready") if cash else None) + "; not a settled funding check.",
        },
        {
            "label": "settled_cash",
            "status": "unknown",
            "detail": "Settled cash is unconfirmed.",
        },
        {
            "label": "pending_orders",
            "status": "unknown",
            "detail": "Pending orders are unknown.",
        },
        {
            "label": "current_recommendation",
            "status": "unknown",
            "detail": "No verified current recommendation, current quote or approved paper mandate is present.",
        },
        {"label": "risk", "status": "not_run", "detail": risk_detail},
    ]


    for check in checks:
        if check['label'] == 'settled_cash' and account_readiness is not None:
            check['detail'] = 'Settled USD funding is unknown. Reported settlement totals retain their currency and unverified scope; HKD and BASE are not USD proof.'
        if check['label'] == 'pending_orders' and account_readiness is not None:
            state = account_readiness['orders_state']
            check['status'] = state
            check['detail'] = ('No visible API orders at capture.' if state == 'no_visible_api_orders' else 'Visible API orders at capture.' if state == 'visible_api_orders' else 'API order coverage is unknown.') + ' Manual/user order coverage and reserved commitments remain unverified; this is not permission to trade.'
    checks.append({'label': 'trading_restrictions', 'status': 'unknown', 'detail': 'Trading permissions and account restrictions are unverified. AccountReady is a broker reliability flag, not approval.'})
    return checks


def _wait(reason, account, checks, config):
    return {
        "status": "WAIT",
        "reason": reason,
        "next_step": (
            "Review currency exposure and settled cash, then supply current "
            "research and risk context. No FX recommendation or permission is made."
        ),
        "account": account,
        "candidate": None,
        "quantity": None,
        "estimated_cost": None,
        "estimated_risk": None,
        "checks": checks,
        "risk_config": config,
        "executable": False,
        "fill_eligible": False,
    }


def _example_card(example, config):
    if not isinstance(example, dict):
        raise ValueError("example must be a mapping")
    if example.get("origin") != "fixture":
        raise ValueError("example origin must be 'fixture'")
    if set(example) != _EXAMPLE_KEYS:
        raise ValueError("example keys must match exactly")
    symbol = example.get("symbol")
    if not isinstance(symbol, str) or not _SYMBOL_RE.match(symbol):
        raise ValueError("symbol must be 1..6 ASCII uppercase letters")
    side = example.get("side")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    quantity = example.get("quantity")
    if isinstance(quantity, bool) or not isinstance(quantity, int):
        raise ValueError("quantity must be an int")
    if quantity <= 0 or quantity > 1000000:
        raise ValueError("quantity out of range")
    reason = example.get("reason")
    if not isinstance(reason, str) or not reason:
        raise ValueError("reason must be a non-empty string")
    price = snapshot._decimal(example.get("reference_price"), "reference_price", nonnegative=True, allow_zero=False)
    stop = snapshot._decimal(example.get("stop_price"), "stop_price", nonnegative=True, allow_zero=False)
    from decimal import Decimal

    price_d = Decimal(price)
    stop_d = Decimal(stop)
    cost = str(price_d * quantity)
    risk = str(abs(price_d - stop_d) * quantity)
    return {
        "status": "INVENTED OFFLINE EXAMPLE",
        "reason": "Invented offline example; no real proposal, analysis, or approval exists.",
        "next_step": "Illustration only; no execution or strategy adoption.",
        "account": None,
        "candidate": {"symbol": symbol, "side": side, "reason": reason},
        "quantity": quantity,
        "estimated_cost": cost,
        "estimated_risk": risk,
        "checks": _checks(None, None, None, config),
        "risk_config": config,
        "executable": False,
        "fill_eligible": False,
    }


def build_card(
    reference,
    cash,
    holdings,
    *,
    config_raw=None,
    example=None,
    account_readiness=None,
    research=None,
    source_proof=None,
    quote=None,
    preparation=None,
):
    """Build an offline action card. Always non-executable."""
    config = _config(config_raw)
    if example is not None:
        return _example_card(example, config)
    account, _ = _match_account(reference, cash, holdings)
    checks = _checks(reference, cash, holdings, config, account_readiness)
    checks.append({"label": "underlying_source_bytes", "status": "unknown" if source_proof is None else source_proof["source_integrity"], "detail": "Saved-byte integrity only; the research conclusion remains unverified."})
    checks.append({"label": "current_price", "status": "unknown", "detail": "No verified current price usable for a trade; saved feed observations do not establish execution eligibility."})
    if preparation is not None:
        checks.append({'label': 'offline_preparation', 'status': 'not_run' if preparation['status'] == 'WAIT' else 'invented_only', 'detail': 'Offline example checks never approve this actual account or a real proposal. Broker/journal diagnostics are not applied.'})
    if research is not None:
        check = next(c for c in checks if c['label'] == 'current_recommendation')
        intent = research['proposal']['side'] if research['proposal'] else 'no reported idea'
        check['detail'] = ('Invented research fixture' if research['origin'] == 'fixture' else 'Supplied unverified research brief') + ': ' + research['symbol'] + ', ' + intent + ', ' + research['freshness'] + '. Current recommendation, a current price usable for this trade and your approved paper-trading rules remain unverified.'
    if reference is None or not isinstance(reference, dict):
        reason = "No verified current proposal is available."
    elif reference.get("reference_status") != "verified_historical_reference":
        reason = "No verified current proposal is available."
    else:
        reason = "No verified current proposal is available."
    usd = _usd_rows(cash) if isinstance(cash, dict) else None
    if usd and "USD" in usd and isinstance(usd["USD"], str) and snapshot.Decimal(usd["USD"]) < 0:
        reason += " USD cash is a debit balance; consider funding only after review."
    return _wait(reason, account, checks, config)
