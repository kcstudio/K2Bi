'''Tests for stage3 research_proof. Synthetic only, no real network.'''

from __future__ import annotations

import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts.stage3_paper.research_proof import (
    FIXED_URLS, capture_sources, verify_research_sources)

CONTACT = 'analyst@synthetic-research.org'
PROGRAM = 'a' * 64
START = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
BODY_G = json.dumps({'cik': 1398659, 'entityName': 'GENPACT LIMITED',
                     'facts': {}}).encode()
BODY_C = json.dumps({'cik': 813672, 'entityName': 'CADENCE DESIGN SYSTEMS',
                     'facts': {}}).encode()
H_G = hashlib.sha256(BODY_G).hexdigest()
H_C = hashlib.sha256(BODY_C).hexdigest()


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status = body, status

    def read(self, limit=-1):
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _clock(step=5):
    box = {'t': START}

    def now():
        out = box['t']
        box['t'] = out + timedelta(seconds=step)
        return out
    return now, box


def _opener(responses, calls=None):
    calls = calls if calls is not None else []

    def op(url, headers, timeout):
        calls.append((url, headers, timeout))
        item = responses.get(url)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)
    return op, calls


def good_receipt(origin='recorded', responses=None, step=5):
    now, _ = _clock(step)
    responses = responses or {FIXED_URLS['G']: BODY_G, FIXED_URLS['CDNS']: BODY_C}
    op, _ = _opener(responses)
    receipt, blobs = capture_sources(CONTACT, program_sha256=PROGRAM,
                                     opener=op, sleep_fn=lambda s: None, now_fn=now)
    receipt['origin'] = origin
    return receipt, blobs


def brief_raw(symbol='G', uri=None, sha=None, captured=None, published=None,
              research_as_of=None, expires=None, origin='recorded', extra_src=None):
    captured = captured or '2024-06-01T12:00:20Z'
    research_as_of = research_as_of or captured
    expires = expires or '2024-06-01T18:00:00Z'
    published = published or '2024-06-01T10:00:00Z'
    uri = uri if uri is not None else FIXED_URLS.get(symbol, FIXED_URLS['G'])
    sha = sha or (H_G if symbol == 'G' else H_C)
    doc = {
        'schema': 1, 'kind': 'offline-research-evidence', 'origin': origin,
        'captured_at': captured, 'research_as_of': research_as_of,
        'expires_at': expires, 'symbol': symbol, 'summary': 'neutral summary',
        'sources': [{'uri': uri, 'published_at': published, 'sha256': sha}],
        'proposal': {'side': 'watch', 'rationale': 'observe'},
    }
    if extra_src:
        doc['sources'].append(extra_src)
    return json.dumps(doc).encode()


AS_OF = '2024-06-02T00:00:00Z'


