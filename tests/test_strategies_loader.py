"""Tests for execution.strategies.loader."""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from execution.strategies import loader
from execution.strategies.types import (
    ALLOWED_STATUSES,
    STATUS_APPROVED,
    STATUS_PROPOSED,
    STATUS_REJECTED,
    STATUS_RETIRED,
    STRATEGY_TYPE_HAND_CRAFTED,
    StrategyFileModifiedError,
    StrategyLoaderError,
)


def _write_strategy(
    dir: Path,
    name: str,
    *,
    status: str = STATUS_APPROVED,
    strategy_type: str = STRATEGY_TYPE_HAND_CRAFTED,
    risk_envelope_pct: str = "0.01",
    order: dict | None = None,
    regime_filter: list[str] | None = None,
    how_this_works: str = "Plain-English explanation.",
    approved_at: str | None = "2026-05-01T10:00:00Z",
    approved_commit_sha: str | None = "abc1234",
    extra_frontmatter: dict | None = None,
) -> Path:
    order = order if order is not None else {
        "ticker": "SPY",
        "side": "buy",
        "qty": 10,
        "limit_price": "500.00",
        "stop_loss": "495.00",
        "time_in_force": "DAY",
    }
    lines = ["---", f"name: {name}", f"status: {status}", f"strategy_type: {strategy_type}"]
    lines.append(f"risk_envelope_pct: {risk_envelope_pct}")
    if approved_at is not None:
        lines.append(f"approved_at: {approved_at}")
    if approved_commit_sha is not None:
        lines.append(f"approved_commit_sha: {approved_commit_sha}")
    if regime_filter is not None:
        lines.append("regime_filter:")
        for r in regime_filter:
            lines.append(f"  - {r}")
    if order is not None:
        lines.append("order:")
        for k, v in order.items():
            lines.append(f"  {k}: {v}")
    if extra_frontmatter:
        for k, v in extra_frontmatter.items():
            lines.append(f"{k}: {v}")
    lines.append("---")
    lines.append("")
    lines.append("## How This Works")
    lines.append("")
    lines.append(how_this_works)
    path = dir / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class LoadDocumentTests(unittest.TestCase):
    def test_full_parse_of_approved_hand_crafted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "spy-rotational", regime_filter=["risk_on"])
            doc = loader.load_document(path)
            self.assertEqual(doc.name, "spy-rotational")
            self.assertEqual(doc.status, STATUS_APPROVED)
            self.assertEqual(doc.strategy_type, STRATEGY_TYPE_HAND_CRAFTED)
            self.assertEqual(doc.risk_envelope_pct, Decimal("0.01"))
            self.assertIsNotNone(doc.order_spec)
            self.assertEqual(doc.order_spec.ticker, "SPY")
            self.assertEqual(doc.order_spec.side, "buy")
            self.assertEqual(doc.order_spec.qty, 10)
            self.assertEqual(doc.order_spec.limit_price, Decimal("500.00"))
            self.assertEqual(doc.order_spec.stop_loss, Decimal("495.00"))
            self.assertEqual(doc.regime_filter, ("risk_on",))
            self.assertIn("Plain-English", doc.how_this_works)
            self.assertNotEqual(doc.source_sha256, "")
            self.assertIsNotNone(doc.approved_at)
            self.assertEqual(doc.approved_commit_sha, "abc1234")

    def test_proposed_parses_without_approval_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(
                Path(tmp),
                "draft",
                status=STATUS_PROPOSED,
                approved_at=None,
                approved_commit_sha=None,
            )
            doc = loader.load_document(path)
            self.assertEqual(doc.status, STATUS_PROPOSED)
            self.assertIsNone(doc.approved_at)
            self.assertIsNone(doc.approved_commit_sha)

    def test_missing_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.md"
            path.write_text("# no frontmatter\n", encoding="utf-8")
            with self.assertRaises(StrategyLoaderError):
                loader.load_document(path)

    def test_unterminated_frontmatter_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.md"
            path.write_text("---\nname: x\n", encoding="utf-8")
            with self.assertRaises(StrategyLoaderError):
                loader.load_document(path)

    def test_invalid_status_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "x", status="pending")
            with self.assertRaises(StrategyLoaderError) as cm:
                loader.load_document(path)
            self.assertIn("status", str(cm.exception))

    def test_rejected_status_parses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(
                Path(tmp),
                "bad-draft",
                status=STATUS_REJECTED,
                approved_at=None,
                approved_commit_sha=None,
            )
            doc = loader.load_document(path)
            self.assertEqual(doc.status, STATUS_REJECTED)

    def test_rejected_status_in_allowed_set(self):
        self.assertIn(STATUS_REJECTED, ALLOWED_STATUSES)
        self.assertIn(STATUS_RETIRED, ALLOWED_STATUSES)
        self.assertEqual(
            ALLOWED_STATUSES,
            frozenset(
                {STATUS_PROPOSED, STATUS_APPROVED, STATUS_REJECTED, STATUS_RETIRED}
            ),
        )

    def test_missing_order_on_hand_crafted_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "x", order={})
            # order={} becomes empty mapping with no ticker etc
            with self.assertRaises(StrategyLoaderError):
                loader.load_document(path)

    def test_bad_side_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(
                Path(tmp),
                "x",
                order={
                    "ticker": "SPY",
                    "side": "long",  # invalid -- must be buy|sell
                    "qty": 1,
                    "limit_price": "100",
                },
            )
            with self.assertRaises(StrategyLoaderError):
                loader.load_document(path)

    def test_boolean_qty_raises(self):
        # Codex R15 P2: bool is a subclass of int in Python; without
        # an explicit bool check, YAML `qty: true` would be silently
        # accepted as 1-share and place a live trade.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(
                Path(tmp),
                "x",
                order={
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": True,
                    "limit_price": "100",
                },
            )
            with self.assertRaises(StrategyLoaderError) as cm:
                loader.load_document(path)
            self.assertIn("qty", str(cm.exception))

    def test_non_positive_qty_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(
                Path(tmp),
                "x",
                order={
                    "ticker": "SPY",
                    "side": "buy",
                    "qty": 0,
                    "limit_price": "100",
                },
            )
            with self.assertRaises(StrategyLoaderError):
                loader.load_document(path)


