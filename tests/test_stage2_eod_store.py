"""Durable replay, conflict, crash and corruption checks."""
import copy
import fcntl
import tempfile
import unittest
from pathlib import Path
from scripts.stage2_eod import inputs, store
from tests.test_stage2_eod import TIMES, request


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_crash_frozen_recovery_idempotency_and_conflict(self):
        with self.assertRaises(RuntimeError):
            store.run_cycle(self.path, request(), fault='after_intent')
        with self.assertRaises(ValueError): store.run_cycle(self.path, request(close='101'))
        first = store.recover(self.path)
        self.assertEqual(store.run_cycle(self.path, request()), first)
        with self.assertRaises(RuntimeError):
            store.run_cycle(self.path, request(TIMES[1]), fault='after_receipt')
        second = store.recover(self.path)
        self.assertEqual(len(second['state']['books']['research']['events']), 1)
        self.assertEqual(store.run_cycle(self.path, request(TIMES[1])), second)

    def test_all_history_corruption_and_resealed_output_fail_closed(self):
        store.run_cycle(self.path, request()); store.run_cycle(self.path, request(TIMES[1]))
        path = self.path/'journal.json'; original = path.read_bytes()
        mutations = []
        obj = inputs.decode(original); obj['entries'][0]['request']['bars']['G']['close'] = '101'; mutations.append(obj)
        obj = inputs.decode(original); obj['entries'].pop(0); mutations.append(obj)
        obj = inputs.decode(original); obj['entries'].pop(); mutations.append(obj)
        obj = inputs.decode(original); rec = obj['entries'][-1]['receipt']; rec['decisions'][0]['reason'] = 'invented tamper'; rec['receipt_hash'] = inputs.digest({k:v for k,v in rec.items() if k!='receipt_hash'}); mutations.append(obj)
        obj = inputs.decode(original); obj['entries'][-1]['receipt']['state']['books']['research']['cash'] = '10000.00'; mutations.append(obj)
        for index, obj in enumerate(mutations):
            # Output tampering is resealed through both envelopes to exercise replay.
            if index in (3, 4):
                obj['journal_hash'] = inputs.digest({k:v for k,v in obj.items() if k != 'journal_hash'})
            path.write_bytes(inputs.canonical(obj))
            with self.assertRaises(ValueError): store.latest(self.path)
            with self.assertRaises(ValueError): store.recover(self.path)
        path.write_bytes(b'{broken')
        with self.assertRaises(ValueError): store.latest(self.path)
        path.write_bytes(original); path.unlink()
        with self.assertRaises(ValueError): store.latest(self.path)

    def test_invalid_request_not_persisted_and_lock_is_mandatory(self):
        invalid = request(); invalid['bars']['G']['close'] = None
        with self.assertRaises(ValueError): store.run_cycle(self.path, invalid)
        self.assertFalse((self.path/'journal.json').exists())
        store.run_cycle(self.path, request())
        with (self.path/'lock').open('r+') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ValueError): store.latest(self.path)
            with self.assertRaises(ValueError): store.run_cycle(self.path, request(TIMES[1]))

    def test_arbitrary_symlink_and_dangling_state_rejected(self):
        link = self.path/'link'; link.symlink_to(self.path/'absent')
        with self.assertRaises(ValueError): store.run_cycle(link, request())
        link.unlink(); link.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(ValueError): store.run_cycle(link/'nested', request())

    def test_read_only_json_bytes_and_pending_recovery(self):
        first = store.run_cycle(self.path, request())
        before = {p.name:p.read_bytes() for p in self.path.glob('*.json')}
        self.assertEqual(store.latest(self.path), first)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.path.glob('*.json')})
        with self.assertRaises(RuntimeError): store.run_cycle(self.path, request(TIMES[1]), fault='after_intent')
        self.assertEqual(store.latest(self.path), first)
        self.assertEqual(len(store.recover(self.path)['state']['books']['research']['events']), 1)
