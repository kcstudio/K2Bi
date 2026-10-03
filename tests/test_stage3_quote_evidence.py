"""Invented strict quote/provenance regressions, no broker requests."""
import copy, hashlib, json, unittest
from scripts.stage3_paper.quote_evidence import parse_quote_evidence

KEYS = 'requested_account_id connection_mode gateway_port client_id requested_market_data_type startup_fetch generic_ticks snapshot regulatory_snapshot'.split()
ASOF = '2025-01-06T14:30:41Z'
PROGRAM = 'a' * 64

def encode(doc):
    return json.dumps(doc).encode()

def canonical():
    rows = []
    for symbol, start in zip(('SPY', 'G', 'CDNS'), (2, 14, 26)):
        stamp = lambda seconds: f'2025-01-06T14:30:{seconds:02d}Z'
        rows.append(dict(symbol=symbol, currency='USD', status='received', reason=None,
            requested_at=stamp(start), completed_at=stamp(start+11), snapshot_end_observed=True,
            observed_data_type=1, observed_data_type_at=stamp(start+1), bid='100.01', ask='100.02', last='100.01',
            last_trade_at=stamp(start+4), received_at=stamp(start+10)))
    return dict(schema=1, kind='ibkr-paper-quote-snapshot', source='IBKR paper Gateway', origin='recorded', status='complete',
        requested_account_id='DUQ220152', account_matched=True, connection_mode='readonly', gateway_port=4002, client_id=90,
        requested_market_data_type=3, startup_fetch=0, generic_ticks='', snapshot=True, regulatory_snapshot=False,
        started_at='2025-01-06T14:30:00Z', completed_at='2025-01-06T14:30:40Z', server_time='2025-01-06T14:30:01Z', rows=rows)

def proof(raw, doc):
    request = {key:doc[key] for key in KEYS}; request['symbols'] = ['SPY','G','CDNS']
    return dict(schema=1, kind='ibkr-paper-quote-capture-proof', source_sha256=PROGRAM,
        response_sha256=hashlib.sha256(raw).hexdigest(), request_sha256=hashlib.sha256(json.dumps(request,sort_keys=True,separators=(',',':')).encode()).hexdigest(),
        started_at=doc['started_at'], completed_at=doc['completed_at'], returncode=0)

def unrequested(row):
    row.update(status='unavailable', reason='not requested', snapshot_end_observed=False)
    for key in ('requested_at','completed_at','observed_data_type','observed_data_type_at','bid','ask','last','last_trade_at','received_at'): row[key]=None

class QuoteEvidenceTests(unittest.TestCase):
    def parse(self, doc, **kwargs):
        return parse_quote_evidence(encode(doc), as_of=kwargs.pop('as_of',ASOF), **kwargs)
    def reject(self, doc):
        with self.assertRaises(ValueError): self.parse(doc)
    def test_recorded_proof_fixture_and_unverified(self):
        for origin in ('recorded','fixture'):
            doc=canonical(); doc['origin']=origin; raw=encode(doc)
            result=self.parse(doc,proof_raw=encode(proof(raw,doc)),expected_program_sha256=PROGRAM)
            self.assertEqual(result['capture_verified'],origin=='recorded')
            self.assertFalse(result['quote_eligibility']); self.assertFalse(result['fill_eligible'])
        self.assertFalse(self.parse(canonical())['capture_verified'])
    def test_feed_source_server_local_and_missing_time(self):
        for kind, label in ((1,'live'),(2,'frozen'),(3,'delayed'),(4,'delayed_frozen'),(None,'unknown')):
            doc=canonical(); doc['rows'][0]['observed_data_type']=kind
            if kind is None: doc['rows'][0]['observed_data_type_at']=None
            doc['rows'][0]['received_at']=None
            result=self.parse(doc); self.assertEqual(result['rows'][0]['feed_classification'],label)
            self.assertEqual(result['rows'][0]['source_freshness'],'unknown_bid_ask_time'); self.assertFalse(result['quote_eligibility'])
        doc=canonical(); self.assertGreater(doc['rows'][0]['last_trade_at'],doc['server_time']); self.parse(doc)
    def test_partial_and_no_requests(self):
        doc=canonical(); doc['status']='partial'; doc['rows'][0].update(status='unavailable',reason='timeout',snapshot_end_observed=False)
        self.assertEqual(self.parse(doc)['rows'][0]['observed_data_type'],1)
        doc=canonical(); doc.update(status='unavailable',account_matched=False,client_id=None,server_time=None)
        for row in doc['rows']: unrequested(row)
        self.assertEqual(len(self.parse(doc)['rows']),3)
    def test_freshness_closed_and_source_future(self):
        self.assertEqual(self.parse(canonical(),as_of='2025-01-06T14:45:40Z')['local_capture_freshness'],'fresh')
        self.assertEqual(self.parse(canonical(),as_of='2025-01-06T14:45:41Z')['local_capture_freshness'],'stale')
        doc=json.loads(encode(canonical()).replace(b'2025-01-06',b'2025-01-04')); self.assertEqual(self.parse(doc,as_of='2025-01-04T14:30:41Z')['regular_session_status'],'closed')
        doc=canonical(); doc['rows'][0]['last_trade_at']='2025-01-06T14:30:42Z'; self.reject(doc)
    def test_root_and_row_invalid_families(self):
        for key,value in [('schema',True),('schema',1.0),('gateway_port',4002.0),('requested_market_data_type',3.0),('startup_fetch',0.0),('client_id',100),('client_id',True),('account_matched',1),('rows',[]),('snapshot',False),('regulatory_snapshot',True),('requested_account_id','wrong'),('completed_at','2025-01-06T14:30:42Z'),('server_time','2025-01-06T14:31:00Z')]:
            with self.subTest(root=key,value=value): doc=canonical(); doc[key]=value; self.reject(doc)
        for key,value in [('symbol','wrong'),('currency','HKD'),('bid','NaN'),('bid','-1'),('bid','0'),('bid','100\n'),('bid',1.0),('ask','1000000001'),('observed_data_type',True),('observed_data_type',5),('snapshot_end_observed',False),('requested_at',None),('requested_at','2025-01-06T14:29:59Z'),('completed_at','2025-01-06T14:30:30Z')]:
            with self.subTest(row=key,value=value): doc=canonical(); doc['rows'][0][key]=value; self.reject(doc)
    def test_proof_tamper_type_future_families(self):
        doc=canonical(); raw=encode(doc); base=proof(raw,doc)
        for key,value in [('schema',True),('schema',1.0),('returncode',True),('returncode',1),('source_sha256','b'*64),('response_sha256','b'*64),('request_sha256','b'*64),('started_at','2025-01-06T14:30:01Z'),('completed_at','2025-01-06T14:30:42Z')]:
            with self.subTest(proof=key,value=value), self.assertRaises(ValueError):
                bad=dict(base); bad[key]=value; self.parse(doc,proof_raw=encode(bad),expected_program_sha256=PROGRAM)
    def test_json_shape_duplicate_and_encoding(self):
        raw=encode(canonical())
        for data in (b'\xff',b'[]',b'null',b'{',b'x'*(1024*1024+1),b'['*2000+b']'*2000,raw.replace(b'"schema": 1',b'"schema": 1, "schema": 1'),raw.replace(b'"bid": "100.01"',b'"bid": NaN'),bytearray(raw)):
            with self.subTest(data=data[:20]), self.assertRaises(ValueError): parse_quote_evidence(data,as_of=ASOF)
        for key in ('rows','kind','source'):
            doc=canonical(); del doc[key]; self.reject(doc)
