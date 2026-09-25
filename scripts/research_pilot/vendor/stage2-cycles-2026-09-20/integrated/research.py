import hashlib
from decimal import Decimal

from contracts import INDUSTRIES, UNIVERSE, aware, validate_source

MAX_LINES = 120
SUPPORT_PCT = Decimal("0.05")
SOURCE_NOTE = ("Source-linked rule-based hypothesis update only; "
               "not complete investment due-diligence or financial advice; "
               "stance never triggers a real trade.")

_UNTRUSTED_SCALAR_KEYS = ("status", "kind", "url", "retrieved_at", "sha256", "reason")


def analyze(bundle: dict, previous: dict | None, as_of) -> dict:
    asof = aware(as_of)
    prev = previous if isinstance(previous, dict) else {}
    results = {}

    bundle_invalid = not isinstance(bundle, dict)
    if not bundle_invalid:
        for symbol in bundle:
            if symbol not in UNIVERSE:
                bundle_invalid = True
                break

    for symbol in UNIVERSE:
        row = {"symbol": symbol,
               "industry": INDUSTRIES[symbol],
               "status": "paused", "stance": "abstain",
               "reason": "", "change": "unchanged",
               "evidence": {}}
        results[symbol] = row
        ev = row["evidence"]

        if bundle_invalid:
            row["reason"] = ("Invalid bundle or unknown symbols; abstaining. "
                             + SOURCE_NOTE)
            _apply_change(row, prev.get(symbol))
            continue

        src = bundle.get(symbol)
        if not isinstance(src, dict):
            row["reason"] = ("Missing source input; abstaining. "
                             + SOURCE_NOTE)
            _apply_change(row, prev.get(symbol))
            continue

        try:
            evidence = validate_source(symbol, src, asof)
        except ValueError as exc:
            for k in _UNTRUSTED_SCALAR_KEYS:
                value = src.get(k)
                if isinstance(value, (str, int, float, bool)) and not isinstance(value, bool) or isinstance(value, bool):
                    ev[k] = value
            row["reason"] = f"Source invalid ({exc}); abstaining. " + SOURCE_NOTE
            _apply_change(row, prev.get(symbol))
            continue

        row["status"] = "fresh"
        ev.update(evidence)
        growth = Decimal(evidence["growth"])
        ni = Decimal(evidence["net_income"])
        if ni > 0 and growth >= SUPPORT_PCT:
            row["stance"] = "support"
            row["reason"] = (f"Positive net income and growth {growth:.2%} >= 5%. "
                             + SOURCE_NOTE)
        elif ni < 0 or growth < 0:
            row["stance"] = "invalidate"
            row["reason"] = (f"Negative net income or growth {growth:.2%}. "
                             + SOURCE_NOTE)
        else:
            row["stance"] = "neutral"
            row["reason"] = (f"Growth {growth:.2%} below 5% with positive net income. "
                             + SOURCE_NOTE)
        _apply_change(row, prev.get(symbol))
    return results


def _apply_change(row, prev_row):
    p = prev_row if isinstance(prev_row, dict) else {}
    p_stance = p.get("stance")
    p_ev = p.get("evidence")
    p_hash = p_ev.get("sha256") if isinstance(p_ev, dict) else None
    cur_hash = row["evidence"].get("sha256")
    if p_stance is None:
        row["change"] = "new"
        row["reason"] = row["reason"] + " Change: new."
    elif p_stance == row["stance"] and p_hash == cur_hash:
        row["change"] = "unchanged"
        row["reason"] = row["reason"] + " Change: unchanged."
    else:
        row["change"] = "changed"
        if p_stance != row["stance"]:
            row["reason"] = (row["reason"]
                              + f" Change: changed stance from {p_stance} to {row['stance']}.")
        else:
            row["reason"] = (row["reason"]
                              + " Change: evidence hash changed.")
    return row

