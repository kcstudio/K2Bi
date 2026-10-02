"""Renderer, escaping, and CLI tests (invented fixtures only)."""
from __future__ import annotations

import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from tests.test_stage3_paper import AS_OF, base_doc, raw

from scripts.stage3_paper import cli, desk


class Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inside_paper = False
        self.paper_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "section" and attrs.get("id") == "panel-paper":
            self.inside_paper = True

    def handle_endtag(self, tag):
        if tag == "section" and self.inside_paper:
            self.inside_paper = False

    def handle_data(self, data):
        if self.inside_paper:
            self.paper_text.append(data)


class FakeReceipt:
    pass


def _receipt_html():
    return ('<html><body><section id="panel-paper" role="tabpanel" hidden>'
            '<p>old unavailable paper box</p></section>'
            '<section id="panel-words"><div id="tabs"></div></section>'
            '<section id="panel-alpha"></section><section id="panel-beta"></section>'
            '<section id="panel-gamma"></section><section id="panel-delta"></section>'
            '<script>var tabs=6;</script></body></html>')


def _render_with(monkeypatch_eod, snapshot_raw):
    return desk.render(FakeReceipt(), snapshot_raw, as_of=AS_OF)


class DeskTests(unittest.TestCase):
    def setUp(self):
        self._original = desk._eod.render
        desk._eod.render = lambda receipt: _receipt_html()

    def tearDown(self):
        desk._eod.render = self._original

    def test_panel_containment_and_old_box_removed(self):
        page = _render_with(None, raw(base_doc(positions=None, account_values=None)))
        collector = Collector()
        collector.feed(page)
        text = " ".join(collector.paper_text)
        self.assertIn("Paper account snapshot", text)
        self.assertNotIn("old unavailable paper box", page)
        self.assertNotIn("panel-paper-snapshot", page)

    def test_missing_snapshot_unknown(self):
        page = desk.render(FakeReceipt(), None, as_of=AS_OF)
        collector = Collector()
        collector.feed(page)
        self.assertIn("unknown", " ".join(collector.paper_text))

    def test_fixture_and_stale_banners(self):
        fixture = base_doc(origin="fixture")
        page = _render_with(None, raw(fixture))
        self.assertIn("INVENTED OFFLINE BROKER FIXTURE", page)
        stale = base_doc(started_at="2026-10-02T10:59:00+00:00", completed_at="2026-10-02T11:00:00+00:00")
        page = _render_with(None, raw(stale))
        self.assertIn("STALE", page)

    def test_escaping_and_acquisition_semantics(self):
        doc = base_doc(positions=[{"account_id": "DUQ220152", "contract_id": 1,
                                   "symbol": "AAPL", "sec_type": "STK", "currency": "USD",
                                   "exchange": "NASDAQ", "quantity": "10", "average_cost": "150.25"}],
                       reason="bad <script> alert(1) </script>")
        page = _render_with(None, raw(doc))
        self.assertNotIn("<script> alert(1)", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("Average acquisition cost", page)

    def test_useful_content_first_and_source_details_collapsed(self):
        doc = base_doc(account_values={
            "TotalCashValue": {"value": "989055.18", "currency": "HKD"},
            "NetLiquidation": {"value": "1002306.97", "currency": "HKD"},
            "AvailableFunds": {"value": "1000628.58", "currency": "HKD"}})
        page = _render_with(None, raw(doc))
        details = page.index('<details class="st3-details">')
        self.assertLess(page.index('Paper holdings'), details)
        self.assertLess(page.index('Account balances'), details)
        for label in ('Cash', 'Estimated account value', 'Funds available (broker-reported)'):
            self.assertIn(label, page[:details])
        for tag in ('TotalCashValue', 'NetLiquidation', 'AvailableFunds', 'Raw sha256'):
            self.assertIn(tag, page[details:])
            self.assertNotIn(tag, page[:details])
        self.assertNotIn('<details class="st3-details" open', page)
        for value in ('989055.18', '1002306.97', '1000628.58'):
            self.assertIn(value, page[:details])
        self.assertEqual(page[:details].count('HKD'), 3)

    def test_missing_as_of_raises(self):
        with self.assertRaises(ValueError):
            desk.render(FakeReceipt(), None, as_of="not-a-time")


class CliTests(unittest.TestCase):
    def test_render_missing_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            out = Path(tmp) / "out.html"
            code = cli.main(["render", "--state-dir", str(state), "--as-of", AS_OF,
                             "--output", str(out)], out_write=lambda p, h: None)
            self.assertEqual(code, 2)

    def test_render_snapshot_mismatch_and_ancestor_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            snap = Path(tmp) / "snap.json"
            snap.write_bytes(raw(base_doc(account_id="DUQ999999")))
            out = Path(tmp) / "out.html"
            code = cli.main(["render", "--state-dir", str(state), "--snapshot", str(snap),
                             "--as-of", AS_OF, "--output", str(out)], out_write=lambda p, h: None)
            self.assertEqual(code, 2)
            self.assertFalse(out.exists())

    def test_source_bytes_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = Path(tmp) / "snap.json"
            payload = raw(base_doc())
            snap.write_bytes(payload)
            self.assertEqual(snap.read_bytes(), payload)

    def test_protected_source_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            snap = Path(tmp) / "snap.json"
            snap.write_bytes(json.dumps(base_doc()).encode("utf-8"))
            code = cli.main(["render", "--state-dir", str(state), "--snapshot", str(snap),
                             "--as-of", AS_OF, "--output", str(snap)], out_write=lambda p, h: None)
            self.assertEqual(code, 2)


class ActualDeskIntegrationTests(unittest.TestCase):
    def setUp(self):
        from scripts.stage2_eod import store
        from tests.test_stage2_eod import request
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'state'
        self.receipt = store.run_cycle(self.state, request())

    def arguments(self, snapshot=None, output=None):
        args = ['render', '--state-dir', str(self.state), '--as-of', AS_OF,
                '--output', str(output or self.root/'desk.html')]
        if snapshot:
            args += ['--snapshot', str(snapshot)]
        return args

    def test_real_template_tab_containment_and_cli_preserve_receipt_bytes(self):
        source = self.root/'snapshot.json'; source.write_bytes(raw(base_doc()))
        before = {p.name:p.read_bytes() for p in self.state.glob('*.json')}
        self.assertEqual(cli.main(self.arguments(source)), 0)
        page = (self.root/'desk.html').read_text()
        parser = Collector(); parser.feed(page)
        self.assertIn('Paper account snapshot', ' '.join(parser.paper_text))
        self.assertNotIn('Paper-account holdings unknown', page)
        for name in ('overview', 'companies', 'trades', 'practice', 'paper', 'words'):
            self.assertIn('id="tab-'+name+'"', page)
        self.assertEqual(before, {p.name:p.read_bytes() for p in self.state.glob('*.json')})
        self.assertEqual(source.read_bytes(), raw(base_doc()))

    def test_actual_snapshot_mismatch_ancestor_symlink_and_collision(self):
        source = self.root/'snapshot.html'; source.write_bytes(raw(base_doc(account_id='DUQ999999')))
        self.assertEqual(cli.main(self.arguments(source)), 2)
        self.assertFalse((self.root/'desk.html').exists())
        source.write_bytes(raw(base_doc()))
        link = self.root/'linked'; link.symlink_to(self.root, target_is_directory=True)
        self.assertEqual(cli.main(self.arguments(link/source.name)), 2)
        self.assertEqual(cli.main(self.arguments(source, source)), 2)
        self.assertEqual(source.read_bytes(), raw(base_doc()))
        protected = Path(__file__).resolve().parents[1]/'scripts/stage3_paper/bad.html'
        self.assertEqual(cli.main(self.arguments(source, protected)), 2)
        self.assertFalse(protected.exists())
