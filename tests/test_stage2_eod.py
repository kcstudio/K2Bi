"""Safety contracts for the offline end-of-day practice ledger and saved inputs."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path
from scripts.stage2_eod import ledger, inputs

ROOT = Path(__file__).resolve().parents[1]
TIMES = ('2026-09-25T22:00:00Z', '2026-09-28T22:00:00Z', '2026-09-29T22:00:00Z',
         '2026-09-30T22:00:00Z', '2026-10-01T22:00:00Z')


def request(at=TIMES[0], close='100', *, symbol='G'):
    """Explicit invented offline normalized fixture, never provider evidence."""
    return dict(as_of=at, analyses={symbol: dict(symbol=symbol, status='fresh', stance='support',
        available_at=at, reason='invented offline fixture', receipt_hash='a'*64)},
        bars={symbol: dict(symbol=symbol, source='fixture', source_ref=f'fixture:{symbol}',
        session_date=at[:10], retrieved_at=at, currency='USD', close=close,
        adjustment_policy='unadjusted', market_data_type='historical', raw_sha256='b'*64,
        resolution='daily', trade_eligible=True, origin='fixture')},
        evidence={'origin': 'synthetic_fixture', 'label': 'Invented offline test'})


def advance(state, req):
    return ledger.advance(state, **{k: req[k] for k in ('as_of', 'analyses', 'bars')})


class LedgerTests(unittest.TestCase):
    def test_exact_frozen_entry_mark_stop_exit(self):
        state = ledger.initial_state()
        original = copy.deepcopy(state)
        for index, (at, close) in enumerate(zip(TIMES, ('100', '100', '102', '96', '94'))):
            result = advance(state, request(at, close))
            if index == 0:
                self.assertEqual(state, original)
                self.assertEqual(result['state']['books']['research']['pending']['G']['quantity'], 19)
            state = result['state']
            self.assertEqual(len(result['decisions']), 10)
            ledger.reconcile(state)
            if index == 2:
                self.assertEqual(state['books']['research']['equity'], '10036.55')
        book = state['books']['research']
        self.assertEqual((book['cash'], book['realized_pnl'], book['fees']), ('9883.16', '-116.84', '1.00'))
        self.assertEqual(book['events'][-1]['fill_price'], '93.9530')
        self.assertEqual(state['books']['intraday'], original['books']['intraday'])

    def test_future_null_unknown_type_and_metadata_families(self):
        invalid = [(group, key, value) for group, key, values in (
            ('bars', 'close', ('NaN', 'Infinity', '-1', '0', '1e3', '1000001', None, 100, True)),
            ('bars', 'trade_eligible', (None, 1, 'true')),
            ('bars', 'origin', ('provider', None)),
            ('bars', 'currency', ('HKD', None)),
            ('bars', 'source_ref', ('fixture:SPY', None)),
            ('bars', 'session_date', ('2026-09-26', '2026-09-28', None)),
            ('bars', 'retrieved_at', ('2026-09-25T19:00:00Z', TIMES[1], '2026-09-25T22:00:00', None)),
            ('analyses', 'available_at', (TIMES[1], None)),
            ('analyses', 'symbol', ('SPY', None)),
            ('analyses', 'receipt_hash', ('x', None))) for value in values]
        for group, key, value in invalid:
            with self.subTest(group=group, key=key, value=value), self.assertRaises(ValueError):
                req = request(); req[group]['G'][key] = value
                advance(ledger.initial_state(), req)
        for group in ('bars', 'analyses'):
            for value in (None, [], {'G': None}, {'UNKNOWN': {}}):
                with self.subTest(group=group, value=value), self.assertRaises(ValueError):
                    req = request(); req[group] = value; advance(ledger.initial_state(), req)
            req = request(); req[group]['G']['extra'] = 'bad'
            with self.assertRaises(ValueError):
                advance(ledger.initial_state(), req)

    def test_gap_missing_stale_adjusted_and_missed_open(self):
        state = advance(ledger.initial_state(), request())['state']
        cases = []
        req = request(TIMES[1], '120'); cases.append(req)
        req = request(TIMES[1]); req['bars'] = {}; cases.append(req)
        req = request(TIMES[1]); req['bars'] = request()['bars']; cases.append(req)
        req = request(TIMES[1]); req['bars']['G']['adjustment_policy'] = 'adjusted'; cases.append(req)
        req = request(TIMES[2]); cases.append(req)
        req = request('2026-09-29T15:00:00Z'); req['bars'] = request(TIMES[1])['bars']; cases.append(req)
        for req in cases:
            with self.subTest(req=req):
                result = advance(state, req)
                self.assertEqual(result['state']['books']['research']['events'], [])

    def test_calendar_holiday_dst_and_queue_after_open(self):
        self.assertEqual(ledger.eligible(ledger.aware('2026-07-02T22:00:00Z')), '2026-07-06')
        self.assertEqual(ledger.calendar().session_open(ledger.session('2026-03-06')).isoformat(), '2026-03-06T14:30:00+00:00')
        self.assertEqual(ledger.calendar().session_open(ledger.session('2026-03-09')).isoformat(), '2026-03-09T13:30:00+00:00')
        self.assertEqual(ledger.eligible(ledger.aware('2026-03-09T13:30:00Z')), '2026-03-10')
        with self.assertRaises(ValueError):
            ledger.session('2026-07-03')

    def test_reconcile_rejects_accounting_holding_intent_and_event_changes(self):
        queued = advance(ledger.initial_state(), request())['state']
        held = advance(queued, request(TIMES[1]))['state']
        for field in ('cash', 'fees', 'realized_pnl', 'equity'):
            broken = copy.deepcopy(held); broken['books']['research'][field] = '9999.00'
            with self.assertRaises(ValueError): ledger.reconcile(broken)
        broken = copy.deepcopy(held); broken['books']['research']['positions']['G']['quantity'] = 20
        with self.assertRaises(ValueError): ledger.reconcile(broken)
        broken = copy.deepcopy(queued); broken['books']['research']['pending']['G']['quantity'] = True
        with self.assertRaises(ValueError): ledger.reconcile(broken)
        for value in (None, {}, {'extra': 1}):
            broken = copy.deepcopy(held); broken['books']['research']['events'][0] = value
            with self.assertRaises(ValueError): ledger.reconcile(broken)


class SavedEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.research = (ROOT/'proposals/dashboard-ux-2026-09-26/first-cycle-receipt.json').read_bytes()
        self.prices = (ROOT/'proposals/stage2-ibkr-price-check-2026-09-26/ibkr-five-daily.json').read_bytes()

    def test_real_saved_rows_remain_display_only_and_expire(self):
        for at in ('2026-09-26T04:00:00Z', '2026-10-02T00:00:00Z'):
            req = inputs.from_saved_evidence(self.research, self.prices, as_of=at)
            self.assertEqual(set(req['bars']), set(ledger.SYMBOLS))
            self.assertTrue(all(not b['trade_eligible'] for b in req['bars'].values()))
            state = advance(ledger.initial_state(), req)['state']
            self.assertFalse(state['books']['research']['events'] or state['books']['research']['pending'])
        req = inputs.from_saved_evidence(self.research, self.prices, as_of='2027-02-01T22:00:00Z')
        self.assertEqual(req['analyses']['G']['status'], 'paused')

    def test_saved_evidence_rejects_duplicate_tamper_and_normalized_fixture(self):
        for research, prices in ((b'{"x":1,"x":2}', self.prices),
            (self.research.replace(b'Business services', b'Tampered'), self.prices),
            (self.research, inputs.canonical(request()['bars']))):
            with self.assertRaises(ValueError):
                inputs.from_saved_evidence(research, prices, as_of='2026-09-26T04:00:00Z')

    def test_resealed_sources_cannot_claim_future_or_wrong_identity(self):
        for key, value in (('url', 'https://evil.test/CIK0001398659.json'), ('sha256', None),
                           ('retrieved_at', TIMES[1]), ('kind', 'fixture')):
            obj = inputs.decode(self.research)
            obj['source_bundle']['G'][key] = value
            obj['receipt_hash'] = inputs.digest({k:v for k,v in obj.items() if k != 'receipt_hash'})
            with self.subTest(key=key), self.assertRaises(ValueError):
                inputs.from_saved_evidence(inputs.canonical(obj), self.prices, as_of='2026-09-26T04:00:00Z')
