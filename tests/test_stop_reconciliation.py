"""Offline stop-fill identity, replay and rejection boundaries."""
import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from execution.connectors.types import BrokerExecution
from execution.engine.stop_reconciliation import StopIdentity, plan_stop_fill
from execution.engine.recovery import _positions_from_journal

NOW = datetime(2026, 9, 25, tzinfo=timezone.utc)
FILL = datetime(2026, 9, 4, 14, 32, 21, tzinfo=timezone.utc)
IDENTITY = StopIdentity('CDNS', 'cdns', 'parent-1', '1378210224', '159402', 1)
EXECUTION = BrokerExecution('exec-1','159402','1378210224','CDNS','sld',1,Decimal('294.77'),FILL)
RECORDS = [{'event_type':'engine_recovered','payload':{'adopted_positions':[{'ticker':'CDNS','qty':1,'avg_price':'330.98'},{'ticker':'SPY','qty':2,'avg_price':'707.72'}]}}]

class StopReconciliationTests(unittest.TestCase):
    def test_exact_close_preserves_other_position_and_inputs(self):
        records = copy.deepcopy(RECORDS)
        planned = plan_stop_fill(records, IDENTITY, EXECUTION, broker_qty=0, as_of=NOW)
        self.assertEqual(records, RECORDS)
        self.assertEqual(planned['payload']['fill_price'],'294.77')
        self.assertEqual(planned['trade_id'],'parent-1:stop')
        self.assertNotIn('commission_usd',planned)
        positions = _positions_from_journal(records+[planned])
        self.assertEqual([(x.ticker,x.qty) for x in positions],[('SPY',2)])
        self.assertIsNone(plan_stop_fill(records+[planned], IDENTITY, EXECUTION, broker_qty=0, as_of=NOW))

    def test_wrong_execution_identity_or_values_rejected(self):
        from dataclasses import replace
        for fields in [{'exec_id':''},{'broker_perm_id':'999'},{'broker_order_id':'99'},{'ticker':'SPY'},{'side':'buy'},{'qty':2},{'qty':True},{'qty':0},{'price':Decimal('NaN')},{'price':Decimal('Infinity')},{'price':Decimal('-1')},{'filled_at':datetime(2026,9,4)},{'filled_at':datetime(2027,1,1,tzinfo=timezone.utc)}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                plan_stop_fill(RECORDS,IDENTITY,replace(EXECUTION,**fields),broker_qty=0,as_of=NOW)

    def test_missing_or_wrong_position_proof_rejected(self):
        for qty in [None,True,1,-1,'0']:
            with self.subTest(qty=qty),self.assertRaises(ValueError):
                plan_stop_fill(RECORDS,IDENTITY,EXECUTION,broker_qty=qty,as_of=NOW)
        with self.assertRaises(ValueError):
            plan_stop_fill([],IDENTITY,EXECUTION,broker_qty=0,as_of=NOW)

    def test_conflicting_execution_id_rejected(self):
        planned=plan_stop_fill(RECORDS,IDENTITY,EXECUTION,broker_qty=0,as_of=NOW)
        planned['payload']['fill_price']='295.00'
        with self.assertRaises(ValueError):
            plan_stop_fill(RECORDS+[planned],IDENTITY,EXECUTION,broker_qty=0,as_of=NOW)

    def test_partial_close_rejected(self):
        from dataclasses import replace
        records=copy.deepcopy(RECORDS);records[0]['payload']['adopted_positions'][0]['qty']=2
        with self.assertRaises(ValueError):
            plan_stop_fill(records,replace(IDENTITY,quantity=2),EXECUTION,broker_qty=0,as_of=NOW)

    def test_unknown_identity_rejected(self):
        from dataclasses import replace
        for fields in [{'ticker':''},{'strategy':''},{'parent_trade_id':''},{'stop_perm_id':'0'},{'stop_order_id':''},{'quantity':True}]:
            with self.subTest(fields=fields),self.assertRaises(ValueError):
                plan_stop_fill(RECORDS,replace(IDENTITY,**fields),EXECUTION,broker_qty=0,as_of=NOW)

    def test_prepare_cli_is_repeatable_and_has_no_apply_mode(self):
        import importlib.util
        from pathlib import Path
        from dataclasses import asdict
        spec=importlib.util.spec_from_file_location('prepare_stop',Path(__file__).resolve().parents[1]/'scripts/prepare_stop_reconciliation.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        raw=asdict(EXECUTION);raw['price']=str(EXECUTION.price);raw['filled_at']=FILL.isoformat()
        evidence={'identity':asdict(IDENTITY),'execution':raw,'journal_records':copy.deepcopy(RECORDS),
                  'broker_quantity':0,'as_of':NOW.isoformat()}
        original=copy.deepcopy(evidence)
        packet=module.prepare(evidence)
        self.assertEqual(evidence,original)
        self.assertFalse(packet['runtime_writes'])
        evidence['journal_records'].append(packet['proposed_record'])
        replay=module.prepare(evidence)
        self.assertTrue(replay['already_applied'])
        self.assertEqual(replay['before'],replay['after'])
        self.assertEqual(packet['after'][0]['ticker'],'SPY')

    def test_barrier_only_resolved_by_later_matching_canonical_sell(self):
        from execution.engine.stop_reconciliation import unresolved_stop_barriers
        barrier={'event_type':'engine_stopped','ticker':'CDNS','broker_perm_id':'1378210224',
                 'payload':{'reason':'protective_stop_fill_unverified','quantity':1,'parent_trade_id':'parent-1'}}
        planned=plan_stop_fill(RECORDS,IDENTITY,EXECUTION,broker_qty=0,as_of=NOW)
        self.assertEqual(unresolved_stop_barriers([barrier,planned]),[])
        self.assertEqual(unresolved_stop_barriers([planned,barrier]),[barrier])
        wrong=copy.deepcopy(planned);wrong['broker_perm_id']='999'
        self.assertEqual(unresolved_stop_barriers([barrier,wrong]),[barrier])
        observed=copy.deepcopy(planned);observed['event_type']='external_fill_observed'
        self.assertEqual(unresolved_stop_barriers([barrier,observed]),[barrier])

    def test_unknown_barrier_cannot_be_cleared_by_same_ticker_and_quantity(self):
        from execution.engine.stop_reconciliation import unresolved_stop_barriers
        planned=plan_stop_fill(RECORDS,IDENTITY,EXECUTION,broker_qty=0,as_of=NOW)
        for perm,parent in [(None,''),(None,'parent-1'),('1378210224','')]:
            barrier={'event_type':'engine_stopped','ticker':'CDNS','broker_perm_id':perm,
                     'payload':{'reason':'protective_stop_fill_unverified','quantity':1,
                                'parent_trade_id':parent}}
            self.assertEqual(unresolved_stop_barriers([barrier,planned]),[barrier])
