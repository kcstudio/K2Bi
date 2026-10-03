"""Offline end-to-end readiness, repeat and failure acceptance without broker reads."""
import copy
import json
import unittest
from unittest.mock import patch
from scripts.stage3_paper import preparation, desk
from tests.test_stage3_preparation import _risk_raw, _recovery_raw, _config_bytes, AS_OF
from tests.test_stage3_paper_desk import FakeReceipt, _receipt_html


class CampaignAcceptanceTests(unittest.TestCase):
    def test_invented_rules_recovery_render_repeat_and_restart(self):
        risk, recovery = _risk_raw(), _recovery_raw()
        first = preparation.build_preparation(risk, recovery, as_of=AS_OF, config_raw=_config_bytes())
        self.assertEqual(first['status'], 'INVENTED OFFLINE PREVIEW')
        self.assertEqual((first['estimated_cost'], first['estimated_risk']), ('100', '1'))
        self.assertFalse(any(first[k] for k in ('approved','executable','fill_eligible','applied')))
        with patch.object(desk._eod, 'render', return_value=_receipt_html()):
            pages = [desk.render(FakeReceipt(), None, as_of=AS_OF, risk_raw=risk, recovery_raw=recovery, config_raw=_config_bytes()) for _ in range(2)]
            self.assertEqual(*pages); self.assertIn('Buy SPY', pages[0]); self.assertIn('NO PROPOSAL: WAIT', pages[0])
        second = preparation.build_preparation(bytes(risk), bytes(recovery), as_of=AS_OF, config_raw=_config_bytes())
        self.assertEqual(first, second)
    def test_actual_recorded_missing_stale_and_tamper_hold(self):
        recorded = json.loads(_recovery_raw()); recorded['origin'] = 'recorded'
        for risk, recovery, at in [(_risk_raw(origin='recorded'), json.dumps(recorded).encode(), AS_OF), (None,None,AS_OF), (_risk_raw(),_recovery_raw(),'2024-03-04T16:00:00Z')]:
            with self.subTest(at=at, missing=risk is None):
                result = preparation.build_preparation(risk,recovery,as_of=at,config_raw=_config_bytes())
                self.assertEqual(result['status'],'WAIT')
                self.assertTrue(all(result[k] is None for k in ('candidate','quantity','estimated_cost','estimated_risk')))
        for raw in (b'{}', b'{"schema":1,"schema":1}', b'\xff'):
            with self.assertRaises(ValueError): preparation.build_preparation(raw,None,as_of=AS_OF,config_raw=_config_bytes())
