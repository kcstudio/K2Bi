'''Stage 3 research proof: strict acquisition receipt + saved-source integrity.

Verifies SUPPLIED bytes against a strict finite acquisition receipt and the
brief's declared source hash. Never authenticates a research thesis, never
approves a recommendation. No import-time network/filesystem/CLI effects.
'''

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional, Tuple

from scripts.stage3_paper.research_evidence import (
    _parse_timestamp, _SHA_RE, parse_research_evidence, _fail)

KIND = 'sec-companyfacts-acquisition'
MAX_BLOB = 16 * 1024 * 1024
MAX_PROOF = 1024 * 1024
MAX_TOTAL_SECONDS = 45
MIN_SPACING_SECONDS = 1
REQUEST_TIMEOUT = 15
_HEX64 = re.compile(r'^[0-9a-f]{64}$')
_EMAIL = re.compile(r'^[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}$')
_PLACEHOLDER_DOMAINS = ('.example', '.test', '.invalid', '.localhost')
_PLACEHOLDER_LOCALS = {'contact', 'you', 'your-email', 'admin', 'test',
                       'n/a', 'none', 'tbd', 'example', 'user', 'email'}

TARGETS: Tuple[Tuple[str, str, str, str], ...] = (
    ('G', 'https://data.sec.gov/api/xbrl/companyfacts/CIK0001398659.json',
     '0001398659', 'GENPACT'),
    ('CDNS', 'https://data.sec.gov/api/xbrl/companyfacts/CIK0000813672.json',
     '0000813672', 'CADENCE'),
)
FIXED_URLS = {sym: url for sym, url, _c, _n in TARGETS}
SYMBOLS = tuple(sym for sym, _u, _c, _n in TARGETS)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _valid_contact(contact: str) -> bool:
    if type(contact) is not str:
        raise _fail('contact: must be a string')
    if '\r' in contact or '\n' in contact or len(contact) > 254:
        raise _fail('contact: invalid')
    if _EMAIL.fullmatch(contact) is None:
        raise _fail('contact: invalid email')
    local, domain = contact.rsplit('@', 1)
    if local.lower() in _PLACEHOLDER_LOCALS:
        raise _fail('contact: placeholder')
    low = domain.lower()
    if any(low.endswith(ph) for ph in _PLACEHOLDER_DOMAINS):
        raise _fail('contact: placeholder domain')
    return True


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def _default_opener(url: str, headers: Dict[str, str], timeout: int):
    import ssl
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, hdrs, newurl):
            raise urllib.error.HTTPError(req.full_url, code, msg, hdrs, fp)

    ctx = ssl.create_default_context()
    build = urllib.request.build_opener(
        _NoRedirect, urllib.request.HTTPSHandler(context=ctx))
    return build.open(urllib.request.Request(url, headers=headers),
                      timeout=timeout)


def _read_bounded(resp, limit: int) -> bytes:
    data = resp.read(limit + 1)
    if len(data) > limit:
        raise ValueError('response exceeds bounded read')
    return data


def _json_strict(raw: bytes, limit: int, label: str) -> Any:
    if type(raw) is not bytes:
        raise _fail(f'{label}: must be bytes')
    if len(raw) > limit:
        raise _fail(f'{label}: too large')
    try:
        text = raw.decode('utf-8')
    except UnicodeDecodeError:
        raise _fail(f'{label}: invalid UTF-8') from None

    def _pairs(pairs):
        seen = set()
        for k, _v in pairs:
            if k in seen:
                raise _fail(f'{label}: duplicate key {k}')
            seen.add(k)
        return dict(pairs)

    def _nonfinite(c):
        raise _fail(f'{label}: nonfinite constant {c}')

    try:
        return json.loads(text, object_pairs_hook=_pairs, parse_constant=_nonfinite)
    except ValueError as exc:
        if str(exc).startswith(label + ': '):
            raise
        raise _fail(f'{label}: invalid JSON') from None


def _identity_ok(symbol: str, body: bytes) -> bool:
    try:
        doc = _json_strict(body, MAX_BLOB, 'companyfacts')
    except ValueError:
        return False
    if type(doc) is not dict:
        return False
    for sym, _url, cik_txt, name_txt in TARGETS:
        if sym != symbol:
            continue
        cik, name, facts = doc.get('cik'), doc.get('entityName'), doc.get('facts')
        if type(cik) is not int or cik != int(cik_txt):
            return False
        if type(name) is not str or name_txt.upper() not in name.upper():
            return False
        return type(facts) is dict
    return False


