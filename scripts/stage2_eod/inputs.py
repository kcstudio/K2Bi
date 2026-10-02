"""Verify sealed saved research and display-only IBKR daily responses offline."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from decimal import Decimal
from . import ledger
from scripts.stage2_quotes.ibkr_batch import parse_ibkr_daily_batch
from scripts.stage2_quotes.ibkr_adapter import parse_ibkr_daily_check

CIKS = {'G': '0001398659', 'CALX': '0001406666', 'CDNS': '0000813672'}
SOURCE_KEYS = set('facts kind reason retrieved_at sha256 status url'.split())
FACT_KEYS = set('filed net_income period_end period_kind prior_revenue revenue'.split())


def canonical(obj):
    """Canonical bytes shared by requests, seals and journal entries."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError('not canonical JSON') from exc


def digest(obj):
    """Hash canonical JSON."""
    return hashlib.sha256(canonical(obj)).hexdigest()


def unique(pairs):
    """Reject duplicate keys throughout JSON."""
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f'duplicate JSON key: {key}')
        obj[key] = value
    return obj


def decode(raw):
    """Read bounded UTF-8 JSON without permissive constants or duplicates."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 16*1024*1024:
        raise ValueError('nonempty bounded bytes required')
    try:
        return json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                          parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f'invalid constant {value}')))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid JSON') from exc


def sealed(obj):
    """Verify a complete canonical receipt seal."""
    if not isinstance(obj, dict) or obj.get('status') != 'complete':
        raise ValueError('incomplete receipt')
    ledger.sha(obj.get('receipt_hash'))
    if obj['receipt_hash'] != digest({k: v for k, v in obj.items() if k != 'receipt_hash'}):
        raise ValueError('receipt seal mismatch')


def sec_url(symbol):
    """Return the exact permitted source URL for a mapped company."""
    return f'https://data.sec.gov/api/xbrl/companyfacts/CIK{CIKS[symbol]}.json' if symbol in CIKS else None


def source_result(symbol, src, at):
    """Validate source facts and recompute freshness and stance at evaluation."""
    ledger.keys(src, SOURCE_KEYS, 'saved source')
    for field in ('kind', 'reason', 'status', 'url'):
        ledger.text(src[field])
    ledger.sha(src['sha256'])
    available = ledger.aware(src['retrieved_at'])
    if available > at:
        raise ValueError('future source retrieval')
    if symbol in CIKS:
        if src['kind'] != 'sec-companyfacts' or src['url'] != sec_url(symbol):
            raise ValueError('SEC source identity mismatch')
    elif src['kind'] != 'fixture' or src['url'] != f'fixture://{symbol}' or src['status'] != 'missing' or src['facts'] != {}:
        raise ValueError('ETF source must remain missing')
    if src['status'] not in ('ok', 'missing', 'error'):
        raise ValueError('unknown source status')
    if src['status'] != 'ok':
        if not isinstance(src['facts'], dict) or src['facts']:
            raise ValueError('failed source has facts')
        return 'paused', 'abstain', f"source {src['status']}: {src['reason']}"
    facts = src['facts']
    ledger.keys(facts, FACT_KEYS, 'facts')
    try:
        filed, end = date.fromisoformat(facts['filed']), date.fromisoformat(facts['period_end'])
        if filed.isoformat() != facts['filed'] or end.isoformat() != facts['period_end']:
            raise ValueError('noncanonical source dates')
    except (ValueError, TypeError) as exc:
        raise ValueError('invalid source dates') from exc
    if not end <= filed <= at.date() or filed > available.date():
        raise ValueError('future/reversed filing dates')
    if facts['period_kind'] not in ('quarter', 'annual'):
        raise ValueError('invalid source period kind')
    ni = ledger.number(facts['net_income'], signed=True)
    revenue = ledger.number(facts['revenue'])
    prior = ledger.number(facts['prior_revenue'])
    if prior <= 0:
        raise ValueError('nonpositive prior revenue')
    if (at.date()-filed).days > 120 or (at.date()-end).days > (180 if facts['period_kind'] == 'quarter' else 450):
        return 'paused', 'abstain', 'underlying SEC filing or period is stale at evaluation'
    growth = revenue/prior-Decimal(1)
    stance = 'support' if ni > 0 and growth >= Decimal('.05') else ('invalidate' if ni < 0 or growth < 0 else 'abstain')
    return 'fresh', stance, f"SEC {facts['period_kind']} filed {filed}, period {end}; net income {ni}, growth {growth:.2%}"


def verify_research(raw, at):
    obj = decode(raw)
    ledger.keys(obj, set('analyses cycle_id decisions previous_hash receipt_hash request_sha256 source_bundle state status'.split()), 'research receipt')
    sealed(obj)
    cycle = ledger.aware(obj['cycle_id'])
    if cycle > at:
        raise ValueError('research receipt not yet available')
    ledger.sha(obj['request_sha256'])
    if obj['previous_hash'] is not None:
        ledger.sha(obj['previous_hash'])
    for field in ('source_bundle', 'analyses'):
        ledger.keys(obj[field], ledger.SYMBOLS, field)
    ledger.keys(obj['state'], ('books', 'last_cycle', 'research', 'resources'), 'original state')
    if obj['state']['research'] != obj['analyses'] or obj['state']['last_cycle'] != obj['cycle_id']:
        raise ValueError('original research/state disagreement')
    if obj['state']['books'] != ledger.initial_state()['books']:
        raise ValueError('original pilot books must be empty')
    resources = obj['state']['resources']
    ledger.keys(resources, ('cycles', 'model_usd', 'source_requests'), 'pilot resources')
    if any(type(resources[k]) is not int or resources[k] < 0 for k in ('cycles', 'source_requests')) or ledger.number(resources['model_usd']) != 0:
        raise ValueError('invalid pilot resources')
    if not isinstance(obj['decisions'], list) or len(obj['decisions']) != 10:
        raise ValueError('original decisions must be ten abstentions')
    expected = {(b, s) for b in ('research', 'intraday') for s in ledger.SYMBOLS}
    seen = set()
    for decision in obj['decisions']:
        ledger.keys(decision, ('book', 'symbol', 'action', 'reason', 'timestamp'), 'original decision')
        ledger.text(decision['book']); ledger.text(decision['symbol'])
        pair = (decision['book'], decision['symbol'])
        if pair not in expected or pair in seen or decision['action'] != 'abstain' or decision['timestamp'] != obj['cycle_id']:
            raise ValueError('invalid original decisions')
        ledger.text(decision['reason']); seen.add(pair)
    for symbol, analysis in obj['analyses'].items():
        ledger.keys(analysis, ('change', 'evidence', 'industry', 'reason', 'stance', 'status', 'symbol'), 'original analysis')
        if analysis['symbol'] != symbol or analysis['change'] not in ('new', 'unchanged', 'changed', 'evidence_changed'):
            raise ValueError('invalid original analysis identity')
        ledger.text(analysis['industry']); ledger.text(analysis['reason'])
        status, stance, _ = source_result(symbol, obj['source_bundle'][symbol], cycle)
        if (analysis['status'], analysis['stance']) != (status, stance):
            raise ValueError('original analysis disagrees with source facts')
        evidence = analysis['evidence']
        if not isinstance(evidence, dict):
            raise ValueError('invalid original analysis evidence')
        src = obj['source_bundle'][symbol]
        base_keys = ('kind', 'reason', 'retrieved_at', 'sha256', 'status', 'url')
        enriched = (*base_keys, *FACT_KEYS, 'symbol', 'industry', 'filing_age_days', 'period_age_days', 'growth')
        ledger.keys(evidence, enriched if status == 'fresh' else base_keys, 'original flat evidence')
        if status == 'fresh':
            if any(evidence[key] != value for key, value in src['facts'].items()):
                raise ValueError('original evidence facts disagree')
            if evidence['symbol'] != symbol or evidence['industry'] != analysis['industry']:
                raise ValueError('original evidence identity disagrees')
            for field, date_field in (('filing_age_days', 'filed'), ('period_age_days', 'period_end')):
                if type(evidence[field]) is not int or evidence[field] != (cycle.date()-date.fromisoformat(src['facts'][date_field])).days:
                    raise ValueError('original evidence age disagrees')
            growth = ledger.number(src['facts']['revenue'])/ledger.number(src['facts']['prior_revenue'])-Decimal(1)
            if not isinstance(evidence['growth'], str) or evidence['growth'] != str(growth):
                raise ValueError('original evidence growth disagrees')
        for key in ('kind', 'reason', 'retrieved_at', 'sha256', 'status', 'url'):
            if evidence.get(key) != src[key]:
                raise ValueError('original source/evidence disagreement')
    return obj


def from_saved_evidence(research_raw: bytes, ibkr_raw: bytes, *, as_of: str):
    """Produce dated display-only input from the original sealed evidence."""
    at = ledger.aware(as_of)
    research = verify_research(research_raw, at)
    batch = parse_ibkr_daily_batch(ibkr_raw)
    raw_batch = decode(ibkr_raw)
    if ledger.aware(batch['received_at']) > at:
        raise ValueError('price batch not yet available')
    analyses, bars, rejected, companies = {}, {}, {}, {}
    for symbol in ledger.SYMBOLS:
        src = research['source_bundle'][symbol]
        status, stance, reason = source_result(symbol, src, at)
        available = max(ledger.aware(research['cycle_id']), ledger.aware(src['retrieved_at']))
        analyses[symbol] = dict(symbol=symbol, status=status, stance=stance, reason=reason,
            available_at=ledger.iso(available), receipt_hash=research['receipt_hash'])
        companies[symbol] = dict(industry=research['analyses'][symbol]['industry'], source=src)
        captured = batch['rows'][symbol]
        if captured['status'] != 'available':
            rejected[symbol] = captured['reason']
            continue
        row = dict(captured['bar'], origin='recorded')
        # Preserve the validated capture for dated display, but evaluate freshness again.
        try:
            parse_ibkr_daily_check(canonical(raw_batch['results'][symbol]), expected_symbol=symbol, as_of=ledger.iso(at), max_session_lag=0)
        except ValueError as exc:
            rejected[symbol] = str(exc)
        else:
            rejected[symbol] = 'display-only saved IBKR daily bar; adjustment and eligibility unverified'
        bars[symbol] = row
    ledger.validate_inputs(ledger.iso(at), analyses, bars)
    return dict(as_of=ledger.iso(at), analyses=analyses, bars=bars, evidence=dict(
        origin='saved_provider_evidence', research_raw_sha256=hashlib.sha256(research_raw).hexdigest(),
        prices_raw_sha256=hashlib.sha256(ibkr_raw).hexdigest(), research_receipt_hash=research['receipt_hash'],
        research_cycle=research['cycle_id'], prices_received_at=batch['received_at'],
        source='IBKR paper Gateway daily, display-only', rejected=rejected, companies=companies))
