"""Evaluate saved closing prices and native paper cash without trading authority."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import timedelta
import exchange_calendars as xc
from scripts.stage3_paper import snapshot as snap
from scripts.stage2_quotes.ibkr_batch import parse_ibkr_daily_batch

CASH_KEYS = {'schema','kind','origin','source','connection_mode','gateway_port',
             'requested_account_id','account_id','started_at','completed_at','status',
             'reason','account_ready','cash'}


def _load(raw):
    """Decode bounded JSON while rejecting duplicate keys and non-JSON numbers."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= 1024*1024:
        raise ValueError('source must be nonempty bytes under 1 MiB')
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result: raise ValueError('duplicate key')
            result[key] = value
        return result
    try:
        return json.loads(raw.decode(), object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
    except (UnicodeError, TypeError) as exc:
        raise ValueError('invalid source encoding') from exc


def _times(doc, as_of):
    start = snap._ts(doc['started_at'], 'started_at')
    done = snap._ts(doc['completed_at'], 'completed_at')
    at = snap._ts(as_of, 'as_of')
    if not start <= done <= at: raise ValueError('capture timestamps reversed or future')
    return done, at


def price_readiness(raw: bytes, *, as_of: str) -> dict:
    """Count completed XNYS sessions since each validated historical close."""
    source = _load(raw)
    if not isinstance(source, dict) or not {'started_at','completed_at'} <= source.keys():
        raise ValueError('price capture envelope')
    done, at = _times(source, as_of)
    parsed = parse_ibkr_daily_batch(raw)
    cal = xc.get_calendar('XNYS')
    available = [r['bar']['session_date'] for r in parsed['rows'].values() if r['status']=='available']
    begin = min(available+[ (at.date()-timedelta(days=10)).isoformat()])
    sessions = [s for s in cal.sessions_in_range(begin, at.date().isoformat())
                if cal.session_close(s).to_pydatetime() <= at]
    last = sessions[-1].date().isoformat() if sessions else None
    rows = []
    for symbol, result in parsed['rows'].items():
        if result['status'] != 'available':
            rows.append({'symbol':symbol,'state':result['status'],'reason':result['reason']}); continue
        bar = result['bar']; day = bar['session_date']
        lag = sum(s.date().isoformat()>day for s in sessions)
        rows.append({'symbol':symbol,'state':'available','session_date':day,
                     'close':bar['close'],'currency':bar['currency'],'session_lag':lag,
                     'stale':lag>1,'history_only':True})
    return {'rows':rows,'completed_at':source['completed_at'],'as_of':as_of,
            'last_completed_session':last,'raw_sha256':hashlib.sha256(raw).hexdigest(),
            'eligibility':'display-only','adjustment':'unverified','entitlement':'unverified'}


def cash_readiness(raw: bytes, *, as_of: str) -> dict:
    """Preserve signed native cash and distinguish unknown account reliability."""
    obj = _load(raw)
    if not isinstance(obj,dict) or set(obj)!=CASH_KEYS: raise ValueError('cash schema keys')
    if type(obj['schema']) is not int or obj['schema']!=1: raise ValueError('cash schema')
    if type(obj['gateway_port']) is not int or obj['gateway_port']!=4002: raise ValueError('gateway port')
    for k,v in {'kind':'ibkr-paper-currency-cash','source':'IBKR paper Gateway',
                'connection_mode':'readonly','requested_account_id':snap.EXPECTED_ACCOUNT}.items():
        if obj[k]!=v: raise ValueError(k+' mismatch')
    if obj['origin'] not in ('recorded','fixture') or obj['status'] not in ('complete','unavailable'):
        raise ValueError('cash origin/status')
    if not isinstance(obj['reason'],str): raise ValueError('cash reason')
    if obj['account_ready'] is not None and type(obj['account_ready']) is not bool:
        raise ValueError('account readiness type')
    done, at = _times(obj,as_of)
    if obj['status']=='unavailable':
        if not obj['reason'] or any(obj[k] is not None for k in ('account_id','account_ready','cash')):
            raise ValueError('unavailable state')
    elif obj['account_id']!=snap.EXPECTED_ACCOUNT:
        raise ValueError('paper account mismatch')
    if obj['cash'] is not None:
        if not isinstance(obj['cash'],list) or len(obj['cash'])>50: raise ValueError('cash list')
        seen = set()
        for row in obj['cash']:
            if not isinstance(row,dict) or set(row)!={'currency','value'}: raise ValueError('cash row')
            cur = row['currency']
            if not isinstance(cur,str) or (cur!='BASE' and not re.fullmatch('[A-Z]{3}',cur)):
                raise ValueError('cash currency')
            if cur in seen: raise ValueError('duplicate currency')
            seen.add(cur); snap._decimal(row['value'],'cash value')
    return {**obj,'rows':obj['cash'] or [],'known_empty':obj['cash']==[],
            'as_of':as_of,'freshness':'unavailable' if obj['status']=='unavailable' else
                ('stale' if at-done>timedelta(minutes=15) else 'fresh'),
            'raw_sha256':hashlib.sha256(raw).hexdigest()}
