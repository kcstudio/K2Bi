"""Offline finite-capture safety and readiness integration regressions."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace as N
from datetime import datetime, timezone
from scripts.stage3_paper import account_capture as cap, account_readiness as parser, action_card
from scripts.stage3_paper.snapshot import EXPECTED_ACCOUNT as ACCOUNT


def value(tag='CashBalance', currency='USD', amount='-2.00', account=ACCOUNT):
    return N(tag=tag, currency=currency, value=amount, account=account)


def trade(account=ACCOUNT, ident=3):
    return N(order=N(account=account, permId=ident, clientId=7, orderId=ident, action='BUY', orderType='LMT', totalQuantity=2, lmtPrice=10), contract=N(symbol='G', secType='STK', currency='USD'), orderStatus=N(status='Submitted'))


class Fake:
    RequestTimeout=0
    RaiseRequestErrors=False
    def __init__(self, fault=None, values=None, trades=None):
        self.fault=fault; self.values=values if values is not None else [value(), value('AccountReady', '', 'true'), value('NetLiquidation','HKD','99'),value(account='DUQ999')]
        self.trades=trades if trades is not None else []; self.calls=[]
        self.original_account=lambda account: self.calls.append(('account_end',account))
        self.original_orders=lambda: self.calls.append(('orders_end',))
        self.wrapper=N(accountDownloadEnd=self.original_account,openOrderEnd=self.original_orders)
        self.client=N(reqAccountUpdates=lambda flag,account:self.calls.append(('unsubscribe',flag,account)))
    def connect(self,*args,**kw):
        self.calls.append(('connect',args,kw,self.RequestTimeout,self.RaiseRequestErrors))
        if self.fault=='connect': raise TimeoutError()
    def managedAccounts(self): return ['DUQ999'] if self.fault=='account' else [ACCOUNT]
    def reqAccountUpdates(self,account):
        self.calls.append(('account',account))
        if self.fault=='timeout': raise TimeoutError()
        if self.fault!='missing_account_end': self.wrapper.accountDownloadEnd('DUQ999' if self.fault=='wrong_end' else account)
    def reqAllOpenOrders(self):
        self.calls.append(('orders',))
        if self.fault=='order_timeout': raise TimeoutError()
        if self.fault!='missing_order_end': self.wrapper.openOrderEnd()
        return self.trades
    def accountValues(self,account): return self.values
    def disconnect(self): self.calls.append(('disconnect',))


class CaptureTests(unittest.TestCase):
    def run_capture(self,ib,env=None):
        out=io.StringIO()
        with contextlib.redirect_stdout(out): code=cap.main(ib=ib,env=env or {'K2BI_GATEWAY_CLIENT_ID':'93'})
        self.assertEqual(code,0); obj=json.loads(out.getvalue())
        parsed=parser.parse_account_readiness(out.getvalue().encode(),as_of=datetime.now(timezone.utc).isoformat())
        self.assertFalse(parsed['executable']); self.assertIsNone(parsed['settled_usd']); self.assertIsNone(parsed['reserved_cash'])
        return obj
    def assert_cleanup(self,ib):
        self.assertIn(('disconnect',),ib.calls); self.assertIn(('unsubscribe',False,ACCOUNT),ib.calls)
        self.assertIs(ib.wrapper.accountDownloadEnd,ib.original_account); self.assertIs(ib.wrapper.openOrderEnd,ib.original_orders)
    def test_contract_and_cleanup(self):
        ib=Fake(); obj=self.run_capture(ib); self.assertEqual(obj['status'],'complete'); self.assert_cleanup(ib)
        _,args,kw,timeout,errors=ib.calls[0]; self.assertEqual(args,('127.0.0.1',4002)); self.assertTrue(kw['readonly']); self.assertEqual(kw['clientId'],93); self.assertEqual(kw['fetchFields'].value,0); self.assertEqual(timeout,15); self.assertTrue(errors)
        self.assertEqual(obj['account']['values'],[{'tag':'CashBalance','currency':'USD','value':'-2.00'}]); self.assertTrue(obj['account']['account_ready'])
        self.assertLessEqual(obj['account']['requested_at'],obj['account']['ended_at']); self.assertLessEqual(obj['orders']['requested_at'],obj['orders']['ended_at'])
    def test_failure_matrix_and_independent_requests(self):
        for fault in ('connect','account','timeout','order_timeout','missing_account_end','wrong_end','missing_order_end'):
            with self.subTest(fault=fault):
                ib=Fake(fault=fault); obj=self.run_capture(ib); self.assert_cleanup(ib)
                self.assertEqual(obj['status'],'unavailable' if fault in ('connect','account') else 'partial')
                if fault not in ('connect','account'): self.assertIn(('orders',),ib.calls)
    def test_attribution_and_identity(self):
        ib=Fake(trades=[trade(),trade(''),trade('DUQ999')]); obj=self.run_capture(ib)
        self.assertEqual(len(obj['orders']['rows']),1); self.assertEqual(obj['orders']['unattributed_count'],1); self.assertEqual(obj['orders']['other_account_count'],1)
        ib=Fake(trades=[trade(),trade()]); obj=self.run_capture(ib); self.assertEqual(obj['status'],'partial'); self.assertIsNone(obj['orders']['rows'])
    def test_bad_values_whole_section_unknown(self):
        for values in ([value(),value()], [value(amount='nan')], [value(amount='0.123456789')], [value('AccountReady','','perhaps')]):
            ib=Fake(values=values); obj=self.run_capture(ib); self.assertEqual(obj['status'],'partial'); self.assertIsNone(obj['account']['values']); self.assert_cleanup(ib)
    def test_missing_readiness_and_market_limit(self):
        t=trade(); t.order.orderType='MKT'; t.order.lmtPrice=1.7976931348623157e308
        obj=self.run_capture(Fake(values=[value()],trades=[t])); self.assertIsNone(obj['account']['account_ready']); self.assertIsNone(obj['orders']['rows'][0]['limit_price'])
    def test_invalid_lease_no_connection(self):
        for lease in ('0','1','100','90.5',''):
            ib=Fake(); obj=self.run_capture(ib,{'K2BI_GATEWAY_CLIENT_ID':lease}); self.assertEqual(obj['status'],'unavailable'); self.assertFalse(ib.calls)
    def test_normalized_card_keeps_wait(self):
        ib=Fake(); obj=self.run_capture(ib); parsed=parser.parse_account_readiness(json.dumps(obj).encode(),as_of=datetime.now(timezone.utc).isoformat()); card=action_card.build_card(None,None,None,account_readiness=parsed)
        self.assertEqual(card['status'],'WAIT'); self.assertIsNone(card['quantity']); self.assertTrue(any(c['label']=='trading_restrictions' and c['status']=='unknown' for c in card['checks']))
