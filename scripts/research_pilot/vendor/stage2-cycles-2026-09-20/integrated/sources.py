from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional

__all__ = ["download", "parse_facts"]

SYMBOLS: tuple[str, ...] = ("G", "CALX", "CDNS", "SPY", "XLV")
EQUITY_SYMBOLS: tuple[str, ...] = SYMBOLS[:3]
ABSTAIN_SYMBOLS: tuple[str, ...] = SYMBOLS[3:]
CIKS: dict[str, int] = {"G": 1398659, "CALX": 1406666, "CDNS": 813672}
ENTITY_TOKEN: dict[str, str] = {"G": "GENPACT", "CALX": "CALIX", "CDNS": "CADENCE"}
SEC_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
REVENUE_CONCEPTS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
)
NET_INCOME_CONCEPT = "NetIncomeLoss"
UA = {"User-Agent": "bounded-research sources.py contact@example.com"}


def _aware_as_of(value: str | datetime) -> datetime:
    dt = datetime.fromisoformat(value) if isinstance(value, str) else value
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("as_of must be timezone-aware")
    return dt.astimezone(timezone.utc)


def _d(value: Any) -> date:
    return date.fromisoformat(str(value))


def _money(value: Any) -> str:
    if isinstance(value, bool):
        raise ValueError("boolean is not a monetary value")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("non-decimal monetary value") from exc
    if not amount.is_finite():
        raise ValueError("non-finite monetary value")
    return format(amount, "f")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(NoRedirect())


def _capture(capture_dir: Path, cik: int, raw: bytes) -> tuple[Path, str]:
    digest = hashlib.sha256(raw).hexdigest()
    path = capture_dir / f"CIK{cik:010d}_{digest}.json"
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise FileExistsError(f"capture collision for {path}")
        return path, digest
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, digest


