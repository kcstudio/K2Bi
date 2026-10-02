"""Offline unit tests for the Stage 2 daily bar contract."""

from __future__ import annotations

import copy
import unittest

from scripts.stage2_quotes.contract import validate_daily_bar


VALID = {
    "symbol": "AAPL",
    "source": "nasdaq-trader",
    "source_url": "https://api.example.com/daily/AAPL",
    "session_date": "2026-01-02",
    "retrieved_at": "2026-01-03T00:00:00+00:00",
    "currency": "USD",
    "close": "123.45000000",
    "adjusted": True,
    "raw_sha256": "a" * 64,
}

ALLOWED_HOSTS = {"api.example.com"}
AS_OF = "2026-01-06T00:00:00+00:00"


class DailyBarContractTests(unittest.TestCase):
    """Exercise required validation branches without network or file I/O."""

    def validate(self, record=None, **overrides):
        """Validate a copy of the valid record with optional overrides."""
        candidate = copy.deepcopy(VALID if record is None else record)
        candidate.update(overrides)
        return validate_daily_bar(
            candidate,
            expected_symbol="AAPL",
            as_of=AS_OF,
            allowed_hosts=ALLOWED_HOSTS,
        )

    def test_valid_record(self):
        """A valid record returns canonical values."""
        result = self.validate()
        self.assertEqual(result["symbol"], "AAPL")
        self.assertEqual(result["retrieved_at"], "2026-01-03T00:00:00+00:00")
        self.assertEqual(result["close"], "123.45000000")
        self.assertIs(result["adjusted"], True)

    def test_missing_key(self):
        """A missing required key is rejected."""
        candidate = copy.deepcopy(VALID)
        del candidate["currency"]
        with self.assertRaises(ValueError):
            self.validate(candidate)

    def test_extra_key(self):
        """An extra key is rejected."""
        with self.assertRaises(ValueError):
            self.validate(extra="x")

    def test_wrong_type(self):
        """Wrong field types are rejected."""
        with self.assertRaises(ValueError):
            self.validate(symbol=123)
        with self.assertRaises(ValueError):
            self.validate(close=123.45)
        with self.assertRaises(ValueError):
            self.validate(adjusted=1)

    def test_empty_or_malformed_url(self):
        """Empty or malformed URLs are rejected."""
        with self.assertRaises(ValueError):
            self.validate(source_url="")
        with self.assertRaises(ValueError):
            self.validate(source_url="not a url")
        with self.assertRaises(ValueError):
            self.validate(source_url="http://api.example.com/x")

    def test_url_query_and_fragment_rejected(self):
        """Query strings and fragments are rejected."""
        with self.assertRaises(ValueError):
            self.validate(source_url="https://api.example.com/x?q=1")
        with self.assertRaises(ValueError):
            self.validate(source_url="https://api.example.com/x#frag")
        with self.assertRaises(ValueError):
            self.validate(source_url="https://api.example.com/x?")
        with self.assertRaises(ValueError):
            self.validate(source_url=" https://api.example.com/x")

    def test_wrong_host(self):
        """Hostnames must exactly match the allow-list."""
        with self.assertRaises(ValueError):
            self.validate(source_url="https://other.example.com/x")
        with self.assertRaises(ValueError):
            self.validate(source_url="https://user@api.example.com/x")
        with self.assertRaises(ValueError):
            self.validate(source_url="https://api.example.com:443/x")
        with self.assertRaises(ValueError):
            validate_daily_bar(
                copy.deepcopy(VALID), expected_symbol="AAPL", as_of=AS_OF,
                allowed_hosts=set(),
            )

    def test_source_slug_rejected(self):
        """The source identifier must be a nonempty lowercase slug."""
        for source in ("", "Bad Source", None, 3):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    self.validate(source=source)

    def test_symbol_mismatch(self):
        """The record symbol must equal expected_symbol."""
        with self.assertRaises(ValueError):
            self.validate(symbol="MSFT")

    def test_invalid_currency(self):
        """Only USD is accepted."""
        with self.assertRaises(ValueError):
            self.validate(currency="EUR")

    def test_invalid_adjustment(self):
        """Adjustment must be a real bool."""
        with self.assertRaises(ValueError):
            self.validate(adjusted="true")

    def test_invalid_close(self):
        """Close must be a positive bounded decimal string."""
        for value in ("0", "0.0", "-1.00", "1e2", "NaN", "Infinity", "1000000.000000001"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.validate(close=value)

    def test_invalid_hash(self):
        """The hash must be exactly 64 lowercase hex characters."""
        with self.assertRaises(ValueError):
            self.validate(raw_sha256="A" * 64)

    def test_naive_timestamp(self):
        """Timestamps require explicit UTC offsets."""
        with self.assertRaises(ValueError):
            self.validate(retrieved_at="2026-01-03T00:00:00")
        with self.assertRaises(ValueError):
            validate_daily_bar(
                copy.deepcopy(VALID), expected_symbol="AAPL",
                as_of="2026-01-06T00:00:00", allowed_hosts=ALLOWED_HOSTS,
            )

    def test_offset_timestamp_normalizes_to_utc(self):
        """Explicit non-UTC offsets are retained as the same UTC instant."""
        result = self.validate(retrieved_at="2026-01-02T19:00:00-05:00")
        self.assertEqual(result["retrieved_at"], "2026-01-03T00:00:00+00:00")

    def test_future_timestamp(self):
        """retrieved_at may not be after as_of."""
        with self.assertRaises(ValueError):
            self.validate(retrieved_at="2026-01-07T00:00:00+00:00")

    def test_pre_close_retrieval(self):
        """Retrieval before the session close is rejected."""
        with self.assertRaises(ValueError):
            self.validate(retrieved_at="2026-01-02T14:00:00-05:00")

    def test_weekend_rejected(self):
        """Weekend session dates are rejected."""
        with self.assertRaises(ValueError):
            self.validate(session_date="2026-01-03")

    def test_market_holiday_rejected(self):
        """XNYS holidays are rejected."""
        with self.assertRaises(ValueError):
            self.validate(session_date="2026-01-01")

    def test_stale_session_rejected(self):
        """A session lag beyond max_session_lag is rejected."""
        with self.assertRaises(ValueError):
            validate_daily_bar(
                {
                    **copy.deepcopy(VALID),
                    "session_date": "2025-12-31",
                    "retrieved_at": "2026-01-06T00:00:00+00:00",
                },
                expected_symbol="AAPL",
                as_of=AS_OF,
                allowed_hosts=ALLOWED_HOSTS,
                max_session_lag=1,
            )

    def test_open_session_does_not_count_as_completed(self):
        """A same-day open market does not make the previous close stale."""
        result = validate_daily_bar(
            copy.deepcopy(VALID),
            expected_symbol="AAPL",
            as_of="2026-01-05T15:00:00+00:00",
            allowed_hosts=ALLOWED_HOSTS,
            max_session_lag=0,
        )
        self.assertEqual(result["session_date"], "2026-01-02")
        with self.assertRaises(ValueError):
            validate_daily_bar(
                {**VALID, "session_date": "2026-01-05",
                 "retrieved_at": "2026-01-05T15:00:00+00:00"},
                expected_symbol="AAPL", as_of="2026-01-05T15:00:00+00:00",
                allowed_hosts=ALLOWED_HOSTS,
            )

    def test_utc_date_does_not_replace_new_york_session_date(self):
        """UTC midnight while New York is still open uses New York's date."""
        candidate = copy.deepcopy(VALID)
        candidate["session_date"] = "2026-01-05"
        candidate["retrieved_at"] = "2026-01-05T22:00:00+00:00"
        result = validate_daily_bar(
            candidate,
            expected_symbol="AAPL",
            as_of="2026-01-06T00:00:00+00:00",
            allowed_hosts=ALLOWED_HOSTS,
            max_session_lag=0,
        )
        self.assertEqual(result["session_date"], "2026-01-05")

    def test_max_price_and_lag_bounds(self):
        """The maximum close and max_session_lag=5 are accepted."""
        candidate = copy.deepcopy(VALID)
        candidate["close"] = "1000000"
        candidate["session_date"] = "2025-12-31"
        candidate["retrieved_at"] = "2026-01-06T00:00:00+00:00"
        result = validate_daily_bar(
            candidate,
            expected_symbol="AAPL",
            as_of=AS_OF,
            allowed_hosts=ALLOWED_HOSTS,
            max_session_lag=5,
        )
        self.assertEqual(result["close"], "1000000")

    def test_no_input_mutation(self):
        """Validation does not mutate the caller's mapping."""
        candidate = copy.deepcopy(VALID)
        before = copy.deepcopy(candidate)
        self.validate(candidate)
        self.assertEqual(candidate, before)

    def test_max_lag_rejects_bool(self):
        """max_session_lag must not be a bool."""
        for lag in (True, -1, 6, 1.0):
            with self.subTest(lag=lag):
                with self.assertRaises(ValueError):
                    validate_daily_bar(
                        copy.deepcopy(VALID), expected_symbol="AAPL",
                        as_of=AS_OF, allowed_hosts=ALLOWED_HOSTS,
                        max_session_lag=lag,
                    )


if __name__ == "__main__":
    unittest.main()
