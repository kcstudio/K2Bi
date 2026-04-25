"""Tests for canonical ticker registry builder and loader."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.lib.canonical_ticker_registry import (
    _fetch_exchange,
    _parse_api_rows,
    _registry_path,
    load_registry,
    refresh_registry,
)


class RegistryPathTests(unittest.TestCase):
    def test_registry_path_resolves_under_vault(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            path = _registry_path(td_path)
            self.assertEqual(path, td_path / "wiki" / "tickers" / "canonical-registry.json")


class LoadRegistryTests(unittest.TestCase):
    def test_load_registry_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as td:
            result = load_registry(Path(td))
            self.assertEqual(result, {})

    def test_load_registry_returns_data_when_present(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            registry_path = td_path / "wiki" / "tickers" / "canonical-registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            data = {"AAPL": {"exchange": "NASDAQ", "name": "Apple Inc."}}
            registry_path.write_text(json.dumps(data))
            result = load_registry(td_path)
            self.assertEqual(result["AAPL"]["name"], "Apple Inc.")

    def test_load_registry_returns_empty_on_bad_json(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            registry_path = td_path / "wiki" / "tickers" / "canonical-registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text("not json")
            result = load_registry(td_path)
            self.assertEqual(result, {})

    def test_load_registry_returns_empty_on_non_dict_json(self):
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            registry_path = td_path / "wiki" / "tickers" / "canonical-registry.json"
            registry_path.parent.mkdir(parents=True, exist_ok=True)
            registry_path.write_text("[1, 2, 3]")
            result = load_registry(td_path)
            self.assertEqual(result, {})


class FetchExchangeTests(unittest.TestCase):
    def _make_resp(self, rows, totalrecords):
        data = json.dumps({"data": {"table": {"rows": rows}, "totalrecords": totalrecords}}).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = data
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("scripts.lib.canonical_ticker_registry._PAGE_SIZE", 2)
    @patch("scripts.lib.canonical_ticker_registry.urllib.request.urlopen")
    def test_fetch_exchange_paginates(self, mock_urlopen):
        page1 = [{"symbol": "A", "name": "A Inc."}, {"symbol": "B", "name": "B Inc."}]
        page2 = [{"symbol": "C", "name": "C Inc."}]

        def side_effect(req, **kwargs):
            url = req.full_url
            if "offset=0" in url:
                return self._make_resp(page1, 3)
            else:
                return self._make_resp(page2, 3)

        mock_urlopen.side_effect = side_effect
        result = _fetch_exchange("NASDAQ")
        self.assertEqual(len(result), 3)
        symbols = {r["symbol"] for r in result}
        self.assertEqual(symbols, {"A", "B", "C"})

    @patch("scripts.lib.canonical_ticker_registry.urllib.request.urlopen")
    def test_fetch_exchange_stops_on_empty_page(self, mock_urlopen):
        mock_urlopen.return_value = self._make_resp([], 0)
        result = _fetch_exchange("NYSE")
        self.assertEqual(result, [])


class ParseApiRowsTests(unittest.TestCase):
    def test_parse_api_rows_basic(self):
        rows = [
            {"symbol": "AAPL", "name": "Apple Inc."},
            {"symbol": "TSLA", "name": "Tesla, Inc."},
        ]
        result = _parse_api_rows(rows, "NASDAQ")
        self.assertEqual(len(result), 2)
        self.assertEqual(result["AAPL"]["exchange"], "NASDAQ")
        self.assertEqual(result["AAPL"]["name"], "Apple Inc.")

    def test_parse_api_rows_skips_empty_symbol(self):
        rows = [
            {"symbol": "", "name": "Empty"},
            {"symbol": "NVDA", "name": "NVIDIA"},
        ]
        result = _parse_api_rows(rows, "NYSE")
        self.assertEqual(len(result), 1)
        self.assertIn("NVDA", result)


class RefreshRegistryTests(unittest.TestCase):
    def _make_resp(self, rows, totalrecords):
        data = json.dumps({"data": {"table": {"rows": rows}, "totalrecords": totalrecords}}).encode("utf-8")
        resp = MagicMock()
        resp.read.return_value = data
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("scripts.lib.canonical_ticker_registry.urllib.request.urlopen")
    def test_refresh_registry_writes_file(self, mock_urlopen):
        nasdaq_page = [{"symbol": "AAPL", "name": "Apple Inc."}]
        nyse_page = [{"symbol": "IBM", "name": "IBM Corp."}]

        def side_effect(req, **kwargs):
            url = req.full_url
            if "exchange=NASDAQ" in url:
                return self._make_resp(nasdaq_page, 1)
            else:
                return self._make_resp(nyse_page, 1)

        mock_urlopen.side_effect = side_effect

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            registry = refresh_registry(td_path)
            self.assertEqual(len(registry), 2)
            self.assertIn("AAPL", registry)
            self.assertIn("IBM", registry)
            # Verify file was written
            path = td_path / "wiki" / "tickers" / "canonical-registry.json"
            self.assertTrue(path.exists())
            loaded = json.loads(path.read_text())
            self.assertIn("AAPL", loaded)

    @patch("scripts.lib.canonical_ticker_registry.urllib.request.urlopen")
    def test_refresh_registry_includes_last_updated(self, mock_urlopen):
        page = [{"symbol": "X", "name": "X Corp."}]
        mock_urlopen.return_value = self._make_resp(page, 1)

        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td)
            registry = refresh_registry(td_path)
            self.assertIn("last_updated_utc", registry["X"])


if __name__ == "__main__":
    unittest.main()