class CaptureTests(unittest.TestCase):
    def test_canonical(self):
        receipt, blobs = good_receipt(origin='fixture')
        self.assertEqual(receipt['origin'], 'fixture')
        self.assertEqual([r['symbol'] for r in receipt['requests']], list(FIXED_URLS))
        self.assertEqual(receipt['schema'], 1)
        self.assertTrue(all(r['status'] == 'received' for r in receipt['requests']))
        self.assertIn(FIXED_URLS['G'], blobs)
        self.assertNotIn('analyst', json.dumps(receipt))

    def test_partial_and_errors(self):
        for bad in (TimeoutError('t'), OSError('500'), ValueError('bad'),
                    _Resp(b'{}', 500)):
            resp = {FIXED_URLS['G']: bad, FIXED_URLS['CDNS']: BODY_C}
            receipt, blobs = good_receipt(responses=resp)
            self.assertEqual(receipt['requests'][0]['status'], 'unavailable')
            self.assertEqual(receipt['requests'][1]['status'], 'received')
            self.assertNotIn(FIXED_URLS['G'], blobs)

    def test_identity_and_oversize(self):
        bad = json.dumps({'cik': 1, 'entityName': 'X', 'facts': {}}).encode()
        receipt, _ = good_receipt(responses={FIXED_URLS['G']: bad,
                                             FIXED_URLS['CDNS']: BODY_C})
        self.assertEqual(receipt['requests'][0]['status'], 'unavailable')
        big = _Resp(b'x' * (16 * 1024 * 1024 + 5))
        receipt, _ = good_receipt(responses={FIXED_URLS['G']: big,
                                             FIXED_URLS['CDNS']: BODY_C})
        self.assertEqual(receipt['requests'][0]['status'], 'unavailable')

    def test_contact_and_program(self):
        for bad in ('', 'you@example.com', 'a@b.example', 'x@unit.test\nX',
                    'plain', 'contact@unit.test', 'a b@x.com'):
            with self.assertRaises(ValueError):
                capture_sources(bad, program_sha256=PROGRAM, opener=lambda *a: None)
        with self.assertRaises(ValueError):
            capture_sources(CONTACT, program_sha256='ABC', opener=lambda *a: None)

    def test_spacing_start_to_start(self):
        now, box = _clock(step=0)
        slept = []

        def sleep(s):
            slept.append(s)
            box['t'] = box['t'] + timedelta(seconds=s)
        op, _ = _opener({FIXED_URLS['G']: BODY_G, FIXED_URLS['CDNS']: BODY_C})
        capture_sources(CONTACT, program_sha256=PROGRAM, opener=op,
                        sleep_fn=sleep, now_fn=lambda: box['t'])
        self.assertTrue(slept and slept[0] >= 1)

    def test_two_attempts_only(self):
        op, calls = _opener({FIXED_URLS['G']: ValueError('x'),
                             FIXED_URLS['CDNS']: ValueError('y')})
        capture_sources(CONTACT, program_sha256=PROGRAM, opener=op,
                        sleep_fn=lambda s: None, now_fn=_clock()[0])
        self.assertEqual([c[0] for c in calls], [FIXED_URLS['G'], FIXED_URLS['CDNS']])
        self.assertTrue(all(c[2] == 15 for c in calls))


