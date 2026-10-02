"""Receipt driven accepted UI, escaping and offline CLI checks."""
import subprocess
from html.parser import HTMLParser
import sys
import tempfile
import unittest
from pathlib import Path
from scripts.stage2_eod import cli, desk, inputs, store
from tests.test_stage2_eod import ROOT, TIMES, request


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)

    def test_receipt_data_escaped_and_six_tabs_retained(self):
        req = request(); req['analyses']['G']['reason'] = '<script>alert("x")</script>'
        rec = store.run_cycle(self.path/'state', req)
        page = desk.render(rec)
        self.assertIn('&lt;script&gt;', page)
        self.assertNotIn('<script>alert(', page)
        for name in ('overview', 'companies', 'trades', 'practice', 'paper', 'words'):
            self.assertIn(f'id="tab-{name}"', page)
            self.assertIn(f'id="panel-{name}"', page)
        for name in ('open-trade-guide', 'example-toggle', 'example-details'):
            self.assertIn(f'id="{name}"', page)
        self.assertIn('INVENTED OFFLINE FIXTURE', page)
        self.assertIn('Paper-account holdings unknown', page)
        self.assertNotIn('2 shares of SPY', page)
        self.assertIn('2026-09-28', page)
        self.assertNotIn('{{', page)
        rec['receipt_hash'] = '0'*64
        with self.assertRaises(ValueError): desk.render(rec)

    def test_footer_preserves_complete_identifiers_without_truncation(self):
        class FooterText(HTMLParser):
            def __init__(self):
                super().__init__()
                self.inside = False
                self.fragments = []

            def handle_starttag(self, tag, attrs):
                if tag == 'footer':
                    self.inside = True

            def handle_endtag(self, tag):
                if tag == 'footer':
                    self.inside = False

            def handle_data(self, data):
                if self.inside:
                    self.fragments.append(data)

        rec = store.run_cycle(self.path/'state', request())
        footer = FooterText(); footer.feed(desk.render(rec))
        text = ''.join(footer.fragments)
        for field in ('receipt_hash', 'request_sha256'):
            self.assertEqual(len(rec[field]), 64)
            self.assertIn(rec[field], text)
        self.assertIn(rec['request']['as_of'], text)

    def test_all_external_links_match_exact_permitted_companyfacts_sources(self):
        class Links(HTMLParser):
            def __init__(self):
                super().__init__()
                self.urls = []

            def handle_starttag(self, tag, attrs):
                if tag == 'a':
                    self.urls.extend(value for key, value in attrs if key == 'href')

        req = inputs.from_saved_evidence(
            (ROOT/'proposals/dashboard-ux-2026-09-26/first-cycle-receipt.json').read_bytes(),
            (ROOT/'proposals/stage2-ibkr-price-check-2026-09-26/ibkr-five-daily.json').read_bytes(),
            as_of='2026-09-26T04:00:00Z')
        rec = store.run_cycle(self.path/'state', req)
        links = Links(); links.feed(desk.render(rec))
        self.assertEqual(set(links.urls), {inputs.sec_url(symbol) for symbol in ('G', 'CALX', 'CDNS')})
        invented = request()
        invented['evidence']['companies'] = {'G': {'industry': 'invented', 'source': {
            'url': 'https://data.sec.gov/api/xbrl/companyfacts/CIK0001398659.json?redirect=evil'}}}
        rec = store.run_cycle(self.path/'invented', invented)
        links = Links(); links.feed(desk.render(rec))
        self.assertEqual(links.urls, [])

    def test_holdings_events_fees_and_wait_reasons_are_receipt_values(self):
        store.run_cycle(self.path/'state', request())
        rec = store.run_cycle(self.path/'state', request(TIMES[1]))
        page = desk.render(rec)
        for value in ('8098.55', '19', '100.0500', '.50', 'daily bars cannot support intraday practice'):
            self.assertIn(value, page)

    def test_cli_real_replay_rebuild_bytes_and_protected_destinations(self):
        state, out = self.path/'state', self.path/'desk.html'
        command = [sys.executable, '-m', 'scripts.stage2_eod.cli', 'run', '--research',
            str(ROOT/'proposals/dashboard-ux-2026-09-26/first-cycle-receipt.json'), '--prices',
            str(ROOT/'proposals/stage2-ibkr-price-check-2026-09-26/ibkr-five-daily.json'),
            '--as-of', '2026-09-26T04:00:00Z', '--state-dir', str(state), '--output', str(out)]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = {p.name:p.read_bytes() for p in state.glob('*.json')}; page = out.read_bytes()
        result = subprocess.run([sys.executable, '-m', 'scripts.stage2_eod.cli', 'rebuild', '--state-dir', str(state), '--output', str(out)], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, {p.name:p.read_bytes() for p in state.glob('*.json')})
        self.assertEqual(page, out.read_bytes())
        for target in (ROOT/'scripts/stage2_eod/no.html', ROOT/'proposals/dashboard-ux-2026-09-26/no.html', state/'journal.json'):
            with self.assertRaises(ValueError): cli.destinations(state, target)
        link = self.path/'linked.html'; link.symlink_to(out)
        with self.assertRaises(ValueError): cli.destinations(state, link)
        self.assertEqual(cli.main(['rebuild', '--state-dir', str(self.path/'missing'), '--output', str(out)]), 2)