def _write_strategy_with_raw_order(
    dir: Path,
    name: str,
    *,
    order_yaml: str,
    status: str = STATUS_APPROVED,
    approved_at: str | None = "2026-05-01T10:00:00Z",
    approved_commit_sha: str | None = "abc1234",
) -> Path:
    """Write a strategy file with the order block expressed as raw YAML.

    Lets matrix tests emit YAML literals like `limit_price: null` that
    `_write_strategy`'s f-string-based dict serialiser cannot produce
    (Python None renders as the string "None" inside an f-string).
    """
    lines = [
        "---",
        f"name: {name}",
        f"status: {status}",
        f"strategy_type: {STRATEGY_TYPE_HAND_CRAFTED}",
        "risk_envelope_pct: 0.01",
    ]
    if approved_at is not None:
        lines.append(f"approved_at: {approved_at}")
    if approved_commit_sha is not None:
        lines.append(f"approved_commit_sha: {approved_commit_sha}")
    lines.append("order:")
    for line in order_yaml.strip("\n").splitlines():
        lines.append(f"  {line}")
    lines.append("---")
    lines.append("")
    lines.append("## How This Works")
    lines.append("")
    lines.append("Plain-English explanation.")
    path = dir / f"{name}.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class OrderTypeLimitPriceMatrixTests(unittest.TestCase):
    """Round 5 (2026-05-08): order_type x limit_price matrix.

    Pre-fix loader required `limit_price` as a Decimal regardless of
    order_type, so a `MKT` order with `limit_price: null` (the only
    semantically valid shape for a market order) failed to load.
    Matrix coverage:

      | order_type | limit_price | expected               |
      |------------|-------------|------------------------|
      | MKT        | null        | pass, limit_price=None |
      | MKT        | non-null    | pass (reference hint)  |
      | LMT        | null        | reject                 |
      | LMT        | non-null    | pass (pre-fix path)    |
      | absent     | non-null    | pass, defaults to LMT  |
      | absent     | null        | reject (LMT default)   |
      | UNKNOWN    | any         | reject                 |
    """

    def test_mkt_with_null_limit_price_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "g-paper",
                order_yaml=(
                    "ticker: G\n"
                    "side: buy\n"
                    "qty: 71\n"
                    "order_type: MKT\n"
                    "limit_price: null\n"
                    "time_in_force: DAY\n"
                    "stop_loss: 30.00"
                ),
            )
            doc = loader.load_document(path)
            self.assertIsNotNone(doc.order_spec)
            self.assertEqual(doc.order_spec.order_type, "MKT")
            self.assertIsNone(doc.order_spec.limit_price)
            self.assertEqual(doc.order_spec.qty, 71)
            self.assertEqual(doc.order_spec.stop_loss, Decimal("30.00"))

    def test_mkt_with_non_null_limit_price_passes_as_reference_hint(self):
        # MKT + non-null limit_price is accepted: the value is stored as
        # a reference-price hint that downstream consumers may ignore
        # (a market order has no authoritative limit). The "pass" branch
        # of Keith's "pass-or-warn-pick-one" spec.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "mkt-with-hint",
                order_yaml=(
                    "ticker: G\n"
                    "side: buy\n"
                    "qty: 71\n"
                    "order_type: MKT\n"
                    "limit_price: 34.50\n"
                ),
            )
            doc = loader.load_document(path)
            self.assertEqual(doc.order_spec.order_type, "MKT")
            self.assertEqual(doc.order_spec.limit_price, Decimal("34.50"))

    def test_lmt_with_null_limit_price_rejects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "lmt-without-price",
                order_yaml=(
                    "ticker: SPY\n"
                    "side: buy\n"
                    "qty: 10\n"
                    "order_type: LMT\n"
                    "limit_price: null\n"
                ),
            )
            with self.assertRaises(StrategyLoaderError) as ctx:
                loader.load_document(path)
            self.assertIn("limit_price", str(ctx.exception))

    def test_lmt_with_non_null_limit_price_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "lmt-classic",
                order_yaml=(
                    "ticker: SPY\n"
                    "side: buy\n"
                    "qty: 10\n"
                    "order_type: LMT\n"
                    "limit_price: 500.00\n"
                ),
            )
            doc = loader.load_document(path)
            self.assertEqual(doc.order_spec.order_type, "LMT")
            self.assertEqual(doc.order_spec.limit_price, Decimal("500.00"))

    def test_absent_order_type_defaults_to_lmt_with_decimal_limit_price(self):
        # Backward-compat: pre-2026-05-08 strategies omit `order_type`
        # entirely and the loader required `limit_price` as Decimal.
        # Default must remain LMT semantics so existing approved
        # strategies (e.g. spy-first-paper-smoke) keep loading.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "legacy-no-order-type",
                order_yaml=(
                    "ticker: SPY\n"
                    "side: buy\n"
                    "qty: 2\n"
                    "limit_price: 715.00\n"
                ),
            )
            doc = loader.load_document(path)
            self.assertEqual(doc.order_spec.order_type, "LMT")
            self.assertEqual(doc.order_spec.limit_price, Decimal("715.00"))

    def test_absent_order_type_with_null_limit_price_rejects(self):
        # Conservative default: when order_type is absent, behave like
        # LMT (require Decimal). A null limit_price under the default
        # must fail rather than silently coerce to MKT.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "legacy-but-null",
                order_yaml=(
                    "ticker: SPY\n"
                    "side: buy\n"
                    "qty: 2\n"
                    "limit_price: null\n"
                ),
            )
            with self.assertRaises(StrategyLoaderError):
                loader.load_document(path)

    def test_unknown_order_type_rejects_with_clear_error(self):
        # Per Keith's spec: every order_type must be explicitly handled
        # or rejected; no silent-accept paths.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "unknown-order-type",
                order_yaml=(
                    "ticker: SPY\n"
                    "side: buy\n"
                    "qty: 10\n"
                    "order_type: STP\n"
                    "limit_price: 500.00\n"
                ),
            )
            with self.assertRaises(StrategyLoaderError) as ctx:
                loader.load_document(path)
            msg = str(ctx.exception)
            self.assertIn("order_type", msg)
            self.assertIn("STP", msg)

    def test_order_type_case_insensitive(self):
        # Tolerate lowercase/mixed-case order_type strings; the loader
        # uppercases before validation.
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy_with_raw_order(
                Path(tmp),
                "lowercase-mkt",
                order_yaml=(
                    "ticker: G\n"
                    "side: buy\n"
                    "qty: 71\n"
                    "order_type: mkt\n"
                    "limit_price: null\n"
                ),
            )
            doc = loader.load_document(path)
            self.assertEqual(doc.order_spec.order_type, "MKT")
            self.assertIsNone(doc.order_spec.limit_price)


