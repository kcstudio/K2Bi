"""Tests for `python -m execution.engine.main --diagnose-approved` -- cycle 5.

The CLI subcommand reads the newest `engine_started` event from today's
(falling back to yesterday's) decision journal and prints the approved
strategies the engine booted with (name, approved_commit_sha,
regime_filter, risk_envelope_pct).

Coverage:

    * _resolve_journal_dir_for_diagnose precedence:
        - --journal-dir flag wins over config.yaml
        - config.yaml engine.journal_dir read when no flag
        - falls through to None when neither present
    * _find_newest_engine_started:
        - empty journal dir -> None
        - single engine_started record today -> returned
        - today empty + yesterday has record -> yesterday's returned
        - multiple engine_started events -> newest by ts wins
        - non-engine_started events ignored
    * _format_diagnose_table:
        - rich cycle-5 payload (with `strategies: [...]`) renders full table
        - legacy payload (only `strategies_loaded: [names]`) falls back to
          name-only list with a pre-cycle-5 hint
        - missing payload.strategies + strategies_loaded -> "(none)"
    * End-to-end CLI via subprocess:
        - Seed a journal with a known engine_started event, run the CLI
          with PYTHONPATH + --journal-dir pointing at the seeded dir,
          assert stdout contains the expected strategy rows.
        - Empty journal dir -> "engine not started in last 24h" hint.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from execution.connectors.mock import MockIBKRConnector
from execution.engine import main as engine_main
from execution.engine.main import (
    DEFAULT_TICK_SECONDS,
    Engine,
    EngineConfig,
    EngineState,
)
from execution.journal.writer import JournalWriter


REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_writer(base_dir: Path) -> JournalWriter:
    # Force git_sha=None so tests don't depend on the repo's HEAD state.
    return JournalWriter(base_dir=base_dir, git_sha=None)


def _append_engine_started(
    writer: JournalWriter,
    *,
    when: datetime,
    strategies: list[dict] | None = None,
    strategies_loaded: list[str] | None = None,
    include_legacy: bool = True,
    include_rich: bool = True,
) -> None:
    payload: dict = {
        "pid": 12345,
        "tick_seconds": 30.0,
        "recovery_status": "clean",
        "reconciled_event_count": 0,
        "mismatch_count": 0,
        "resumed_awaiting": None,
        "validator_config_hash": "deadbeef01234567",
        "kill_file_present_at_startup": False,
        "retired_dir": "/tmp/test-retired-dir",
    }
    if include_legacy:
        payload["strategies_loaded"] = strategies_loaded or [
            s["name"] for s in (strategies or [])
        ]
    if include_rich and strategies is not None:
        payload["strategies"] = strategies
    writer.append("engine_started", payload=payload, ts=when)


# ---------- resolver ----------


class ResolveJournalDirTests(unittest.TestCase):
    def test_cli_flag_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cli_override = tmp / "cli_override"
            resolved = engine_main._resolve_journal_dir_for_diagnose(
                cli_override, None
            )
            self.assertEqual(resolved, cli_override)

    def test_config_yaml_read_when_no_cli_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            config_jd = tmp / "cfg_journal_dir"
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(
                f"engine:\n  journal_dir: {config_jd}\n", encoding="utf-8"
            )
            resolved = engine_main._resolve_journal_dir_for_diagnose(
                None, cfg_path
            )
            self.assertEqual(resolved, config_jd)

    def test_returns_none_when_nothing_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg_path = tmp / "no_such_config.yaml"
            resolved = engine_main._resolve_journal_dir_for_diagnose(
                None, cfg_path
            )
            self.assertIsNone(resolved)

    def test_config_yaml_without_journal_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            cfg_path = tmp / "config.yaml"
            cfg_path.write_text(
                "engine:\n  tick_seconds: 30\n", encoding="utf-8"
            )
            resolved = engine_main._resolve_journal_dir_for_diagnose(
                None, cfg_path
            )
            self.assertIsNone(resolved)


# ---------- find_newest ----------


class FindNewestEngineStartedTests(unittest.TestCase):
    def test_empty_dir_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(
                engine_main._find_newest_engine_started(Path(tmp))
            )

    def test_read_only_does_not_create_missing_dir(self):
        # Codex R7 P1 #2: the diagnose path is strictly read-only; it
        # must NOT create a journal directory that did not exist.
        # JournalWriter.__init__ would otherwise mkdir as a side
        # effect. Use a scratch parent + a non-existent child.
        with tempfile.TemporaryDirectory() as tmp:
            nonexistent = Path(tmp) / "never_created"
            result = engine_main._find_newest_engine_started(nonexistent)
            self.assertIsNone(result)
            self.assertFalse(
                nonexistent.exists(),
                "diagnose must not create the journal dir as a side effect",
            )

    def test_read_only_does_not_run_recovery_truncation(self):
        # Codex R7 P1 #2: the diagnose path must NOT invoke
        # JournalWriter.recover_trailing_partial(), which truncates a
        # corrupt trailing line and appends a recovery_truncated
        # event. Write a jsonl file with a corrupt trailing line;
        # after diagnose, the file content must be byte-identical and
        # no recovery_truncated record appears.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            day_file = tmp / f"{today}.jsonl"
            # Write a valid engine_started line + a deliberately-
            # corrupt trailing partial line (no newline, not valid json).
            day_file.write_text(
                '{"ts": "2026-04-19T10:00:00+00:00", '
                '"schema_version": 2, '
                '"event_type": "engine_started", '
                '"trade_id": null, "journal_entry_id": "01ABCD", '
                '"strategy": null, "git_sha": "test01", '
                '"payload": {"strategies": []}}\n'
                "{partial line no newline",
                encoding="utf-8",
            )
            before_bytes = day_file.read_bytes()
            result = engine_main._find_newest_engine_started(tmp)
            after_bytes = day_file.read_bytes()
            self.assertEqual(
                before_bytes,
                after_bytes,
                "diagnose must NOT rewrite the jsonl file",
            )
            # The engine_started event was still found (corrupt trailing
            # line is silently skipped by the read-only iterator).
            self.assertIsNotNone(result)
            self.assertEqual(result["event_type"], "engine_started")

    def test_nan_bearing_journal_line_is_skipped(self):
        # Codex m2.23 round-3 surface audit: the diagnose read path
        # (_iter_journal_read_only) must enforce the same RFC-8259
        # strict-JSON contract as writer + recovery. A legacy or
        # tampered line containing bare NaN / Infinity tokens must be
        # silently skipped (not yielded with Python float('nan')
        # values), so the diagnose output never surfaces non-RFC-8259
        # journal records. Pair with a clean engine_started record on
        # the same day so we can assert the clean one is still
        # returned.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            day_file = tmp / f"{today}.jsonl"
            day_file.write_text(
                '{"ts": "2026-04-20T09:00:00+00:00", '
                '"schema_version": 2, '
                '"event_type": "engine_started", '
                '"trade_id": null, "journal_entry_id": "01CLEAN", '
                '"strategy": null, "git_sha": "test01", '
                '"payload": {"strategies": []}}\n'
                # Legacy/tampered NaN-bearing line for order_filled -
                # earlier ts so it would lose the tie-break anyway.
                '{"ts": "2026-04-20T08:00:00+00:00", '
                '"schema_version": 2, '
                '"event_type": "order_filled", '
                '"trade_id": null, "journal_entry_id": "01BAD", '
                '"strategy": null, "git_sha": "test01", '
                '"payload": {}, "slippage_bps": NaN}\n',
                encoding="utf-8",
            )
            # Iterator must not raise + the clean record must surface.
            records = list(engine_main._iter_journal_read_only(day_file))
            # The NaN-bearing order_filled line is skipped entirely;
            # only the clean engine_started record survives.
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["event_type"], "engine_started")
            self.assertEqual(records[0]["journal_entry_id"], "01CLEAN")
            # And the higher-level finder still works correctly.
            found = engine_main._find_newest_engine_started(tmp)
            self.assertIsNotNone(found)
            self.assertEqual(found["journal_entry_id"], "01CLEAN")

    def test_single_record_today(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            writer = _make_writer(tmp)
            now = datetime.now(timezone.utc)
            _append_engine_started(
                writer,
                when=now,
                strategies=[
                    {
                        "name": "spy-rot",
                        "approved_commit_sha": "abc1234",
                        "regime_filter": ["risk_on"],
                        "risk_envelope_pct": "0.01",
                    }
                ],
            )
            record = engine_main._find_newest_engine_started(tmp)
            self.assertIsNotNone(record)
            self.assertEqual(record["event_type"], "engine_started")
            self.assertEqual(
                record["payload"]["strategies"][0]["name"], "spy-rot"
            )

    def test_yesterday_fallback_when_today_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            writer = _make_writer(tmp)
            yesterday = datetime.now(timezone.utc) - timedelta(days=1)
            _append_engine_started(
                writer,
                when=yesterday,
                strategies=[
                    {
                        "name": "yesterday-strat",
                        "approved_commit_sha": "yday0001",
                        "regime_filter": [],
                        "risk_envelope_pct": "0.02",
                    }
                ],
            )
            record = engine_main._find_newest_engine_started(tmp)
            self.assertIsNotNone(record)
            self.assertEqual(
                record["payload"]["strategies"][0]["name"],
                "yesterday-strat",
            )

    def test_newest_by_ts_when_multiple_events_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            writer = _make_writer(tmp)
            now = datetime.now(timezone.utc)
            older = now - timedelta(minutes=30)
            _append_engine_started(
                writer,
                when=older,
                strategies=[
                    {
                        "name": "older",
                        "approved_commit_sha": "old1234",
                        "regime_filter": [],
                        "risk_envelope_pct": "0.01",
                    }
                ],
            )
            _append_engine_started(
                writer,
                when=now,
                strategies=[
                    {
                        "name": "newer",
                        "approved_commit_sha": "new5678",
                        "regime_filter": ["risk_on"],
                        "risk_envelope_pct": "0.03",
                    }
                ],
            )
            record = engine_main._find_newest_engine_started(tmp)
            self.assertEqual(
                record["payload"]["strategies"][0]["name"], "newer"
            )

    def test_non_dict_json_line_is_skipped_not_crashed(self):
        # Codex R7 round 2 [medium]: a journal line that parses as
        # valid JSON but is NOT a dict (scalar, list, null) must be
        # skipped, not crash `_find_newest_engine_started` with
        # AttributeError. `--diagnose-approved`'s stated contract is
        # "exits 0 always (diagnostic; non-blocking)"; a malformed
        # journal entry must NOT subvert that.
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            from datetime import datetime, timezone

            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            day_file = tmp / f"{today}.jsonl"
            # Three lines: a valid-JSON scalar (would crash .get()),
            # a valid-JSON list (would also crash), and a real
            # engine_started record. The scalar + list must be
            # skipped cleanly; the dict must be returned.
            day_file.write_text(
                '"just a string -- valid JSON, not a dict"\n'
                '["also valid JSON", "also not a dict"]\n'
                '{"ts": "2026-04-19T10:00:00.000000+00:00", '
                '"schema_version": 2, '
                '"event_type": "engine_started", '
                '"trade_id": null, "journal_entry_id": "01ABCD", '
                '"strategy": null, "git_sha": "test01", '
                '"payload": {"strategies": [{"name": "real-one", '
                '"approved_commit_sha": "abc1234", '
                '"regime_filter": ["risk_on"], '
                '"risk_envelope_pct": "0.01"}]}}\n',
                encoding="utf-8",
            )
            result = engine_main._find_newest_engine_started(tmp)
            self.assertIsNotNone(
                result, "valid engine_started dict must survive the scan"
            )
            self.assertEqual(result["event_type"], "engine_started")
            self.assertEqual(
                result["payload"]["strategies"][0]["name"], "real-one"
            )

    def test_non_engine_started_events_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            writer = _make_writer(tmp)
            now = datetime.now(timezone.utc)
            # Write a tick_clock-adjacent event first; diagnose MUST
            # filter for engine_started only. We pick order_submitted
            # because it's in EVENT_TYPES.
            writer.append(
                "order_submitted",
                payload={"status": "Submitted"},
                ts=now - timedelta(minutes=5),
                trade_id="trade-ignored",
                ticker="SPY",
                side="buy",
                qty=1,
            )
            _append_engine_started(
                writer,
                when=now,
                strategies=[
                    {
                        "name": "only-this-one",
                        "approved_commit_sha": "only0001",
                        "regime_filter": [],
                        "risk_envelope_pct": "0.01",
                    }
                ],
            )
            record = engine_main._find_newest_engine_started(tmp)
            self.assertEqual(record["event_type"], "engine_started")


# ---------- format_diagnose_table ----------


class FormatDiagnoseTableTests(unittest.TestCase):
    def _record(
        self,
        *,
        strategies: list[dict] | None = None,
        strategies_loaded: list[str] | None = None,
        ts: str = "2026-04-19T10:00:00.123456+00:00",
    ) -> dict:
        payload: dict = {
            "pid": 999,
            "validator_config_hash": "cafef00d12345678",
            "retired_dir": "/vault/System",
        }
        if strategies is not None:
            payload["strategies"] = strategies
        if strategies_loaded is not None:
            payload["strategies_loaded"] = strategies_loaded
        return {
            "ts": ts,
            "event_type": "engine_started",
            "payload": payload,
        }

    def test_rich_payload_renders_rows(self):
        record = self._record(
            strategies=[
                {
                    "name": "spy-rotational",
                    "approved_commit_sha": "abc1234",
                    "regime_filter": ["risk_on"],
                    "risk_envelope_pct": "0.01",
                },
                {
                    "name": "meanrev-qqq",
                    "approved_commit_sha": "def5678",
                    "regime_filter": ["risk_on", "chop"],
                    "risk_envelope_pct": "0.02",
                },
            ]
        )
        out = engine_main._format_diagnose_table(record)
        self.assertIn("Most recent engine_started: 2026-04-19T10:00:00", out)
        self.assertIn("PID: 999", out)
        self.assertIn("cafef00d12345678", out)
        self.assertIn("spy-rotational", out)
        self.assertIn("abc1234", out)
        self.assertIn("[risk_on]", out)
        self.assertIn("0.01", out)
        self.assertIn("meanrev-qqq", out)
        self.assertIn("def5678", out)
        self.assertIn("[risk_on, chop]", out)

    def test_legacy_payload_falls_back_to_names_list(self):
        record = self._record(strategies_loaded=["legacy-a", "legacy-b"])
        out = engine_main._format_diagnose_table(record)
        self.assertIn("legacy-a", out)
        self.assertIn("legacy-b", out)
        self.assertIn("pre-cycle-5 journal", out)
        # No table header for the rich path:
        self.assertNotIn("approved_commit_sha", out.split("Approved")[1])

    def test_empty_strategies_shows_none(self):
        record = self._record(strategies=[], strategies_loaded=[])
        out = engine_main._format_diagnose_table(record)
        self.assertIn("(none)", out.lower())


# ---------- CLI subprocess ----------


class DiagnoseCLISubprocessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = os.environ.copy()
        cls.env["PYTHONPATH"] = str(REPO_ROOT)

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "execution.engine.main",
                "--diagnose-approved",
                *args,
            ],
            capture_output=True,
            text=True,
            env=self.env,
        )

    def test_empty_journal_prints_not_started_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = self._run("--journal-dir", tmp)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("engine not started in last 24h", result.stdout)

    def test_full_table_from_seeded_journal(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            writer = _make_writer(tmp)
            _append_engine_started(
                writer,
                when=datetime.now(timezone.utc),
                strategies=[
                    {
                        "name": "spy-rotational",
                        "approved_commit_sha": "abcd1234",
                        "regime_filter": ["risk_on"],
                        "risk_envelope_pct": "0.01",
                    }
                ],
            )
            result = self._run("--journal-dir", str(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("spy-rotational", result.stdout)
            self.assertIn("abcd1234", result.stdout)
            self.assertIn("[risk_on]", result.stdout)
            self.assertIn("0.01", result.stdout)

    def test_legacy_only_payload_still_works_from_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            writer = _make_writer(tmp)
            _append_engine_started(
                writer,
                when=datetime.now(timezone.utc),
                strategies_loaded=["old-strategy"],
                include_rich=False,
            )
            result = self._run("--journal-dir", str(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("old-strategy", result.stdout)
            self.assertIn("pre-cycle-5", result.stdout)


# ---------- engine_started payload integration (runtime, not grep) ----------


_STRATEGY_FILE_TEMPLATE = """---
name: {name}
status: approved
strategy_type: hand_crafted
risk_envelope_pct: {risk}
regime_filter:
  - risk_on
