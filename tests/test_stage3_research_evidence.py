import json
import unittest
from datetime import datetime, timedelta, timezone

from scripts.stage3_paper.research_evidence import parse_research_evidence

SHA_A = "a" * 64
SHA_B = "b" * 64


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


BASE = datetime(2024, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def brief(**over):
    doc = {
        "schema": 1,
        "kind": "offline-research-evidence",
        "origin": "recorded",
        "captured_at": ts(BASE),
        "research_as_of": ts(BASE - timedelta(hours=1)),
        "expires_at": ts(BASE + timedelta(hours=6)),
        "symbol": "ACME",
        "summary": "Short local research note.",
        "sources": [
            {
                "uri": "https://example.com/a",
                "published_at": ts(BASE - timedelta(hours=2)),
                "sha256": SHA_A,
            }
        ],
        "proposal": {"side": "watch", "rationale": "Observe only."},
    }
    doc.update(over)
    return json.dumps(doc).encode("utf-8")


class HappyPathTests(unittest.TestCase):
    def test_basic_shape_and_flags_always_false(self):
        out = parse_research_evidence(brief(), as_of=ts(BASE))
        self.assertEqual(out["schema"], 1)
        self.assertEqual(out["kind"], "offline-research-evidence")
        self.assertEqual(out["origin"], "recorded")
        self.assertEqual(out["symbol"], "ACME")
        self.assertEqual(out["freshness"], "fresh")
        self.assertEqual(out["provenance"], "supplied_unverified")
        for flag in (
            "recommendation_verified",
            "executable",
            "fill_eligible",
        ):
            self.assertIs(out[flag], False)

    def test_fixture_provenance_and_null_proposal(self):
        out = parse_research_evidence(
            brief(origin="fixture", proposal=None), as_of=ts(BASE)
        )
        self.assertEqual(out["provenance"], "invented_fixture")
        self.assertIsNone(out["proposal"])
        self.assertIs(out["executable"], False)

    def test_preserves_source_and_date_strings(self):
        out = parse_research_evidence(brief(), as_of=ts(BASE))
        self.assertEqual(out["captured_at"], ts(BASE))
        self.assertEqual(out["sources"][0]["sha256"], SHA_A)
        self.assertEqual(out["sources"][0]["uri"], "https://example.com/a")


class FreshnessTests(unittest.TestCase):
    def test_boundary_stale_at_equality(self):
        for as_of, expect in (
            (ts(BASE + timedelta(hours=5, minutes=59)), "fresh"),
            (ts(BASE + timedelta(hours=6)), "stale"),
            (ts(BASE + timedelta(hours=6, seconds=1)), "stale"),
        ):
            with self.subTest(as_of=as_of):
                out = parse_research_evidence(brief(), as_of=as_of)
                self.assertEqual(out["freshness"], expect)
                self.assertIs(out["fill_eligible"], False)

    def test_timezone_equivalence(self):
        for as_of in (
            "2024-06-01T12:00:00Z",
            "2024-06-01T14:00:00+02:00",
            "2024-06-01T10:00:00-02:00",
            "2024-06-01T12:00:00.000Z",
        ):
            with self.subTest(as_of=as_of):
                out = parse_research_evidence(brief(), as_of=as_of)
                self.assertEqual(out["evaluated_at"], as_of)
                self.assertEqual(out["freshness"], "fresh")


class ValidationTests(unittest.TestCase):
    def assert_reject(self, payload, *, as_of=ts(BASE)):
        with self.assertRaises(ValueError):
            parse_research_evidence(payload, as_of=as_of)

    def test_raw_type_and_size(self):
        for payload in (
            "string",
            b"",
            b"x" * (1024 * 1024 + 1),
            b"\xff\xfe",
            b"[]",
            b"not json",
        ):
            with self.subTest(payload=payload[:16] if payload else payload):
                self.assert_reject(payload)

    def test_bool_where_int_expected(self):
        self.assert_reject(brief(schema=True))
        self.assert_reject(brief(schema=1.5))

    def test_duplicate_keys(self):
        dup = (
            b'{"schema":1,"schema":1,"kind":"offline-research-evidence",'
            b'"origin":"recorded","captured_at":"2024-06-01T12:00:00Z",'
            b'"research_as_of":"2024-06-01T11:00:00Z",'
            b'"expires_at":"2024-06-01T18:00:00Z","symbol":"ACME",'
            b'"summary":"s","sources":[],"proposal":null}'
        )
        self.assert_reject(dup)

    def test_nonfinite_and_unknown_keys(self):
        nan = json.dumps({"schema": float("nan")}).encode()
        self.assert_reject(nan)
        self.assert_reject(
            json.dumps(
                {
                    "schema": 1,
                    "kind": "offline-research-evidence",
                    "origin": "recorded",
                    "captured_at": ts(BASE),
                    "research_as_of": ts(BASE),
                    "expires_at": ts(BASE + timedelta(hours=1)),
                    "symbol": "ACME",
                    "summary": "s",
                    "sources": [
                        {
                            "uri": "https://e.com",
                            "published_at": ts(BASE),
                            "sha256": SHA_A,
                        }
                    ],
                    "proposal": None,
                    "extra": 1,
                }
            ).encode()
        )

    def test_symbol_and_summary(self):
        for sym in ("", "a", "TOOLONGX", "AA1"):
            with self.subTest(sym=sym):
                self.assert_reject(brief(symbol=sym))
        for summary in ("", "   ", "x" * 2001, "bad\x00ctl"):
            with self.subTest(s=summary[:8]):
                self.assert_reject(brief(summary=summary))

    def test_timestamps(self):
        bad = (
            "2024-06-01",
            "2024-06-01T12:00:00",
            "2024-06-01 12:00:00Z",
            " 2024-06-01T12:00:00Z",
            "2024-06-01T12:00:00+1:00",
        )
        for v in bad:
            with self.subTest(v=v):
                self.assert_reject(brief(captured_at=v))

    def test_time_ordering_and_expiry_window(self):
        self.assert_reject(brief(research_as_of=ts(BASE + timedelta(hours=1))))
        self.assert_reject(
            brief(
                captured_at=ts(BASE),
                expires_at=ts(BASE + timedelta(days=8)),
            )
        )
        self.assert_reject(brief(expires_at=ts(BASE - timedelta(hours=1))))

    def test_future_source_publication(self):
        s = {
            "uri": "https://e.com/a",
            "published_at": ts(BASE + timedelta(days=1)),
            "sha256": SHA_A,
        }
        self.assert_reject(brief(sources=[s]))

    def test_sources_families(self):
        cases = [
            [],
            [{}],
            [
                {
                    "uri": "http://e.com",
                    "published_at": ts(BASE),
                    "sha256": SHA_A,
                }
            ],
            [
                {
                    "uri": "https://u:p@e.com",
                    "published_at": ts(BASE),
                    "sha256": SHA_A,
                }
            ],
            [
                {
                    "uri": "file:///etc/passwd",
                    "published_at": ts(BASE),
                    "sha256": SHA_A,
                }
            ],
            [
                {
                    "uri": "javascript:alert(1)",
                    "published_at": ts(BASE),
                    "sha256": SHA_A,
                }
            ],
            [
                {
                    "uri": "https://e.com/a#frag",
                    "published_at": ts(BASE),
                    "sha256": SHA_A,
                }
            ],
            [
                {
                    "uri": "https://e.com/a",
                    "published_at": ts(BASE),
                    "sha256": SHA_A.upper(),
                }
            ],
        ]
        for s in cases:
            with self.subTest(s=s):
                self.assert_reject(brief(sources=s))

    def test_duplicate_source_uri(self):
        s = {
            "uri": "https://e.com/a",
            "published_at": ts(BASE),
            "sha256": SHA_A,
        }
        s2 = dict(s, sha256=SHA_B)
        self.assert_reject(brief(sources=[s, s2]))

    def test_proposal_keys_and_values(self):
        for prop in (
            {"side": "hold", "rationale": "x"},
            {"side": "buy"},
            {"side": "buy", "rationale": "", "quantity": 1},
            {"side": "buy", "rationale": "x" * 2001},
            {"side": True, "rationale": "x"},
        ):
            with self.subTest(prop=prop):
                self.assert_reject(brief(proposal=prop))
        out = parse_research_evidence(
            brief(proposal={"side": "buy", "rationale": "note"}),
            as_of=ts(BASE),
        )
        self.assertEqual(out["proposal"]["side"], "buy")
        self.assertIs(out["recommendation_verified"], False)

    def test_null_required_fields(self):
        for field in ("captured_at", "symbol", "summary"):
            with self.subTest(field=field):
                self.assert_reject(brief(**{field: None}))

    def test_raw_sha256_is_deterministic(self):
        a = parse_research_evidence(brief(), as_of=ts(BASE))
        b = parse_research_evidence(brief(), as_of=ts(BASE))
        self.assertEqual(a["raw_sha256"], b["raw_sha256"])
        c = parse_research_evidence(brief(origin="fixture"), as_of=ts(BASE))
        self.assertIs(c["recommendation_verified"], False)


if __name__ == "__main__":
    unittest.main()


class IntegrationBoundaryRegressions(unittest.TestCase):
    def test_schema_future_capture_and_offsets(self):
        for doc in (brief(schema=2), brief(captured_at=ts(BASE + timedelta(seconds=1))), brief(symbol="ACME\n")):
            with self.subTest(raw=doc), self.assertRaises(ValueError):
                parse_research_evidence(doc, as_of=ts(BASE))
        for offset in ('+00:60', '+24:00', '+01:99'):
            with self.subTest(offset=offset), self.assertRaises(ValueError):
                parse_research_evidence(brief(), as_of='2024-06-01T12:00:00' + offset)
        out = parse_research_evidence(brief(captured_at='2024-06-01T20:00:00+08:00'), as_of=ts(BASE))
        self.assertEqual(out['captured_at'], '2024-06-01T20:00:00+08:00')
        with self.assertRaises(ValueError):
            parse_research_evidence(brief(captured_at='2024-06-01T11:00:00-02:00'), as_of=ts(BASE))


class ResidualInputFamilyTests(unittest.TestCase):
    def test_utc_overflow_is_controlled(self):
        for stamp in ('0001-01-01T00:00:00+01:00', '9999-12-31T23:59:59-01:00'):
            with self.subTest(stamp=stamp), self.assertRaises(ValueError):
                parse_research_evidence(brief(), as_of=stamp)
    def test_invalid_https_authorities(self):
        for uri in ('https://[', 'https://example.invalid:badport', 'https://example.invalid:65536'):
            source = {'uri':uri, 'published_at':'2024-06-01T10:00:00Z', 'sha256':SHA_A}
            with self.subTest(uri=uri), self.assertRaises(ValueError):
                parse_research_evidence(brief(sources=[source]), as_of=ts(BASE))
