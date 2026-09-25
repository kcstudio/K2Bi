"""Offline operational tests against the real accepted pipeline."""
import datetime as dt
import fcntl
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import pilot

UTC = dt.timezone.utc

def now(day='2026-09-25', hour=22):
    return dt.datetime.fromisoformat(day).replace(hour=hour, tzinfo=UTC)


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / 'config.json'
        self.cfg = {'pilot_id':'us-sec-2026-09-25','start_date':'2026-09-25','sec_contact':'operator@test.org'}
        self.config.write_text(json.dumps(self.cfg))
        self.state = self.root / 'state'
        self.pipeline, self.sources = pilot.modules()
        bundle = {s:{'status':'missing','reason':'offline fixture'} for s in self.pipeline.UNIVERSE}
        self.download = patch.object(self.sources, 'download', return_value=(bundle, 3)).start()
        self.addCleanup(patch.stopall)

    def run_at(self, day='2026-09-25', hour=22, **kwargs):
        return pilot.run(self.config, self.state, now(day,hour), **kwargs)

    def test_five_sessions_and_no_sixth(self):
        for day in ['2026-09-25','2026-09-28','2026-09-29','2026-09-30','2026-10-01']:
            result = self.run_at(day)
            self.assertNotEqual(result['status'], 'integrity_error', result)
        self.assertEqual(result['status'], 'pilot_complete')
        self.assertEqual(result['requests_reserved'],15)
        self.assertEqual(self.download.call_count,5)
        self.assertEqual(self.run_at('2026-10-02')['status'],'pilot_complete')
        self.assertEqual(self.download.call_count,5)
        receipts,_ = pilot.corpus(self.pipeline,self.state)
        self.assertEqual(receipts[-1]['state']['resources']['source_requests'],15)
        self.assertTrue(all(d['action']=='abstain' for r in receipts for d in r['decisions']))
        self.assertIn('No market prices or new trades', (self.state/'dashboard.html').read_text())

    def test_first_manual_and_replay(self):
        self.assertEqual(self.run_at(hour=14,first_proof=True)['status'],'ready')
        self.assertEqual(self.run_at()['status'],'waiting')
        self.assertEqual(self.run_at('2026-09-28',14,first_proof=True)['status'],'waiting')
        self.assertEqual(self.download.call_count,1)
        self.assertIsInstance(self.sources.UA,dict)

    def test_calendar_dst_holiday_weekend(self):
        self.assertEqual(pilot.eligible(now('2026-11-02',22),'2026-09-25'),'2026-10-30')
        self.assertEqual(pilot.eligible(now('2026-11-02',23),'2026-09-25'),'2026-11-02')
        self.assertEqual(pilot.eligible(now('2026-11-26',23),'2026-09-25'),'2026-11-25')
        self.assertEqual(pilot.eligible(now('2026-09-27'),'2026-09-25'),'2026-09-25')
        self.assertIsNone(pilot.eligible(now('2026-09-26'),'2026-09-25',True))
        self.assertIsNone(pilot.eligible(now(hour=14),'2026-09-25'))

    def test_config_family(self):
        variants=[{},None,dict(self.cfg,x=1),dict(self.cfg,start_date=3),dict(self.cfg,start_date='2026-02-30')]
        variants += [dict(self.cfg,sec_contact=v) for v in (None,3,'','nobody','a@example.com','a@test.org\r\nX:1')]
        for cfg in variants:
            with self.subTest(cfg=cfg):
                with self.assertRaises((ValueError,TypeError)):
                    pilot.validate_config(cfg)
        self.assertEqual(pilot.validate_config(self.cfg), self.cfg)

    def test_control_missing_or_corrupt(self):
        self.run_at()
        control=(self.state/'control.json').read_bytes()
        for data in (None,b'{}',b'{invalid'):
            with self.subTest(data=data):
                if data is None: (self.state/'control.json').unlink()
                else: (self.state/'control.json').write_bytes(data)
                self.assertEqual(self.run_at('2026-09-28')['status'],'integrity_error')
                (self.state/'control.json').write_bytes(control)
                (self.state/'PAUSED').unlink(missing_ok=True)
        self.assertEqual(self.download.call_count,1)

    def test_changed_config_and_code(self):
        self.run_at()
        self.config.write_text(json.dumps(dict(self.cfg,start_date='2026-09-24')))
        self.assertEqual(self.run_at('2026-09-28')['status'],'integrity_error')
        self.config.write_text(json.dumps(self.cfg)); (self.state/'PAUSED').unlink()
        with patch.object(pilot,'verify_code',return_value={'pilot':'changed','vendor':'changed'}):
            self.assertEqual(self.run_at('2026-09-28')['status'],'integrity_error')
        self.assertEqual(self.download.call_count,1)

    def test_wrapper_reservation_before_intent_recovery(self):
        real = self.pipeline.run_cycle
        with patch.object(self.pipeline,'run_cycle',side_effect=RuntimeError('process lost before intent')):
            self.assertEqual(self.run_at()['status'],'integrity_error')
        self.assertEqual(self.download.call_count,0)
        (self.state/'PAUSED').unlink()
        self.assertEqual(self.run_at('2026-09-28')['status'],'paused')
        self.assertEqual(self.download.call_count,0)
        self.assertEqual(pilot.read(self.state/'status.json')['session'],'2026-09-25')

    def test_pending_intent_recovery_does_not_fetch_again(self):
        real = self.pipeline.run_cycle
        with patch.object(self.pipeline,'run_cycle',side_effect=lambda req,out,limits:real(req,out,limits,crash='after_intent')):
            self.assertEqual(self.run_at()['status'],'integrity_error')
        (self.state/'PAUSED').unlink()
        self.assertEqual(self.run_at('2026-09-28')['status'],'paused')
        self.assertEqual(self.download.call_count,0)
        receipts,_=pilot.corpus(self.pipeline,self.state)
        self.assertEqual(receipts[0]['state']['resources']['source_requests'],3)
        self.assertEqual(receipts[0]['status'],'interrupted')

    def test_prepared_recovery_no_duplicate_fetch(self):
        real=self.pipeline.run_cycle
        with patch.object(self.pipeline,'run_cycle',side_effect=lambda req,out,limits:real(req,out,limits,crash='after_prepared')):
            self.run_at()
        self.assertEqual(self.download.call_count,1)
        (self.state/'PAUSED').unlink()
        self.assertEqual(self.run_at('2026-09-28')['status'],'ready')
        self.assertEqual(self.download.call_count,1)

    def test_corrupt_receipt_and_slots(self):
        self.run_at()
        receipt=next((self.state/'history/cycles').glob('*/receipt.json'))
        original=receipt.read_bytes(); receipt.write_text('{}')
        self.assertEqual(self.run_at('2026-09-28')['status'],'integrity_error')
        receipt.write_bytes(original); (self.state/'PAUSED').unlink()
        control=pilot.read(self.state/'control.json');control['slots']={}
        pilot.write(self.state/'control.json',control)
        self.assertEqual(self.run_at('2026-09-28')['status'],'integrity_error')
        self.assertEqual(self.download.call_count,1)

    def test_source_failure_pauses_without_retry(self):
        bundle={s:{'status':'missing','reason':'offline'} for s in self.pipeline.UNIVERSE}
        bundle['G']={'status':'failed','reason':'HTTP403 <unavailable>'}
        self.download.return_value=(bundle,3)
        self.assertEqual(self.run_at()['status'],'paused')
        self.assertEqual(self.run_at('2026-09-28')['status'],'paused')
        self.assertEqual(self.download.call_count,1)
        self.assertIn('&lt;unavailable&gt;', (self.state/'dashboard.html').read_text())

    def test_pause_and_lock(self):
        self.state.mkdir();(self.state/'PAUSED').write_text('operator stop')
        # Preexisting control-less files reject initialization and never fetch.
        self.assertEqual(self.run_at()['status'],'integrity_error')
        with (self.state/'.lock').open('a+') as f:
            fcntl.flock(f, fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.assertEqual(self.run_at()['status'],'busy')
        self.assertEqual(self.download.call_count,0)

    def test_symlink_and_naive_time(self):
        self.state.symlink_to(self.root/'elsewhere')
        with self.assertRaises(ValueError): self.run_at()
        self.state.unlink()
        self.state.mkdir();(self.state/'control.json').symlink_to(self.config)
        with self.assertRaises(ValueError): self.run_at()
        with self.assertRaises(ValueError): pilot.run(self.config,self.state,dt.datetime(2026,9,25))
        self.assertEqual(self.download.call_count,0)


if __name__=='__main__':
    unittest.main()