approved_at: 2026-04-19T09:00:00+00:00
approved_commit_sha: {commit}
order:
  ticker: SPY
  side: buy
  qty: 10
  limit_price: 500.00
  stop_loss: 495.00
  time_in_force: DAY
---

## How This Works

Buy SPY at 500 limit while regime is risk-on.
"""


_VALIDATOR_CONFIG = {
    "position_size": {
        "max_trade_risk_pct": 0.01,
        "max_ticker_concentration_pct": 0.20,
    },
    "trade_risk": {"max_open_risk_pct": 0.05},
    "leverage": {"cash_only": True, "max_leverage": 1.0},
    "market_hours": {
        "regular_open": "09:30",
        "regular_close": "16:00",
        "allow_pre_market": False,
        "allow_after_hours": False,
    },
    "instrument_whitelist": {"symbols": ["SPY"]},
}


class EngineStartedPayloadContainsRichStrategiesTests(
    unittest.IsolatedAsyncioTestCase
):
    """MiniMax R2 F4: Replace the grep test with a real runtime check.

    Spin up a minimal Engine via MockIBKRConnector, let it run one tick
    to emit the engine_started event, then read it back and assert the
    cycle-5 per-strategy metadata block is well-formed: list-of-dicts
    with the exact keys operators consume through --diagnose-approved.
    """

    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name)
        self.journal_dir = base / "journal"
        self.journal_dir.mkdir()
        self.strategies_dir = base / "strategies"
        self.strategies_dir.mkdir()
        self.kill_path = base / ".killed"

        # Two approved strategies with distinguishable metadata so the
        # assertion catches any accidental swap / loss of per-strategy
        # fields (e.g. if the loop copied the same snapshot twice).
        (self.strategies_dir / "spy-rotational.md").write_text(
            _STRATEGY_FILE_TEMPLATE.format(
                name="spy-rotational", risk="0.01", commit="abc1234"
            ),
            encoding="utf-8",
        )
        (self.strategies_dir / "meanrev-qqq.md").write_text(
            _STRATEGY_FILE_TEMPLATE.format(
                name="meanrev-qqq", risk="0.02", commit="def5678"
            ),
            encoding="utf-8",
        )

        self.connector = MockIBKRConnector()
        self.journal = JournalWriter(
            base_dir=self.journal_dir, git_sha="test01"
        )

    async def asyncTearDown(self):
        self._tmp.cleanup()

    async def test_strategies_payload_is_well_formed(self):
        engine = Engine(
            connector=self.connector,
            journal=self.journal,
            validator_config=_VALIDATOR_CONFIG,
            engine_config=EngineConfig(
                tick_seconds=DEFAULT_TICK_SECONDS,
                strategies_dir=self.strategies_dir,
                kill_path=self.kill_path,
            ),
        )
        await engine.tick_once()
        events = self.journal.read_all()
        started = [e for e in events if e["event_type"] == "engine_started"]
        self.assertEqual(len(started), 1, "expected exactly one engine_started event")

        payload = started[0]["payload"]
        # Backwards-compat field still emitted.
        self.assertIn("strategies_loaded", payload)
        # Cycle-5 rich field present and well-shaped.
        self.assertIn("strategies", payload)
        self.assertIsInstance(payload["strategies"], list)
        self.assertEqual(
            len(payload["strategies"]),
            2,
            f"expected 2 strategies, got {payload['strategies']}",
        )

        by_name = {s["name"]: s for s in payload["strategies"]}
        self.assertEqual(set(by_name), {"spy-rotational", "meanrev-qqq"})

        spy = by_name["spy-rotational"]
        self.assertEqual(spy["approved_commit_sha"], "abc1234")
        self.assertEqual(spy["regime_filter"], ["risk_on"])
        # risk_envelope_pct is a Decimal in the snapshot and stringified
        # for JSON round-trip. Exact string form depends on Decimal's
        # repr -- assert semantic equality via Decimal parse.
        from decimal import Decimal

        self.assertEqual(Decimal(spy["risk_envelope_pct"]), Decimal("0.01"))

        qqq = by_name["meanrev-qqq"]
        self.assertEqual(qqq["approved_commit_sha"], "def5678")
        self.assertEqual(Decimal(qqq["risk_envelope_pct"]), Decimal("0.02"))


# ---------- malformed payload edge case (MiniMax R2 F6) ----------


class FormatDiagnoseTableMalformedInputTests(unittest.TestCase):
    """If a journal record has `strategies` but it's not a list (maybe
    the engine source gets reverted to emit a scalar, or a manual edit
    corrupts a record), the formatter must degrade gracefully rather
    than blow up with a TypeError. Fall through to the legacy list."""

    def test_strategies_field_is_scalar_falls_back_to_legacy(self):
        record = {
            "ts": "2026-04-19T10:00:00+00:00",
            "event_type": "engine_started",
            "payload": {
                "strategies": "not-a-list",
                "strategies_loaded": ["legacy-one"],
            },
        }
        out = engine_main._format_diagnose_table(record)
        self.assertIn("legacy-one", out)
        self.assertIn("pre-cycle-5", out)

    def test_strategies_field_is_none_falls_back_to_legacy(self):
        record = {
            "ts": "2026-04-19T10:00:00+00:00",
            "event_type": "engine_started",
            "payload": {
                "strategies": None,
                "strategies_loaded": ["legacy-two"],
            },
        }
        out = engine_main._format_diagnose_table(record)
        self.assertIn("legacy-two", out)

    def test_record_is_not_mapping_does_not_raise(self):
        # Codex R7 P1 #3: a corrupt journal record that isn't a dict
        # must not crash the formatter.
        out = engine_main._format_diagnose_table("oops-not-a-dict")
        self.assertIn("malformed", out.lower())

    def test_payload_is_scalar_does_not_raise(self):
        # Codex R7 P1 #3: record is a dict but payload is a string.
        record = {
            "ts": "2026-04-19T10:00:00+00:00",
            "event_type": "engine_started",
            "payload": "oops-not-a-dict",
        }
        out = engine_main._format_diagnose_table(record)
        self.assertIn("malformed", out.lower())
        self.assertIn("2026-04-19", out)

    def test_strategies_list_contains_non_dict_entries(self):
        # Codex R7 P1 #3: a strategies list with some non-dict
        # entries (e.g. strings from a schema-corrupted record)
        # must render the well-formed ones and note the skipped count.
        record = {
            "ts": "2026-04-19T10:00:00+00:00",
            "event_type": "engine_started",
            "payload": {
                "strategies": [
                    {
                        "name": "good-one",
                        "approved_commit_sha": "ok12345",
                        "regime_filter": ["risk_on"],
                        "risk_envelope_pct": "0.01",
                    },
                    "bad-string",  # not a dict
                    None,  # also not a dict
                ],
            },
        }
        out = engine_main._format_diagnose_table(record)
        self.assertIn("good-one", out)
        self.assertIn("malformed", out)
        self.assertIn("2 malformed", out)

    def test_strategies_list_with_missing_keys_does_not_raise(self):
        # R4-minimax F3: a list of dicts each missing some per-strategy
        # keys (e.g. a partially-written journal record, or a record
        # from a pre-cycle-5 engine that knew the field but left it
        # empty) must not crash the formatter. `.get` with defaults
        # should keep everything safe, but prove it by exercise.
        record = {
            "ts": "2026-04-19T10:00:00+00:00",
            "event_type": "engine_started",
            "payload": {
                "strategies": [
                    {"name": "partial"},  # every other key missing
                    {
                        "name": "another",
                        "approved_commit_sha": None,  # explicit null
                        "regime_filter": None,
                        "risk_envelope_pct": None,
                    },
                ],
            },
        }
        out = engine_main._format_diagnose_table(record)
        self.assertIn("partial", out)
        self.assertIn("another", out)
        # No traceback / exception reached the caller: if we got here,
        # the formatter handled missing keys cleanly.


if __name__ == "__main__":
    unittest.main()