def _payload(raw: bytes | str | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(raw, Mapping):
        return raw
    data = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    obj = json.loads(data)
    if not isinstance(obj, Mapping):
        raise ValueError("companyfacts payload is not an object")
    return obj


def _validate_identity(data: Mapping[str, Any], symbol: str) -> None:
    if int(data.get("cik", -1)) != CIKS[symbol]:
        raise ValueError("CIK mismatch")
    name = str(data.get("entityName", "")).upper()
    if ENTITY_TOKEN[symbol] not in name:
        raise ValueError("entityName mismatch")


def _usd_facts(data: Mapping[str, Any], concept: str) -> list[Mapping[str, Any]]:
    node = data.get("facts", {}).get("us-gaap", {}).get(concept, {})
    units = node.get("units", {}) if isinstance(node, Mapping) else {}
    out: list[Mapping[str, Any]] = []
    for unit, facts in units.items():
        if unit != "USD" or not isinstance(facts, list):
            continue
        out.extend(fact for fact in facts if isinstance(fact, Mapping) and fact.get("val") is not None)
    return out


def _days(fact: Mapping[str, Any]) -> Optional[int]:
    try:
        return (_d(fact["end"]) - _d(fact["start"])).days
    except Exception:
        return None


def _filed(fact: Mapping[str, Any]) -> date:
    return _d(fact["filed"])


def _fact_dates(fact: Mapping[str, Any]) -> Optional[tuple[date, date, date]]:
    try:
        start = _d(fact["start"])
        end = _d(fact["end"])
        filed = _d(fact["filed"])
    except (KeyError, TypeError, ValueError):
        return None
    if start > end or end > filed:
        return None
    return start, end, filed


def _revenue_ok(fact: Mapping[str, Any]) -> bool:
    if "val" not in fact:
        return False
    try:
        _money(fact["val"])
    except ValueError:
        return False
    return True


def _candidate_streams(
    data: Mapping[str, Any], concept: str, forms: set[str], lo: int, hi: int, as_of: date
) -> list[tuple[date, date, date, Mapping[str, Any]]]:
    seen: dict[tuple[date, date], Mapping[str, Any]] = {}
    for fact in _usd_facts(data, concept):
        if str(fact.get("form")) not in forms or not _revenue_ok(fact):
            continue
        dates = _fact_dates(fact)
        if dates is None:
            continue
        start, end, filed = dates
        if filed > as_of:
            continue
        if not (lo <= (end - start).days <= hi):
            continue
        key = (start, end)
        prev = seen.get(key)
        if prev is None:
            seen[key] = fact
        else:
            try:
                prev_dates = _fact_dates(prev)
            except Exception:
                prev_dates = None
            if prev_dates is None or filed > prev_dates[2]:
                seen[key] = fact
    out = []
    for (start, end), fact in seen.items():
        filed = _fact_dates(fact)
        if filed is None:
            continue
        out.append((start, end, filed[2], fact))
    return out


def _select(data: Mapping[str, Any], as_of: date) -> tuple[dict[str, str], str]:
    forms_q = {"10-Q", "10-Q/A"}
    forms_k = {"10-K", "10-K/A"}

    candidates: list[tuple[bool, date, date, str, date, date, Mapping[str, Any]]] = []
    for annual, forms, lo, hi in ((False, forms_q, 70, 110), (True, forms_k, 300, 400)):
        for concept in REVENUE_CONCEPTS:
            for start, end, filed, fact in _candidate_streams(data, concept, forms, lo, hi, as_of):
                candidates.append((annual, end, filed, concept, start, end, fact))

    if not candidates:
        raise ValueError("no comparable filed USD revenue/net-income periods")

    candidates.sort(key=lambda item: (item[1], item[2], 0 if item[0] else 1, item[3]), reverse=True)

    for annual, cend, cfiled, concept, cstart, cend2, current in candidates:
        revs = _candidate_streams(
            data,
            concept,
            forms_k if annual else forms_q,
            300 if annual else 70,
            400 if annual else 110,
            as_of,
        )
        priors = [
            (start, end, filed, fact)
            for (start, end, filed, fact) in revs
            if 330 <= (cend - end).days <= 400
        ]
        if not priors:
            continue
        priors.sort(key=lambda item: (abs((cend - item[1]).days - 365), item[2]), reverse=False)
        prior_start, prior_end, prior_filed, prior_fact = priors[0]

        ni_forms = forms_k if annual else forms_q
        nis: list[tuple[date, date, Mapping[str, Any]]] = []
        for fact in _usd_facts(data, NET_INCOME_CONCEPT):
            if str(fact.get("form")) not in ni_forms:
                continue
            dates = _fact_dates(fact)
            if dates is None:
                continue
            nstart, nend, nfiled = dates
            if nend != cend or nstart != cstart or nfiled > as_of:
                continue
            try:
                _money(fact["val"])
            except ValueError:
                continue
            nis.append((nfiled, nend, fact))
        if not nis:
            continue
        nis.sort(key=lambda item: (item[0], item[1]), reverse=True)
        ni_filed, _, ni_fact = nis[0]

        try:
            revenue = _money(current["val"])
            prior_revenue = _money(prior_fact["val"])
            net_income = _money(ni_fact["val"])
        except ValueError:
            continue

        try:
            rev_dec = Decimal(revenue)
            prior_dec = Decimal(prior_revenue)
            ni_dec = Decimal(net_income)
        except InvalidOperation:
            continue
        if not (rev_dec.is_finite() and prior_dec.is_finite() and ni_dec.is_finite()):
            continue
        if prior_dec <= 0 or rev_dec < 0:
            continue

        facts = {
            "period_end": cend.isoformat(),
            "filed": max(cfiled, prior_filed, ni_filed).isoformat(),
            "period_kind": "annual" if annual else "quarter",
            "revenue": revenue,
            "prior_revenue": prior_revenue,
            "net_income": net_income,
        }
        return facts, ("ok:annual_fallback" if annual else "ok:quarterly")

    raise ValueError("no comparable filed USD revenue/net-income periods")


def parse_facts(raw: bytes | str | Mapping[str, Any], symbol: str, as_of: str | datetime) -> Optional[dict[str, str]]:
    as_of_dt = _aware_as_of(as_of)
    if symbol in ABSTAIN_SYMBOLS:
        return None
    if symbol not in CIKS:
        raise ValueError(f"unsupported symbol {symbol}")
    data = _payload(raw)
    _validate_identity(data, symbol)
    facts, _ = _select(data, as_of_dt.date())
    return facts


def _entry(symbol: str, status: str, kind: str, url: str, retrieved_at: str, sha: str, facts: Any, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "kind": kind,
        "url": url,
        "retrieved_at": retrieved_at,
        "sha256": sha,
        "facts": facts,
        "reason": reason,
    }


def download(as_of: str | datetime, capture_dir: str | Path) -> tuple[dict[str, dict[str, Any]], int]:
    as_of_dt = _aware_as_of(as_of)
    capture = Path(capture_dir)
    capture.mkdir(parents=True, exist_ok=True)
    retrieved_at = as_of_dt.isoformat()
    bundle: dict[str, dict[str, Any]] = {}
    attempted = 0
    empty_sha = hashlib.sha256(b"").hexdigest()
    for symbol in SYMBOLS:
        if symbol in ABSTAIN_SYMBOLS:
            bundle[symbol] = _entry(symbol, "missing", "fixture", f"fixture://{symbol}", retrieved_at, empty_sha, {}, "etf_fundamentals_abstain: no permitted ETF fundamental adapter")
            continue
        cik = CIKS[symbol]
        url = SEC_URL.format(cik=cik)
        raw = b""
        sha = empty_sha
        try:
            attempted += 1
            req = urllib.request.Request(url, headers=UA)
            with _OPENER.open(req, timeout=20) as resp:
                if getattr(resp, "status", 200) != 200:
                    raise urllib.error.HTTPError(url, resp.status, "HTTP error", resp.headers, None)
                raw = resp.read(5 * 1024 * 1024 + 1)
            if len(raw) > 5 * 1024 * 1024:
                raise ValueError("companyfacts response exceeds 5MB")
            _, sha = _capture(capture, cik, raw)
            payload = _payload(raw)
            _validate_identity(payload, symbol)
            facts, reason = _select(payload, as_of_dt.date())
            bundle[symbol] = _entry(symbol, "ok", "sec-companyfacts", url, retrieved_at, sha, facts, reason)
        except Exception as exc:
            if raw and sha == empty_sha:
                try:
                    _, sha = _capture(capture, cik, raw)
                except Exception:
                    pass
            bundle[symbol] = _entry(symbol, "failed", "sec-companyfacts", url, retrieved_at, sha, {}, f"{type(exc).__name__}: {exc}")
    return bundle, attempted