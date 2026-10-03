"""Verify acquisition proof and derive display-only historical reference."""
from __future__ import annotations

import hashlib
import re

from scripts.stage3_paper import snapshot as snap
from scripts.stage3_paper.readiness import _load, _times, price_readiness

_PROOF_KEYS = {'schema', 'kind', 'origin', 'source', 'connection_mode', 'gateway_port',
               'requested_account_id', 'matched_account_id', 'started_at', 'completed_at',
               'response_sha256', 'source_script_sha256', 'request'}
_REQ_KEYS = {'symbols', 'duration', 'bar_size', 'what_to_show', 'useRTH', 'keepUpToDate',
             'requested_market_data_type', 'observed_market_data_type'}
_SYMBOLS = ['G', 'CALX', 'CDNS', 'SPY', 'XLV']
_HEX64 = re.compile('[0-9a-f]{64}')
# Reviewed finite TRADES probe, account-matched and non-streaming.
TRUSTED_SOURCE_SHA256 = 'c0da5c9fa6e6e99be886ccc9455cc407964f908cf29446fc965f5405c983b3b6'


def _validate_request(req):
    if not isinstance(req, dict) or set(req) != _REQ_KEYS:
        raise ValueError('request schema keys')
    expect = {'symbols': _SYMBOLS, 'duration': '5 D', 'bar_size': '1 day',
              'what_to_show': 'TRADES', 'useRTH': True, 'keepUpToDate': False,
              'requested_market_data_type': 3, 'observed_market_data_type': 'unverified'}
    for key, want in expect.items():
        got = req[key]
        if key == 'symbols':
            if not isinstance(got, list) or got != _SYMBOLS:
                raise ValueError('request symbols')
        elif key == 'requested_market_data_type':
            if type(got) is not int or got != 3:
                raise ValueError('request MDT type')
        elif key in ('useRTH', 'keepUpToDate'):
            if type(got) is not bool or got is not want:
                raise ValueError('request bool ' + key)
        elif got != want:
            raise ValueError('request ' + key)


def _verify_proof(raw: bytes, proof_raw):
    if not isinstance(proof_raw, bytes) or not 0 < len(proof_raw) <= 1024 * 1024:
        raise ValueError('proof must be nonempty bytes under 1 MiB')
    proof = _load(proof_raw)
    if not isinstance(proof, dict) or set(proof) != _PROOF_KEYS:
        raise ValueError('proof schema keys')
    if type(proof['schema']) is not int or proof['schema'] != 1:
        raise ValueError('proof schema')
    if proof['kind'] != 'ibkr-history-acquisition-proof':
        raise ValueError('proof kind')
    if proof['origin'] not in ('recorded', 'fixture'):
        raise ValueError('proof origin')
    if proof['source'] != 'IBKR paper Gateway':
        raise ValueError('proof source')
    if proof['connection_mode'] != 'readonly':
        raise ValueError('proof connection_mode')
    if type(proof['gateway_port']) is not int or proof['gateway_port'] != 4002:
        raise ValueError('proof gateway_port')
    if proof['requested_account_id'] != snap.EXPECTED_ACCOUNT:
        raise ValueError('requested account mismatch')
    if proof['matched_account_id'] != snap.EXPECTED_ACCOUNT:
        raise ValueError('matched account mismatch')
    for field in ('response_sha256', 'source_script_sha256'):
        reg = proof[field]
        if not isinstance(reg, str) or not _HEX64.fullmatch(reg):
            raise ValueError('proof digest ' + field)
    if proof['response_sha256'] != hashlib.sha256(raw).hexdigest():
        raise ValueError('response digest mismatch')
    if proof['source_script_sha256'] != TRUSTED_SOURCE_SHA256:
        raise ValueError('unrecognized acquisition program')
    _validate_request(proof['request'])
    return proof


def verify_reference(raw: bytes, proof_raw, *, as_of: str) -> dict:
    """Return display-only reference rows with proof-gated eligibility."""
    result = price_readiness(raw, as_of=as_of)
    base = {'rows': result['rows'], 'completed_at': result['completed_at'], 'as_of': result['as_of'],
            'last_completed_session': result['last_completed_session'],
            'raw_sha256': result['raw_sha256'], 'adjustment': result['adjustment'],
            'entitlement': 'unverified', 'executable': False, 'fill_eligible': False}
    if proof_raw is None:
        return {**base, 'provenance': 'unverified', 'reference_status': 'unverified',
                'reference_eligible': False}
    proof = _verify_proof(raw, proof_raw)
    source = _load(raw)
    if proof['started_at'] != source.get('started_at') or proof['completed_at'] != source.get('completed_at'):
        raise ValueError('proof timestamps differ from batch')
    _times(proof, as_of)
    recorded = proof['origin'] == 'recorded'
    rows = []
    for row in result['rows']:
        entry = dict(row)
        ok = (recorded and row.get('state') == 'available'
              and isinstance(row.get('session_lag'), int) and row['session_lag'] <= 1
              and not row.get('stale'))
        entry['reference_eligible'] = bool(ok)
        rows.append(entry)
    if recorded and any(r['reference_eligible'] for r in rows):
        status = 'verified_historical_reference'
    elif proof['origin'] == 'fixture':
        status = 'fixture'
    else:
        status = 'stale_or_unavailable'
    return {**base, 'rows': rows,
            'provenance': 'recorded_request_verified' if recorded else 'invented_fixture',
            'reference_status': status, 'reference_eligible': any(r['reference_eligible'] for r in rows),
            'adjustment': 'split-adjusted; not dividend-adjusted',
            'proof_sha256': hashlib.sha256(proof_raw).hexdigest(),
            'source_script_sha256': proof['source_script_sha256']}
