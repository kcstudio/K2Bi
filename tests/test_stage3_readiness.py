"""Native cash safety, completed sessions and real CLI integration regressions."""
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from scripts.stage3_paper import readiness, cash_capture, cli, desk
from scripts.stage2_eod import store
from tests.test_stage2_eod import request
from tests.test_stage3_paper import base_doc, raw

ROOT = Path(__file__).resolve().parents[1]
AS_OF = '2026-10-03T06:00:00+00:00'


def cash(**kwargs):
    """Explicit invented cash with negative USD, separate from recorded evidence."""
    doc = {'schema':1,'kind':'ibkr-paper-currency-cash','origin':'fixture',
           'source':'IBKR paper Gateway','connection_mode':'readonly','gateway_port':4002,
           'requested_account_id':'DUQ220152','account_id':'DUQ220152',
           'started_at':'2026-10-03T05:59:00+00:00','completed_at':'2026-10-03T05:59:30+00:00',
           'status':'complete','reason':'','account_ready':None,
           'cash':[{'currency':'USD','value':'-1400.25'},{'currency':'HKD','value':'100.00'}]}
    doc.update(kwargs); return doc


def prices(fresh=False):
    doc = json.loads((ROOT/'proposals/stage2-ibkr-price-check-2026-09-26/ibkr-five-daily.json').read_bytes())
    if fresh:
        doc['started_at']='2026-10-03T05:59:00+00:00';doc['completed_at']='2026-10-03T05:59:30+00:00'
        for row in doc['results'].values():
            row['received_at']='2026-10-03T05:59:15+00:00'
            for bar in row['bars']:
                bar['session_date']={'2026-09-23':'2026-09-30','2026-09-24':'2026-10-01','2026-09-25':'2026-10-02'}[bar['session_date']]
    return raw(doc)


class ReadinessTests(unittest.TestCase):
    def test_signed_cash_unknown_reliability_and_base(self):
        got=readiness.cash_readiness(raw(cash()),as_of=AS_OF)
        self.assertEqual(got['rows'][0]['value'],'-1400.25');self.assertIsNone(got['account_ready'])
        self.assertFalse(readiness.cash_readiness(raw(cash(account_ready=False)),as_of=AS_OF)['account_ready'])
        self.assertEqual(readiness.cash_readiness(raw(cash(cash=[{'currency':'BASE','value':'2'}])),as_of=AS_OF)['rows'][0]['currency'],'BASE')
        self.assertEqual(readiness.cash_readiness(raw(cash(cash=None)),as_of=AS_OF)['rows'],[])
        self.assertFalse(readiness.cash_readiness(raw(cash(cash=None)),as_of=AS_OF)['known_empty'])

    def test_strict_cash_families(self):
        cases=[cash(schema=True),cash(gateway_port=True),cash(source='other'),cash(connection_mode='write'),cash(account_id='All'),cash(account_ready='false'),cash(cash={}),cash(reason=None),cash(cash=[{'currency':'U$D','value':'1'}]),cash(cash=[{'currency':'USD','value':'NaN'}]),cash(cash=[{'currency':'USD','value':'1e3'}]),cash(cash=[{'currency':'USD','value':True}]),cash(cash=[{'currency':'USD','value':'1'}]*2),cash(started_at='2026-10-03T06:01:00Z'),cash(completed_at='2026-10-04T00:00:00Z')]
        for doc in cases:
            with self.subTest(doc=doc),self.assertRaises(ValueError):readiness.cash_readiness(raw(doc),as_of=AS_OF)
        with self.assertRaises(ValueError):readiness.cash_readiness(b'{"schema":1,"schema":1}',as_of=AS_OF)
        for bad in (b'',b'[]',b'\xff'):
            with self.assertRaises(ValueError):readiness.cash_readiness(bad,as_of=AS_OF)

    def test_session_count_weekend_and_before_close(self):
        old=readiness.price_readiness(prices(),as_of=AS_OF)
        self.assertEqual(old['last_completed_session'],'2026-10-02');self.assertEqual(old['rows'][0]['session_lag'],5)
        self.assertEqual(readiness.price_readiness(prices(),as_of='2026-10-02T19:00:00Z')['rows'][0]['session_lag'],4)
        new=readiness.price_readiness(prices(True),as_of=AS_OF)
        self.assertEqual(new['rows'][0]['session_lag'],0);self.assertEqual(new['eligibility'],'display-only')
        with self.assertRaises(ValueError):readiness.price_readiness(prices(True),as_of='2026-10-02T19:00:00Z')

    def test_real_cli_sources_dates_collisions_and_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);state=root/'state';receipt=store.run_cycle(state,request());before={p.name:p.read_bytes() for p in state.glob('*.json')}
            sources={'snapshot':raw(base_doc()),'prices':prices(True),'cash-snapshot':raw(cash())}
            args=['render','--state-dir',str(state),'--as-of',AS_OF,'--output',str(root/'out.html')]
            for name,value in sources.items():
                path=root/(name+'.html');path.write_bytes(value);args+=['--'+name,str(path)]
            self.assertEqual(cli.main(args),0);page=(root/'out.html').read_text()
            for text in ('STALE snapshot','Saved account snapshot:','2026-10-02T11:59:30','Tracked-company closing prices','-1400.25','refresh is manual','reliability is unproven'):
                self.assertIn(text,page)
            for tab in ('overview','companies','trades','practice','paper','words'):self.assertIn('id="tab-'+tab+'"',page)
            self.assertEqual(before,{p.name:p.read_bytes() for p in state.glob('*.json')})
            for name,value in sources.items():self.assertEqual((root/(name+'.html')).read_bytes(),value)
            collide=args.copy();collide[collide.index('--output')+1]=str(root/'prices.html');self.assertEqual(cli.main(collide),2)
            unknown=desk.render(receipt,None,as_of=AS_OF,cash_raw=raw(cash(cash=[{'currency':'BASE','value':'5'}])),prices_raw=prices(True))
            self.assertIn('Unknown USD',unknown);self.assertIn('Tracked-company closing prices',unknown)
            self.assertIn('does not distinguish recorded from invented prices',unknown)
            escaped=desk.render(receipt,raw(base_doc(reason='<script>bad</script>')),as_of=AS_OF)
            self.assertNotIn('<script>bad</script>',escaped)