def capture_sources(contact: str, *, program_sha256: str,
                    opener: Optional[Callable] = None,
                    sleep_fn: Optional[Callable[[float], None]] = None,
                    now_fn: Optional[Callable[[], datetime]] = None
                    ) -> Tuple[dict, Dict[str, bytes]]:
    '''Acquire the two fixed SEC companyfacts payloads exactly once each.'''
    _valid_contact(contact)
    if type(program_sha256) is not str or _HEX64.fullmatch(program_sha256) is None:
        raise _fail('program_sha256: invalid')
    fixture = opener is not None
    if opener is None:
        opener = _default_opener
    elif not callable(opener):
        raise _fail('opener: must be callable')
    if sleep_fn is None:
        import time
        sleep_fn = time.sleep
    if now_fn is None:
        def now_fn():
            return datetime.now(timezone.utc)

    started = now_fn()
    if type(started) is not datetime or started.tzinfo is None:
        raise _fail('now_fn: must return aware datetime')
    requests, blobs = [], {}
    headers = {'User-Agent': contact, 'Accept': 'application/json'}
    prev_start = None

    for symbol, url, _cik, _name in TARGETS:
        rstart = now_fn()
        if prev_start is not None:
            gap = (rstart - prev_start).total_seconds()
            if gap < MIN_SPACING_SECONDS:
                sleep_fn(MIN_SPACING_SECONDS - gap)
                rstart = now_fn()
        prev_start = rstart
        status, http_status, reason, digest = 'unavailable', None, None, None
        try:
            with opener(url, headers, REQUEST_TIMEOUT) as resp:
                code = getattr(resp, 'status', None)
                if type(code) is not int or code != 200:
                    raise ValueError('http status not 200')
                body = _read_bounded(resp, MAX_BLOB)
            if not _identity_ok(symbol, body):
                raise ValueError('invalid companyfacts identity')
            status, http_status, digest = 'received', 200, _sha256(body)
            blobs[url] = body
        except ValueError as exc:
            reason = (str(exc).replace(contact, '[redacted]') or 'error')[:200]
        except Exception as exc:
            reason = type(exc).__name__
        requests.append({
            'symbol': symbol, 'url': url,
            'started_at': _iso(rstart), 'completed_at': _iso(now_fn()),
            'status': status, 'http_status': http_status,
            'response_sha256': digest, 'reason': reason,
        })
    completed = now_fn()
    receipt = {
        'schema': 1, 'kind': KIND,
        'origin': 'fixture' if fixture else 'recorded',
        'program_sha256': program_sha256,
        'started_at': _iso(started), 'completed_at': _iso(completed),
        'requests': requests,
    }
    return receipt, blobs


_REQ_KEYS = {'symbol', 'url', 'started_at', 'completed_at', 'status',
             'http_status', 'response_sha256', 'reason'}
_ROOT_KEYS = {'schema', 'kind', 'origin', 'program_sha256', 'started_at',
              'completed_at', 'requests'}


def _validate_proof(proof: Any, expected_program_sha256: str,
                    as_of: datetime) -> datetime:
    if type(proof) is not dict or set(proof) != _ROOT_KEYS:
        raise _fail('proof: root keys mismatch')
    if type(proof['schema']) is not int or proof['schema'] != 1:
        raise _fail('proof.schema: invalid')
    if proof['kind'] != KIND:
        raise _fail('proof.kind: invalid')
    if proof['origin'] not in ('recorded', 'fixture'):
        raise _fail('proof.origin: invalid')
    ps = proof['program_sha256']
    if type(ps) is not str or _HEX64.fullmatch(ps) is None:
        raise _fail('proof.program_sha256: invalid')
    if ps != expected_program_sha256:
        raise _fail('proof.program_sha256: mismatch')
    root_start = _parse_timestamp(proof['started_at'], 'proof.started_at')
    root_end = _parse_timestamp(proof['completed_at'], 'proof.completed_at')
    if not (root_start <= root_end):
        raise _fail('proof: start after end')
    if root_end > as_of:
        raise _fail('proof: completed after evaluation')
    if (root_end - root_start).total_seconds() > MAX_TOTAL_SECONDS:
        raise _fail('proof: exceeds max total time')
    reqs = proof['requests']
    if type(reqs) is not list or len(reqs) != len(TARGETS):
        raise _fail('proof: need exactly two requests')
    prev_start = prev_end = None
    for idx, req in enumerate(reqs):
        if type(req) is not dict or set(req) != _REQ_KEYS:
            raise _fail('proof: request keys mismatch')
        symbol, url, _cik, _name = TARGETS[idx]
        if req['symbol'] != symbol or req['url'] != url:
            raise _fail('proof: request not fixed order')
        rstart = _parse_timestamp(req['started_at'], 'request.started_at')
        rend = _parse_timestamp(req['completed_at'], 'request.completed_at')
        if not (root_start <= rstart and rend <= root_end):
            raise _fail('request: outside root interval')
        if not (rstart <= rend):
            raise _fail('request: start after end')
        if prev_end is not None:
            if not (prev_end <= rstart):
                raise _fail('request: overlap')
            if (rstart - prev_start).total_seconds() < MIN_SPACING_SECONDS:
                raise _fail('request: spacing under 1s')
        prev_start, prev_end = rstart, rend
        status, hs = req['status'], req['http_status']
        dg, rn = req['response_sha256'], req['reason']
        if status not in ('received', 'unavailable'):
            raise _fail('request.status: invalid')
        if status == 'received':
            if type(hs) is not int or hs != 200:
                raise _fail('request.http_status: invalid')
            if type(dg) is not str or _SHA_RE.fullmatch(dg) is None:
                raise _fail('request.response_sha256: invalid')
            if rn is not None:
                raise _fail('request.reason: must be null')
        else:
            if hs is not None and (type(hs) is not int or hs != 200):
                raise _fail('request.http_status: invalid')
            if dg is not None:
                raise _fail('request.response_sha256: must be null')
            if type(rn) is not str or not rn or len(rn) > 200:
                raise _fail('request.reason: invalid')
    return root_end