class VerifyTests(unittest.TestCase):
    def test_none_proof_unknown(self):
        out = verify_research_sources(brief_raw(), None, {},
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['source_integrity'], 'unknown')
        self.assertEqual(out['acquisition_origin'], 'unverified')
        self.assertFalse(out['executable'])

    def test_bad_as_of_still_validated(self):
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), None, {}, as_of='nope',
                                    expected_program_sha256=PROGRAM)

    def test_bad_expected_hash_even_none(self):
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), None, {}, as_of=AS_OF,
                                    expected_program_sha256='XYZ')

    def test_missing_blob_unknown(self):
        receipt, _ = good_receipt()
        out = verify_research_sources(brief_raw(), json.dumps(receipt).encode(), {},
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['source_integrity'], 'unknown')

    def test_unavailable_request_unknown(self):
        receipt, blobs = good_receipt(responses={FIXED_URLS['G']: ValueError('x'),
                                                 FIXED_URLS['CDNS']: BODY_C})
        out = verify_research_sources(brief_raw(), json.dumps(receipt).encode(), blobs,
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['source_integrity'], 'unknown')

    def test_matched_recorded(self):
        receipt, blobs = good_receipt()
        out = verify_research_sources(brief_raw(), json.dumps(receipt).encode(), blobs,
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['source_integrity'], 'matched_saved_bytes')
        self.assertEqual(out['acquisition_origin'], 'recorded_request_receipt')
        self.assertFalse(out['recommendation_verified'])

    def test_matched_fixture_proof_is_invented(self):
        receipt, blobs = good_receipt(origin='fixture')
        out = verify_research_sources(brief_raw(), json.dumps(receipt).encode(), blobs,
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['acquisition_origin'], 'invented_fixture')
        self.assertEqual(out['source_integrity'], 'matched_saved_bytes')

    def test_fixture_brief_origin_invented(self):
        receipt, blobs = good_receipt()
        out = verify_research_sources(brief_raw(origin='fixture'),
                                      json.dumps(receipt).encode(), blobs,
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['acquisition_origin'], 'invented_fixture')

    def test_cdns_symbol(self):
        receipt, blobs = good_receipt()
        out = verify_research_sources(brief_raw(symbol='CDNS'),
                                      json.dumps(receipt).encode(), blobs,
                                      as_of=AS_OF, expected_program_sha256=PROGRAM)
        self.assertEqual(out['source_integrity'], 'matched_saved_bytes')

    def test_tampered_body_and_hash(self):
        receipt, blobs = good_receipt()
        bad = dict(blobs)
        bad[FIXED_URLS['G']] = BODY_G + b' '
        for ob in (bad, blobs):
            with self.assertRaises(ValueError):
                verify_research_sources(brief_raw(sha='0' * 64 if ob is blobs else H_G),
                                        json.dumps(receipt).encode(), ob,
                                        as_of=AS_OF,
                                        expected_program_sha256=PROGRAM)

    def test_identity_in_blob(self):
        receipt, _ = good_receipt()
        forged = json.dumps({'cik': 1398659, 'entityName': 'GENPACT',
                             'facts': {}}).encode()
        bad = {FIXED_URLS['G']: forged}
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(sha=hashlib.sha256(forged).hexdigest()),
                                    json.dumps(receipt).encode(), bad,
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_program_mismatch(self):
        receipt, blobs = good_receipt()
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), json.dumps(receipt).encode(), blobs,
                                    as_of=AS_OF, expected_program_sha256='b' * 64)

    def test_brief_fixed_uri_mismatch(self):
        receipt, blobs = good_receipt()
        with self.assertRaises(ValueError):
            verify_research_sources(
                brief_raw(uri='https://data.sec.gov/api/xbrl/companyfacts/evil.json'),
                json.dumps(receipt).encode(), blobs,
                as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_symbol_not_g_cdns(self):
        receipt, blobs = good_receipt()
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(symbol='MSFT'),
                                    json.dumps(receipt).encode(), blobs,
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_extra_blob_url(self):
        receipt, blobs = good_receipt()
        blobs = dict(blobs)
        blobs['https://evil.example/x'] = BODY_G
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), json.dumps(receipt).encode(), blobs,
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_blob_not_bytes(self):
        receipt, blobs = good_receipt()
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), json.dumps(receipt).encode(),
                                    {FIXED_URLS['G']: bytearray(BODY_G)},
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_capture_before_capture_rejected(self):
        receipt, blobs = good_receipt()
        raw = brief_raw(captured='2024-06-01T11:00:00Z',
                        research_as_of='2024-06-01T11:30:00Z',
                        expires='2024-06-01T15:00:00Z')
        with self.assertRaises(ValueError):
            verify_research_sources(raw, json.dumps(receipt).encode(), blobs,
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_duplicate_root_key(self):
        raw = b'{"schema":1,"schema":1}'
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), raw, {},
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_nan_and_nonfinite(self):
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), b'{"schema": NaN}', {},
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_oversize_and_invalid_json(self):
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), b'x' * (1024 * 1024 + 1), {},
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), b'not json', {},
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_bool_schema_and_request_types(self):
        receipt, blobs = good_receipt()
        for mutate in ('schema', 'hs', 'reason'):
            r = json.loads(json.dumps(receipt))
            if mutate == 'schema':
                r['schema'] = True
            elif mutate == 'hs':
                r['requests'][0]['http_status'] = 200.0
            else:
                r['requests'][0]['status'] = 'received'
                r['requests'][0]['reason'] = ''
            with self.assertRaises(ValueError):
                verify_research_sources(brief_raw(), json.dumps(r).encode(), blobs,
                                        as_of=AS_OF,
                                        expected_program_sha256=PROGRAM)

    def test_future_root_end(self):
        receipt, blobs = good_receipt()
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), json.dumps(receipt).encode(), blobs,
                                    as_of='2024-06-01T12:00:24Z',
                                    expected_program_sha256=PROGRAM)

    def test_request_overlap_and_spacing(self):
        receipt, blobs = good_receipt(step=5)
        r = json.loads(json.dumps(receipt))
        r['requests'][1]['started_at'] = r['requests'][0]['started_at']
        with self.assertRaises(ValueError):
            verify_research_sources(brief_raw(), json.dumps(r).encode(), blobs,
                                    as_of=AS_OF, expected_program_sha256=PROGRAM)

    def test_bad_timestamps_and_overflow(self):
        receipt, blobs = good_receipt()
        for ts in ('2024-06-01', '2024-06-01T12:00:00',
                   '9999-12-31T23:59:59+99:00'):
            r = json.loads(json.dumps(receipt))
            r['started_at'] = ts
            with self.assertRaises(ValueError):
                verify_research_sources(brief_raw(), json.dumps(r).encode(), blobs,
                                        as_of=AS_OF,
                                        expected_program_sha256=PROGRAM)

    def test_no_invocation_without_call(self):
        self.assertTrue(callable(capture_sources))


if __name__ == '__main__':
    unittest.main()
