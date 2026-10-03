"""Invented complete snapshots exercise pure recovery, never a broker."""
import copy, json, os, unittest
from unittest.mock import patch
from scripts.stage3_paper import offline_recovery as rec

CAP = '2025-01-06T15:00:00Z'
EXP = '2025-01-06T15:10:00Z'

def blob(doc): return json.dumps(doc).encode()
def context():
    return dict(schema=1, kind='offline-recovery-context', origin='fixture', account_id='DUQ220152', captured_at=CAP, expires_at=EXP, journal_tail=[], broker_positions=[], broker_open_orders=[], broker_order_status=[], coverage=dict(journal=True,positions=True,open_orders=True,order_status=True))
def position(qty=10): return dict(ticker='SPY',qty=qty,avg_price='500')
def event(kind='order_submitted', **over):
    payload = dict(ticker='SPY',side='buy',qty=10,limit_price='500',stop_loss=None,submitted_at=CAP)
    if kind=='engine_recovered': payload=dict(adopted_positions=[position()])
    row=dict(ts=CAP,schema_version=2,event_type=kind,trade_id='T1',journal_entry_id='J1',strategy='fixture',git_sha='a'*40,payload=payload,ticker='SPY',side='buy',qty=10,broker_order_id='1000',broker_perm_id='2000000')
    row.update(over); return row

def open_order():
    return dict(broker_order_id='1000',broker_perm_id='2000000',ticker='SPY',side='buy',qty=10,filled_qty=0,limit_price='500',status='Submitted',submitted_at=CAP,tif='DAY',client_tag='',aux_price='0',order_type='LMT')
def status(filled=10):
    return dict(broker_order_id='1000',broker_perm_id='2000000',status='Filled',filled_qty=filled,remaining_qty=10-filled,avg_fill_price='500',last_update_at=CAP,reason=None,client_tag='')

class RecoveryTests(unittest.TestCase):
    def evaluate(self, doc): return rec.evaluate_recovery(blob(doc),as_of=CAP)
    def test_clean_repeat_and_real_override_is_ignored(self):
        doc=context(); raw=blob(doc)
        with patch.dict(os.environ,{'K2BI_ALLOW_RECOVERY_MISMATCH':'1','K2BI_ADOPT_ORPHAN_STOP':'bad'}), patch.object(rec,'reconcile',wraps=rec.reconcile) as spy:
            result=self.evaluate(doc)
            self.assertEqual(result['status'],'fixture_clean')
            self.assertEqual(spy.call_args.kwargs['override_env'],'')
            self.assertIsNone(spy.call_args.kwargs['adopt_orphan_stop'])
            doc['broker_positions']=[position()]
            result=self.evaluate(doc); self.assertEqual(result['status'],'fixture_mismatch_refused')
        self.assertEqual(self.evaluate(context()),self.evaluate(context()))
        self.assertEqual(raw,blob(context()))
        for field in ('executable','applied','approved'): self.assertFalse(result[field])
        json.dumps(result)
    def test_actual_partial_filled_open_checkpoint_and_repeat(self):
        for qty in (7,10):
            doc=context();doc['journal_tail']=[event()];doc['broker_positions']=[position(qty)];doc['broker_order_status']=[status(qty)]
            out=self.evaluate(doc);self.assertEqual(out['status'],'fixture_catch_up');self.assertTrue(out['events']);json.dumps(out)
        doc=context();doc['journal_tail']=[event()];doc['broker_open_orders']=[open_order()]
        out=self.evaluate(doc);self.assertEqual(out['status'],'fixture_catch_up')
        doc=context();doc['journal_tail']=[event('engine_recovered')];doc['broker_positions']=[position()]
        out=self.evaluate(doc);self.assertNotEqual(out['status'],'fixture_mismatch_refused');self.assertEqual(out,self.evaluate(doc))
    def test_missing_stale_recorded_unsupported_do_not_run(self):
        self.assertEqual(rec.evaluate_recovery(None,as_of=CAP)['status'],'not_run')
        cases=[]
        for key in ('journal_tail','broker_positions','broker_open_orders','broker_order_status'):
            doc=context();doc[key]=None;cases.append(doc)
        doc=context();doc['coverage']['journal']=False;cases.append(doc)
        doc=context();doc['origin']='recorded';cases.append(doc)
        doc=context();doc['journal_tail']=[event('engine_started',payload={})];cases.append(doc)
        with patch.object(rec,'reconcile',side_effect=AssertionError('must not run')):
            for doc in cases:
                with self.subTest(doc=doc): self.assertEqual(self.evaluate(doc)['status'],'not_run')
            self.assertEqual(rec.evaluate_recovery(blob(context()),as_of='2025-01-06T15:11:00Z')['status'],'not_run')
    def test_strict_invalid_families_and_identity_collision(self):
        mutations=[lambda d:d.update(schema=1.0),lambda d:d.update(account_id='OTHER'),lambda d:d.update(extra=1),lambda d:d.update(coverage=None),lambda d:d.update(captured_at=EXP),lambda d:d.update(expires_at=CAP),lambda d:d.update(broker_positions=[dict(position(),qty=True)]),lambda d:d.update(broker_positions=[dict(position(),avg_price=500)]),lambda d:d.update(broker_positions=[dict(position(),ticker='!!!')]),lambda d:d.update(broker_positions=[position(),position()]),lambda d:d.update(broker_open_orders=[open_order(),open_order()]),lambda d:d.update(broker_open_orders=[dict(open_order(),broker_order_id='ABC')]),lambda d:d.update(broker_order_status=[dict(status(),last_update_at=EXP)]),lambda d:d.update(journal_tail=[event(ts=EXP)]),lambda d:d.update(journal_tail=[event(schema_version=2.0)]),lambda d:d.update(journal_tail=[event(extra=True)]),lambda d:d.update(journal_tail=[event(),event()]),lambda d:d.update(journal_tail=[event(qty=True)])]
        for mutate in mutations:
            doc=context();mutate(doc)
            with self.subTest(doc=doc),self.assertRaises(ValueError): self.evaluate(doc)
        for raw in (b'{"schema":1,"schema":1}',b'NaN',b'\xff',b'[]',bytearray(blob(context()))):
            with self.subTest(raw=raw),self.assertRaises(ValueError): rec.evaluate_recovery(raw,as_of=CAP)