def verify_research_sources(brief_raw: bytes, proof_raw: Optional[bytes],
                            source_blobs: Dict[str, bytes], *, as_of: str,
                            expected_program_sha256: str) -> dict:
    '''Verify saved-source integrity against a strict acquisition receipt.'''
    brief = parse_research_evidence(brief_raw, as_of=as_of)
    if type(expected_program_sha256) is not str or \
            _HEX64.fullmatch(expected_program_sha256) is None:
        raise _fail('expected_program_sha256: invalid')
    if type(source_blobs) is not dict:
        raise _fail('source_blobs: must be a mapping')
    for key, val in source_blobs.items():
        if key not in FIXED_URLS.values():
            raise _fail('source_blobs: unexpected url')
        if type(val) is not bytes:
            raise _fail('source_blobs: must be bytes')
        if len(val) > MAX_BLOB:
            raise _fail('source_blobs: too large')

    out = {
        'research': brief,
        'source_integrity': 'unknown',
        'acquisition_origin': 'unverified',
        'acquired_at': None,
        'proof_sha256': None,
        'recommendation_verified': False,
        'executable': False,
        'fill_eligible': False,
    }
    if proof_raw is None:
        return out

    as_of_dt = _parse_timestamp(as_of, 'as_of')
    proof = _json_strict(proof_raw, MAX_PROOF, 'proof')
    root_end = _validate_proof(proof, expected_program_sha256, as_of_dt)
    out['proof_sha256'] = _sha256(proof_raw)
    out['acquired_at'] = _iso(root_end)
    out['acquisition_origin'] = ('recorded_request_receipt'
                                 if proof['origin'] == 'recorded' and brief['origin'] == 'recorded'
                                 else 'invented_fixture')

    symbol = brief['symbol']
    if symbol not in FIXED_URLS:
        raise _fail('brief.symbol: not G/CDNS')
    fixed_url = FIXED_URLS[symbol]
    by_symbol = {r['symbol']: r for r in proof['requests']}
    req = by_symbol[symbol]
    captured = _parse_timestamp(brief['captured_at'], 'captured_at')
    if captured < _parse_timestamp(req['completed_at'], 'request.completed_at'):
        raise _fail('captured_at precedes acquisition completed')

    if any(src['uri'] != fixed_url for src in brief['sources']):
        raise _fail('brief source URI does not match symbol')
    blob = source_blobs.get(fixed_url)
    if req['status'] != 'received' or blob is None:
        return out
    actual = _sha256(blob)
    if actual != req['response_sha256']:
        raise _fail('blob: mismatch with receipt hash')
    if not _identity_ok(symbol, blob):
        raise _fail('blob: identity failed')
    matched = False
    for src in brief['sources']:
        if src['uri'] != fixed_url:
            continue
        if src['sha256'] != actual:
            raise _fail('brief source sha256 mismatch')
        matched = True
    if not matched:
        raise _fail('brief: no matching fixed-url source')
    out['source_integrity'] = 'matched_saved_bytes'
    return out

docs_placeholder = None