class LoadApprovedTests(unittest.TestCase):
    def test_approved_snapshot_has_mtime_and_sha(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "spy-rotational")
            snap = loader.load_approved(path)
            self.assertEqual(snap.name, "spy-rotational")
            self.assertGreater(snap.source_mtime, 0)
            self.assertEqual(len(snap.source_sha256), 64)  # sha256 hex length

    def test_load_approved_rejects_proposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(
                Path(tmp),
                "draft",
                status=STATUS_PROPOSED,
                approved_at=None,
                approved_commit_sha=None,
            )
            with self.assertRaises(StrategyLoaderError) as cm:
                loader.load_approved(path)
            self.assertIn("status", str(cm.exception))

    def test_load_approved_missing_approved_at_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "x", approved_at=None)
            with self.assertRaises(StrategyLoaderError) as cm:
                loader.load_approved(path)
            self.assertIn("approved_at", str(cm.exception))


class DriftDetectionTests(unittest.TestCase):
    def test_unchanged_file_reports_no_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "spy-rotational")
            snap = loader.load_approved(path)
            self.assertFalse(loader.detect_drift(snap))

    def test_content_change_reports_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "spy-rotational")
            snap = loader.load_approved(path)
            # Append a line; mtime + sha256 both change.
            time.sleep(0.01)
            path.write_text(path.read_text() + "\n# extra", encoding="utf-8")
            self.assertTrue(loader.detect_drift(snap))

    def test_missing_file_reports_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "spy-rotational")
            snap = loader.load_approved(path)
            path.unlink()
            self.assertTrue(loader.detect_drift(snap))

    def test_assert_file_unchanged_raises_on_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _write_strategy(Path(tmp), "spy-rotational")
            snap = loader.load_approved(path)
            time.sleep(0.01)
            path.write_text(path.read_text() + "\n# extra", encoding="utf-8")
            with self.assertRaises(StrategyFileModifiedError) as cm:
                loader.assert_file_unchanged(snap)
            self.assertEqual(cm.exception.approved_sha256, snap.source_sha256)
            self.assertNotEqual(cm.exception.current_sha256, snap.source_sha256)


