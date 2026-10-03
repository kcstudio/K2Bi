"""Tests for proof-gated historical reference verification."""
from __future__ import annotations

import hashlib
import json
import os
import unittest

from scripts.stage3_paper.reference import verify_reference, TRUSTED_SOURCE_SHA256

_FIXTURE = os.path.join('proposals', 'stage2-ibkr-price-check-2026-09-26', 'ibkr-five-daily.json')
_AS_OF = '2026-09-26T04:00:00Z'
_LATER = '2026-10-03T04:00:00Z'


def _raw():
    with open(_FIXTURE, 'rb') as fh:
        return fh.read()


def _proof(raw, **over):
    doc = json.loads(raw.decode())
    proof = {
        'schema': 1, 'kind': 'ibkr-history-acquisition-proof', 'origin': 'recorded',
        'source': 'IBKR paper Gateway', 'connection_mode': 'readonly', 'gateway_port': 4002,
        'requested_account_id': 'DUQ220152', 'matched_account_id': 'DUQ220152',
        'started_at': doc['started_at'], 'completed_at': doc['completed_at'],
        'response_sha256': hashlib.sha256(raw).hexdigest(),
        'source_script_sha256': TRUSTED_SOURCE_SHA256,
        'request': {'symbols': ['G', 'CALX', 'CDNS', 'SPY', 'XLV'], 'duration': '5 D',
                    'bar_size': '1 day', 'what_to_show': 'TRADES', 'useRTH': True,
                    'keepUpToDate': False, 'requested_market_data_type': 3,
                    'observed_market_data_type': 'unverified'}}
    proof.update(over)
    return json.dumps(proof).encode()


class ReferenceTest(unittest.TestCase):
    def setUp(self):
        self.raw = _raw()

    def test_missing_proof_unverified(self):
        out = verify_reference(self.raw, None, as_of=_AS_OF)
        self.assertEqual(out['provenance'], 'unverified')
        self.assertEqual(out['adjustment'], 'unverified')
        self.assertFalse(out['reference_eligible'] or out['executable'] or out['fill_eligible'])

    def test_recorded_eligible_never_fillable(self):
        out = verify_reference(self.raw, _proof(self.raw), as_of=_AS_OF)
        self.assertEqual(out['reference_status'], 'verified_historical_reference')
        self.assertEqual(out['provenance'], 'recorded_request_verified')
        self.assertTrue(out['reference_eligible'])
        self.assertFalse(out['executable'] or out['fill_eligible'])

    def test_later_as_of_stale(self):
        out = verify_reference(self.raw, _proof(self.raw), as_of=_LATER)
        self.assertEqual(out['reference_status'], 'stale_or_unavailable')
        self.assertFalse(any(r['reference_eligible'] for r in out['rows']))
        self.assertFalse(out['reference_eligible'])

    def test_fixture_excluded(self):
        out = verify_reference(self.raw, _proof(self.raw, origin='fixture'), as_of=_AS_OF)
        self.assertEqual(out['provenance'], 'invented_fixture')
        self.assertEqual(out['reference_status'], 'fixture')
        self.assertFalse(out['reference_eligible'])

    def test_source_bytes_unchanged(self):
        before = bytes(self.raw)
        verify_reference(self.raw, _proof(self.raw), as_of=_AS_OF)
        self.assertEqual(self.raw, before)

    def _reject(self, proof):
        with self.assertRaises(ValueError):
            verify_reference(self.raw, proof, as_of=_AS_OF)

    def test_unrecognized_source(self):
        self._reject(_proof(self.raw, source_script_sha256='a'*64))

    def test_wrong_hash(self):
        self._reject(_proof(self.raw, response_sha256='b' * 64))

    def test_wrong_account(self):
        self._reject(_proof(self.raw, matched_account_id='DU000000'))

    def test_bool_port(self):
        self._reject(_proof(self.raw, gateway_port=True))

    def test_wrong_schema(self):
        self._reject(_proof(self.raw, schema=True))

    def test_bad_settings(self):
        proof = json.loads(_proof(self.raw).decode())
        proof['request']['useRTH'] = False
        self._reject(json.dumps(proof).encode())

    def test_bad_mdt_type(self):
        proof = json.loads(_proof(self.raw).decode())
        proof['request']['requested_market_data_type'] = True
        self._reject(json.dumps(proof).encode())

    def test_extra_key(self):
        self._reject(_proof(self.raw, extra='x'))

    def test_duplicate_key(self):
        self._reject(b'{"schema":1,"schema":1}')

    def test_future_timestamps(self):
        self._reject(_proof(self.raw, started_at='2099-01-01T00:00:00Z'))


if __name__ == '__main__':
    unittest.main()
