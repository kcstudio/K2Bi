"""Tests for invest-narrative Ship 2 validators."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, PropertyMock, patch

from scripts.lib.invest_narrative_validators import (
    ValidatorSkipped,
    validate_citation_url,
    validate_liquidity,
    validate_market_cap,
    validate_priced_in,
    validate_ticker_exists,
)


class TickerExistsTests(unittest.TestCase):
    def test_validate_ticker_exists_true(self):
        registry = {"NVDA": {"exchange": "NASDAQ", "name": "NVIDIA"}}
        self.assertTrue(validate_ticker_exists("NVDA", registry))

    def test_validate_ticker_exists_false(self):
        registry = {"NVDA": {"exchange": "NASDAQ", "name": "NVIDIA"}}
        self.assertFalse(validate_ticker_exists("XYZQQ", registry))

    def test_validate_ticker_exists_case_insensitive(self):
        registry = {"nvda": {"exchange": "NASDAQ", "name": "NVIDIA"}}
        self.assertTrue(validate_ticker_exists("NVDA", registry))

    def test_validate_ticker_exists_empty_registry(self):
        self.assertFalse(validate_ticker_exists("AAPL", {}))


class MarketCapTests(unittest.TestCase):
    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_market_cap_pass(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {"marketCap": 5_000_000_000}
        mock_ticker_cls.return_value = mock_ticker
        self.assertTrue(validate_market_cap("NVDA"))

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_market_cap_fail(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {"marketCap": 500_000_000}
        mock_ticker_cls.return_value = mock_ticker
        self.assertFalse(validate_market_cap("TINY"))

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_market_cap_skipped_on_none(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker.fast_info = MagicMock()
        mock_ticker.fast_info.market_cap = None
        mock_ticker_cls.return_value = mock_ticker
        with self.assertRaises(ValidatorSkipped):
            validate_market_cap("UNKNOWN")

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_market_cap_skipped_on_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = RuntimeError("network down")
        with self.assertRaises(ValidatorSkipped):
            validate_market_cap("ANY")

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_market_cap_uses_fast_info_fallback(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = {}
        mock_ticker.fast_info = MagicMock()
        mock_ticker.fast_info.market_cap = 3_000_000_000
        mock_ticker_cls.return_value = mock_ticker
        self.assertTrue(validate_market_cap("FALLBACK"))

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_market_cap_fast_info_when_info_raises(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.info = PropertyMock(side_effect=RuntimeError("info flake"))
        mock_ticker.fast_info = MagicMock()
        mock_ticker.fast_info.market_cap = 3_000_000_000
        mock_ticker_cls.return_value = mock_ticker
        self.assertTrue(validate_market_cap("FLAKE"))


class LiquidityTests(unittest.TestCase):
    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_liquidity_pass(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        import pandas as pd

        hist = pd.DataFrame({
            "Close": [100.0, 101.0, 102.0],
            "Volume": [1_000_000, 1_500_000, 2_000_000],
        })
        mock_ticker.history.return_value = hist
        mock_ticker_cls.return_value = mock_ticker
        # Avg dollar volume = (100*1M + 101*1.5M + 102*2M) / 3 = 154.8M > 10M
        self.assertTrue(validate_liquidity("LIQUID"))

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_liquidity_fail(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        import pandas as pd

        hist = pd.DataFrame({
            "Close": [1.0, 1.0, 1.0],
            "Volume": [100, 100, 100],
        })
        mock_ticker.history.return_value = hist
        mock_ticker_cls.return_value = mock_ticker
        self.assertFalse(validate_liquidity("ILLIQUID"))

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_liquidity_skipped_on_empty_history(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        import pandas as pd

        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker
        with self.assertRaises(ValidatorSkipped):
            validate_liquidity("EMPTY")

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_liquidity_skipped_on_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = RuntimeError("network down")
        with self.assertRaises(ValidatorSkipped):
            validate_liquidity("ANY")


class PricedInTests(unittest.TestCase):
    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_priced_in_flagged(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        import pandas as pd

        hist = pd.DataFrame({
            "Close": [100.0, 200.0],
        })
        mock_ticker.history.return_value = hist
        mock_ticker_cls.return_value = mock_ticker
        result = validate_priced_in("DOUBLED")
        self.assertTrue(result["flagged"])
        self.assertAlmostEqual(result["gain"], 1.0)
        self.assertFalse(result["skipped"])

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_priced_in_not_flagged(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        import pandas as pd

        hist = pd.DataFrame({
            "Close": [100.0, 105.0],
        })
        mock_ticker.history.return_value = hist
        mock_ticker_cls.return_value = mock_ticker
        result = validate_priced_in("STABLE")
        self.assertFalse(result["flagged"])
        self.assertAlmostEqual(result["gain"], 0.05)
        self.assertFalse(result["skipped"])

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_priced_in_skipped_on_empty(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        import pandas as pd

        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker
        result = validate_priced_in("EMPTY")
        self.assertFalse(result["flagged"])
        self.assertTrue(result["skipped"])

    @patch("scripts.lib.invest_narrative_validators.yf.Ticker")
    def test_validate_priced_in_skipped_on_exception(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = RuntimeError("network down")
        result = validate_priced_in("ANY")
        self.assertFalse(result["flagged"])
        self.assertTrue(result["skipped"])


class CitationUrlTests(unittest.TestCase):
    def _make_resp(self, status: int = 200):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    @patch("scripts.lib.invest_narrative_validators.urllib.request.urlopen")
    def test_validate_citation_url_200(self, mock_urlopen):
        mock_urlopen.return_value = self._make_resp(200)
        self.assertTrue(validate_citation_url("https://example.com/news"))

    @patch("scripts.lib.invest_narrative_validators.urllib.request.urlopen")
    def test_validate_citation_url_404(self, mock_urlopen):
        from urllib.error import HTTPError

        mock_urlopen.side_effect = HTTPError(
            "https://example.com/missing", 404, "Not Found", {}, None
        )
        self.assertFalse(validate_citation_url("https://example.com/missing"))

    @patch("scripts.lib.invest_narrative_validators.urllib.request.urlopen")
    def test_validate_citation_url_timeout(self, mock_urlopen):
        import socket

        mock_urlopen.side_effect = socket.timeout("timed out")
        self.assertFalse(validate_citation_url("https://example.com/slow"))

    @patch("scripts.lib.invest_narrative_validators.urllib.request.urlopen")
    def test_validate_citation_url_head_405_fallback_to_get(self, mock_urlopen):
        from urllib.error import HTTPError

        def side_effect(req, **kwargs):
            if req.get_method() == "HEAD":
                raise HTTPError("https://example.com/page", 405, "Method Not Allowed", {}, None)
            return self._make_resp(200)

        mock_urlopen.side_effect = side_effect
        self.assertTrue(validate_citation_url("https://example.com/page"))

    @patch("scripts.lib.invest_narrative_validators.urllib.request.urlopen")
    def test_validate_citation_url_head_403_fallback_to_get(self, mock_urlopen):
        from urllib.error import HTTPError

        def side_effect(req, **kwargs):
            if req.get_method() == "HEAD":
                raise HTTPError("https://example.com/page", 403, "Forbidden", {}, None)
            return self._make_resp(200)

        mock_urlopen.side_effect = side_effect
        self.assertTrue(validate_citation_url("https://example.com/page"))


if __name__ == "__main__":
    unittest.main()