class LoadAllApprovedTests(unittest.TestCase):
    def test_returns_only_approved(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_strategy(Path(tmp), "approved-one")
            _write_strategy(
                Path(tmp),
                "draft-one",
                status=STATUS_PROPOSED,
                approved_at=None,
                approved_commit_sha=None,
            )
            snaps = loader.load_all_approved(Path(tmp))
            self.assertEqual(len(snaps), 1)
            self.assertEqual(snaps[0].name, "approved-one")

    def test_ignores_index_md(self):
        with tempfile.TemporaryDirectory() as tmp:
            _write_strategy(Path(tmp), "approved-one")
            (Path(tmp) / "index.md").write_text(
                "---\ntype: index\n---\n# index\n", encoding="utf-8"
            )
            snaps = loader.load_all_approved(Path(tmp))
            self.assertEqual(len(snaps), 1)

    def test_missing_directory_returns_empty(self):
        snaps = loader.load_all_approved(Path("/nonexistent/path/for/test"))
        self.assertEqual(snaps, [])

    def test_malformed_draft_does_not_abort_startup(self):
        # Codex round-11 P1: a malformed proposed/retired draft must
        # not abort engine startup -- runtime only consumes approved
        # strategies.
        with tempfile.TemporaryDirectory() as tmp:
            _write_strategy(Path(tmp), "approved-one")
            (Path(tmp) / "broken-draft.md").write_text(
                "---\nname: broken\nstatus: proposed\nrisk_envelope_pct: not-a-number\n---\nbody\n",
                encoding="utf-8",
            )
            snaps = loader.load_all_approved(Path(tmp))
            self.assertEqual(len(snaps), 1)
            self.assertEqual(snaps[0].name, "approved-one")

    def test_malformed_yaml_syntax_in_draft_does_not_abort_startup(self):
        # Codex R17 P1: yaml.YAMLError must be caught as
        # StrategyLoaderError so the load_all_approved handler can
        # quietly skip malformed drafts instead of aborting startup.
        with tempfile.TemporaryDirectory() as tmp:
            _write_strategy(Path(tmp), "valid-approved")
            # Write frontmatter with malformed YAML (unclosed bracket).
            (Path(tmp) / "syntax-broken.md").write_text(
                "---\nname: bad\nstatus: proposed\norder: [unclosed\n---\nbody\n",
                encoding="utf-8",
            )
            # Must not raise -- approved file loads, broken draft skipped.
            snaps = loader.load_all_approved(Path(tmp))
            self.assertEqual(len(snaps), 1)

    def test_malformed_approved_intent_raises_via_peek(self):
        # Codex round-12 P2: a file whose FULL parse fails but whose
        # raw status line says `approved` must abort startup even
        # though load_document() couldn't build the dataclass.
        with tempfile.TemporaryDirectory() as tmp:
            _write_strategy(Path(tmp), "valid-approved")
            (Path(tmp) / "broken-approved.md").write_text(
                "---\nname: broken\nstatus: approved\nrisk_envelope_pct: not-a-number\n---\nbody\n",
                encoding="utf-8",
            )
            with self.assertRaises(StrategyLoaderError):
                loader.load_all_approved(Path(tmp))

    def test_malformed_approved_still_raises(self):
        # An approved strategy that fails stricter contract MUST still
        # abort startup -- that's the runtime path.
        with tempfile.TemporaryDirectory() as tmp:
            # Approved but missing approved_at.
            _write_strategy(Path(tmp), "broken-approved", approved_at=None)
            with self.assertRaises(StrategyLoaderError) as cm:
                loader.load_all_approved(Path(tmp))
            self.assertIn("approved_at", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