class CaptureTests(unittest.TestCase):
    def run_capture(self,values,fail=False):
        class Fake:
            RequestTimeout=0
            def __init__(self):self.client=self;self.calls=[]
            def connect(self,*args,**kwargs):
                self.calls.append(('connect',kwargs))
                if fail:raise RuntimeError('failed')
            def managedAccounts(self):return ['DUQ220152']
            def reqAccountUpdates(self,*args):self.calls.append(('updates',args))
            def accountValues(self,account):self.calls.append(('values',account));return values
            def disconnect(self):self.calls.append(('disconnect',))
        ib=Fake();out=io.StringIO()
        with contextlib.redirect_stdout(out):cash_capture.main(ib,env={'K2BI_GATEWAY_CLIENT_ID':'90'})
        return ib,json.loads(out.getvalue())

    def test_exact_account_prefix_cleanup_and_connect_failure(self):
        row=lambda tag,cur,val,account='DUQ220152':NS(account=account,tag=tag,currency=cur,value=val)
        ib,doc=self.run_capture([row('$LEDGER-CashBalance','USD','-1400.25'),row('CashBalance','HKD','2'),row('CashBalance','USD','999','All'),row('AccountReady','','false')])
        self.assertEqual(doc['cash'][0]['value'],'-1400.25');self.assertFalse(doc['account_ready'])
        self.assertEqual(ib.calls[0][1]['fetchFields'],0);self.assertTrue(ib.calls[0][1]['readonly'])
        self.assertIn(('updates',('DUQ220152',)),ib.calls);self.assertIn(('updates',(False,'DUQ220152')),ib.calls)
        self.assertEqual(ib.calls[-1],('disconnect',));readiness.cash_readiness(raw(doc),as_of=doc['completed_at'])
        failed,doc=self.run_capture([],fail=True);self.assertEqual(doc['status'],'unavailable');self.assertEqual(failed.calls[-1],('disconnect',))
        for rows in ([row('CashBalance','USD','1'),row('$LEDGER-CashBalance','USD','2')],[row('CashBalance','USD','NaN')],[row('CashBalance','USD','1.123456789')],[row('CashBalance','USD',True)]):
            _,doc=self.run_capture(rows);self.assertEqual(doc['status'],'unavailable')
