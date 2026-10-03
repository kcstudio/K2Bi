"""Compact tests for the offline stage 3 action card."""
import hashlib
import unittest
from decimal import Decimal

from scripts.stage3_paper.action_card import build_card


from scripts.stage3_paper.snapshot import EXPECTED_ACCOUNT
ACCOUNT = EXPECTED_ACCOUNT


def make_cash(value="100.00", freshness="fresh", account=ACCOUNT, ready=True):
    return {
        "account_id": account,
        "account_ready": ready,
        "freshness": freshness,
        "rows": [{"currency": "USD", "value": value}],
    }


def make_holdings(account=ACCOUNT, freshness="stale"):
    return {"account_id": account, "freshness": freshness, "rows": []}


def make_reference():
    return {
        "reference_eligible": True,
        "reference_status": "verified_historical_reference",
        "fill_eligible": False,
    }


def make_example(**overrides):
    data = {
        "origin": "fixture",
        "symbol": "G",
        "side": "buy",
        "quantity": 2,
        "reference_price": "33.29",
        "stop_price": "32.29",
        "reason": "Invented demonstration",
    }
    data.update(overrides)
    return data


def check(card, label):
    return [c for c in card["checks"] if c["label"] == label][0]


class ActionCardTest(unittest.TestCase):
    def test_actual_waits_without_withholding_quantity(self):
        card = build_card(make_reference(), make_cash(), make_holdings())
        self.assertEqual(card["status"], "WAIT")
        self.assertIsNone(card["candidate"])
        self.assertIsNone(card["quantity"])
        self.assertIsNone(card["estimated_cost"])
        self.assertIsNone(card["estimated_risk"])
        self.assertFalse(card["executable"])
        self.assertFalse(card["fill_eligible"])

    def test_negative_usd_is_debit_not_pnl(self):
        card = build_card(None, make_cash(value="-2168.56"), make_holdings())
        usd = check(card, "usd_cash")
        self.assertEqual(usd["status"], "negative")
        self.assertIn("debit balance", usd["detail"])
        self.assertIn("not profit or loss", usd["detail"])
        self.assertIn("HKD", usd["detail"])

    def test_signed_zero_is_not_a_debit(self):
        for value in ("-0.00", "0.00"):
            card = build_card(None, make_cash(value=value), make_holdings())
            self.assertEqual(check(card, "usd_cash")["status"], "ok")
            self.assertNotIn("debit balance", card["reason"])

    def test_stale_cash_not_funding_proof(self):
        card = build_card(None, make_cash(freshness="stale"), make_holdings())
        self.assertIn(check(card, "usd_cash")["status"], ("stale", "negative"))
        self.assertIn("capture", check(card, "usd_cash")["detail"])

    def test_base_not_usd_unknown(self):
        cash = make_cash()
        cash["rows"] = [{"currency": "HKD", "value": "10"}]
        card = build_card(None, cash, make_holdings())
        self.assertEqual(check(card, "usd_cash")["status"], "unknown")

    def test_account_ready_display_only(self):
        card = build_card(None, make_cash(ready=None, freshness="stale"), make_holdings())
        self.assertEqual(check(card, "account_ready")["status"], "unknown")

    def test_mismatched_account_rejected(self):
        with self.assertRaises(ValueError):
            build_card(None, make_cash(), make_holdings(account="DUQ999999999"))

    def test_risk_not_run_and_config_hash(self):
        raw = b"limits:\n  max: 1\n"
        card = build_card(None, None, None, config_raw=raw)
        self.assertEqual(check(card, "risk")["status"], "not_run")
        self.assertEqual(card["risk_config"]["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(card["risk_config"]["limits"], {"limits": {"max": 1}})

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            build_card(None, None, None, config_raw=b"- 1\n")
        with self.assertRaises(ValueError):
            build_card(None, None, None, config_raw=b"x" * (1024 * 1024 + 1))

    def test_example_card_filled(self):
        card = build_card(None, None, None, example=make_example())
        self.assertEqual(card["status"], "INVENTED OFFLINE EXAMPLE")
        self.assertEqual(card["candidate"], {"symbol": "G", "side": "buy", "reason": "Invented demonstration"})
        self.assertEqual(card["quantity"], 2)
        self.assertEqual(card["estimated_cost"], str(Decimal("33.29") * 2))
        self.assertEqual(card["estimated_cost"], "66.58")
        self.assertEqual(card["estimated_risk"], str(abs(Decimal("33.29") - Decimal("32.29")) * 2))
        self.assertEqual(card["estimated_risk"], "2.00")
        self.assertFalse(card["executable"])
        self.assertFalse(card["fill_eligible"])
        self.assertEqual(check(card, "risk")["status"], "not_run")

    def test_example_rejections(self):
        for override in (
            {"origin": "real"},
            {"quantity": True},
            {"reference_price": "-1"},
            {"stop_price": "0"},
            {"stop_price": "nan"},
            {"symbol": "aapl"},
        ):
            with self.assertRaises(ValueError):
                build_card(None, None, None, example=make_example(**override))

    def test_inputs_unchanged(self):
        cash = make_cash()
        holdings = make_holdings()
        reference = make_reference()
        raw = b"a: 1\n"
        cash_before = dict(cash)
        holdings_before = dict(holdings)
        reference_before = dict(reference)
        build_card(reference, cash, holdings, config_raw=raw)
        self.assertEqual(cash, cash_before)
        self.assertEqual(holdings, holdings_before)
        self.assertEqual(reference, reference_before)
        self.assertEqual(raw, b"a: 1\n")


if __name__ == "__main__":
    unittest.main()


class SuppliedResearchCardTests(unittest.TestCase):
    def test_all_research_states_withhold_real_action(self):
        from scripts.stage3_paper.research_evidence import parse_research_evidence
        from tests.test_stage3_research_evidence import brief, ts, BASE
        for origin in ('recorded', 'fixture'):
            for as_of in ('2024-06-01T12:00:00Z', '2024-06-02T12:00:00Z'):
                research = parse_research_evidence(brief(origin=origin), as_of=as_of)
                card = build_card(make_reference(), make_cash('100.00'), make_holdings(), research=research)
                self.assertEqual(card['status'], 'WAIT')
                for field in ('candidate', 'quantity', 'estimated_cost', 'estimated_risk'):
                    self.assertIsNone(card[field])
                self.assertFalse(card['executable'])
                check = next(c for c in card['checks'] if c['label'] == 'current_recommendation')
                self.assertEqual(check['status'], 'unknown')
                self.assertIn('approved paper-trading rules', check['detail'])
                self.assertIn(research['freshness'], check['detail'])

class AcquisitionWaitTests(unittest.TestCase):
    def test_matching_source_and_feed_observations_do_not_approve(self):
        card = build_card(None, make_cash(), make_holdings(), source_proof={'source_integrity':'matched_saved_bytes'}, quote={'quote_eligibility':False})
        self.assertEqual(card['status'], 'WAIT')
        checks = {row['label']:row for row in card['checks']}
        self.assertEqual(checks['current_price']['status'], 'unknown')
        self.assertEqual(checks['underlying_source_bytes']['status'], 'matched_saved_bytes')
