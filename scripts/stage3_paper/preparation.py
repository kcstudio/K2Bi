"""Stage 3 paper preparation presentation layer: combines offline risk and recovery.

Never authenticates origins, hashes or timestamps.  Approval, execution
and application are always False.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Dict, Optional

from execution.validators.types import Order
from scripts.stage3_paper.offline_recovery import evaluate_recovery
from scripts.stage3_paper.offline_risk import (
    evaluate_risk,
    parse_money,
    parse_timestamp,
)

__all__ = ["build_preparation"]

_RISK_OK = "fixture_pass"
_RECOVERY_OK = ("fixture_clean", "fixture_catch_up")
_KEYS = ("ticker", "side", "qty", "limit_price", "stop_loss", "strategy", "submitted_at")


def _proposal(risk: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = risk.get("proposal")
    if not raw:
        return None
    return {key: raw.get(key) for key in _KEYS}


def _order(proposal: Dict[str, Any]) -> Optional[Order]:
    try:
        return Order(
            str(proposal["ticker"]),
            str(proposal["side"]),
            int(proposal["qty"]),
            Decimal(parse_money(str(proposal["limit_price"]), "limit_price")),
            None if proposal["stop_loss"] is None else Decimal(parse_money(str(proposal["stop_loss"]), "stop_loss")),
            str(proposal["strategy"]),
            parse_timestamp(str(proposal["submitted_at"]), "submitted_at"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _consistency(risk: Dict[str, Any], recovery: Dict[str, Any]) -> None:
    if risk.get("account_id") and recovery.get("account_id") and risk["account_id"] != recovery["account_id"]:
        raise ValueError("risk and recovery account contexts disagree; refusing to mix fixture and actual")
    if risk.get("origin") and recovery.get("origin") and risk["origin"] != recovery["origin"]:
        raise ValueError("risk and recovery origins disagree; refusing to mix fixture and actual")


def _checks(risk: Dict[str, Any], recovery: Dict[str, Any]) -> list:
    rows = [{"label": row.get("module"), "status": row.get("status"), "detail": row.get("reason")} for row in risk.get("results", [])]
    label = "approved" if recovery.get("status") in _RECOVERY_OK else (recovery.get("status") or "not_run")
    rows.append({"label": "recovery_reconciliation", "status": label, "detail": recovery.get("reason") or "; ".join(recovery.get("mismatch_reasons", [])) or "recovery not run"})
    rows.append({"label": "broker_reconciliation", "status": "not_run", "detail": "diagnostic only; broker positions not authenticated"})
    rows.append({"label": "current_quote", "status": "unverified", "detail": "current prices unverified; any fixture price is fictional"})
    rows.append({"label": "approved_paper_rules", "status": "unverified", "detail": "approved paper trading rules unverified"})
    rows.append({"label": "current_recommendation", "status": "unverified", "detail": "no current recommendation; preview is invented only"})
    return rows


def _dates(risk: Dict[str, Any], recovery: Dict[str, Any]) -> Dict[str, Any]:
    def part(item):
        return {"captured": item.get("captured_at"), "evaluated": item.get("evaluated_at"), "expires": item.get("expires_at")}
    return {"risk": part(risk), "recovery": part(recovery)}


def _wait(risk, recovery, checks, reason, next_step) -> Dict[str, Any]:
    return {
        "status": "WAIT",
        "origin": risk.get("origin") or recovery.get("origin"),
        "account": risk.get("account_id") or recovery.get("account_id"),
        "candidate": None, "quantity": None, "estimated_cost": None, "estimated_risk": None,
        "reason": reason, "next_step": next_step, "checks": checks,
        "risk_config": {"sha256": risk.get("config_sha256")},
        "risk": risk, "recovery": recovery, "evidence_dates": _dates(risk, recovery),
        "executable": False, "fill_eligible": False, "approved": False, "applied": False,
    }


def build_preparation(risk_raw: Optional[bytes], recovery_raw: Optional[bytes], *, as_of: str, config_raw: bytes) -> Dict[str, Any]:
    if not isinstance(config_raw, bytes):
        raise ValueError("config_raw must be bytes")
    risk = evaluate_risk(risk_raw, as_of=as_of, config_raw=config_raw)
    recovery = evaluate_recovery(recovery_raw, as_of=as_of)
    _consistency(risk, recovery)
    checks = _checks(risk, recovery)
    proposal = _proposal(risk)
    order = _order(proposal) if proposal else None
    if not (risk.get("status") == _RISK_OK and recovery.get("status") in _RECOVERY_OK and order is not None):
        if risk.get("status") == "fixture_rejected":
            reason = "blocked for trade: whitelist/risk module rejected the invented candidate"
        elif recovery.get("status") == "fixture_mismatch_refused":
            reason = "blocked for trade: broker/journal reconciliation reported a mismatch"
        elif recovery_raw is None or recovery.get("status") == "not_run":
            reason = "waiting: recovery context not run or missing; genuine funding, current price and approved paper rules remain unverified"
        else:
            reason = "waiting: funding, current price and approved paper rules remain unverified"
        return _wait(risk, recovery, checks, reason, "verify funding, live quotes and approved rules; re-run preparation")
    cost = format(order.notional, "f")
    risk_amt = None if order.stop_loss is None else format(order.trade_risk, "f")
    reason = "invented offline preview of a proposed action; not approved, not an order"
    if risk_amt is None:
        reason += "; stop loss unknown so risk is not estimated"
    return {
        "status": "INVENTED OFFLINE PREVIEW",
        "origin": risk.get("origin") or recovery.get("origin"),
        "account": risk.get("account_id") or recovery.get("account_id"),
        "candidate": {"symbol": proposal["ticker"], "side": proposal["side"]},
        "quantity": int(proposal["qty"]) if proposal.get("qty") is not None else None,
        "estimated_cost": cost, "estimated_risk": risk_amt, "reason": reason,
        "next_step": "treat as invented only; verify funding, quotes and approved rules before any paper action",
        "checks": checks, "risk_config": {"sha256": risk.get("config_sha256")},
        "risk": risk, "recovery": recovery, "evidence_dates": _dates(risk, recovery),
        "executable": False, "fill_eligible": False, "approved": False, "applied": False,
    }
