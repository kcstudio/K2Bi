"""Tests for Stage 15 trade retros."""

from __future__ import annotations

import json
import os
import subprocess
import shutil
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from scripts.lib import invest_trade_retro as retro


FIXTURE = Path(__file__).parent / "fixtures" / "stage15_journal" / "2026-06-16.jsonl"
STRATEGY_ID = "fixture-stage15-breakout"
CLOSURE_ID = "01RETROEXIT000000000000000"


class InvestTradeRetroTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.vault = self.base / "K2Bi-Vault"
        self.repo = self.base / "K2Bi"
        self.journal_dir = self.vault / "raw" / "journal"
        self.memory_dir = self.vault / "System" / "memory"
        self.journal_dir.mkdir(parents=True)
        self.memory_dir.mkdir(parents=True)
        self.repo.mkdir()
        (self.memory_dir / "self_improve_learnings.md").write_text(
            "# Self Improve Learnings\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _copy_fixture(self) -> None:
        shutil.copy(FIXTURE, self.journal_dir / "2026-06-16.jsonl")

    def _fixture_records(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in FIXTURE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _write_records(self, *records: dict[str, object]) -> None:
        (self.journal_dir / "2026-06-16.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def _retro_files(self) -> list[Path]:
        insights_dir = self.vault / "wiki" / "insights"
        if not insights_dir.exists():
            return []
        return sorted(insights_dir.glob("*_trade-retro_*.md"))

    def test_run_writes_retro_and_learning_from_closed_trade_fixture(self) -> None:
        self._copy_fixture()

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(result["strategy_id"], STRATEGY_ID)
        self.assertEqual(result["ticker"], "TST")
        self.assertEqual(
            result["source"]["closure_journal_entry_id"],
            CLOSURE_ID,
        )
        self.assertEqual(
            result["source"]["closure_source_file"],
            "raw/journal/2026-06-16.jsonl",
        )
        self.assertEqual(result["source"]["closure_source_line"], 3)
        self.assertEqual(
            result["source"]["entry_source_file"],
            "raw/journal/2026-06-16.jsonl",
        )
        self.assertEqual(result["source"]["entry_source_line"], 1)
        self.assertTrue(result["entry"]["found"])
        self.assertEqual(result["outcome"]["realized_pnl_usd"], "-80.00")
        self.assertEqual(result["outcome"]["return_pct"], "-8.00")
        self.assertGreaterEqual(len(result["concrete_changes"]), 1)
        self.assertEqual(result["learn"]["learning_id"], "L-2026-06-16-001")

        retro_path = self.vault / result["retro_path"]
        self.assertTrue(retro_path.exists())
        retro_text = retro_path.read_text(encoding="utf-8")
        self.assertIn("fixture-stage15-breakout", retro_text)
        self.assertIn("Review entry timing and stop distance", retro_text)

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("### L-2026-06-16-001", learnings)
        self.assertIn(f"**Source:** stage15-retro `{CLOSURE_ID}`", learnings)

    def test_run_is_idempotent_for_same_closure_event(self) -> None:
        self._copy_fixture()

        first = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        first_path = self.vault / first["retro_path"]
        first_text = first_path.read_text(encoding="utf-8")
        second = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(first["retro_path"], second["retro_path"])
        self.assertEqual(first_path.read_text(encoding="utf-8"), first_text)
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(learnings.count("### L-2026-06-16-001"), 1)
        self.assertEqual(learnings.count(f"stage15-retro `{CLOSURE_ID}`"), 1)

    def test_run_is_idempotent_across_processes(self) -> None:
        self._copy_fixture()
        cmd = [
            sys.executable,
            "-m",
            "scripts.lib.invest_trade_retro",
            "run",
            "--strategy",
            STRATEGY_ID,
            "--vault-root",
            str(self.vault),
            "--repo-root",
            str(self.repo),
            "--as-of",
            "2026-06-16",
        ]

        procs = [
            subprocess.Popen(
                cmd,
                cwd=Path(__file__).parents[1],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(4)
        ]
        results = [proc.communicate(timeout=45) + (proc.returncode,) for proc in procs]

        for stdout, stderr, returncode in results:
            self.assertEqual(returncode, 0, msg=f"stdout={stdout}\nstderr={stderr}")
        self.assertEqual(len(self._retro_files()), 1)
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(learnings.count(f"stage15-retro `{CLOSURE_ID}`"), 1)
        self.assertFalse(retro._pending_learning_path(self.vault, CLOSURE_ID).exists())

    def test_run_reuses_existing_retro_for_same_closure_with_new_as_of(self) -> None:
        self._copy_fixture()

        first = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        first_path = self.vault / first["retro_path"]
        first_text = first_path.read_text(encoding="utf-8")

        second = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 17),
        )

        self.assertEqual(second["retro_path"], first["retro_path"])
        self.assertEqual(first_path.read_text(encoding="utf-8"), first_text)
        stored_retro = retro._load_retro_json(first_path)
        self.assertIsNotNone(stored_retro)
        self.assertEqual(stored_retro["retro_date"], "2026-06-16")
        self.assertFalse(
            (self.vault / "wiki" / "insights" / "2026-06-17_trade-retro_fixture-stage15-breakout.md").exists()
        )
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(learnings.count(f"stage15-retro `{CLOSURE_ID}`"), 1)

    def test_existing_retro_missing_learning_repairs_without_new_retro_date(self) -> None:
        self._copy_fixture()

        first = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        first_path = self.vault / first["retro_path"]
        (self.memory_dir / "self_improve_learnings.md").write_text(
            "# Self Improve Learnings\n",
            encoding="utf-8",
        )

        second = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 17),
        )

        self.assertEqual(second["retro_path"], first["retro_path"])
        self.assertEqual(second["retro_date"], "2026-06-16")
        stored_retro = retro._load_retro_json(first_path)
        self.assertIsNotNone(stored_retro)
        self.assertEqual(stored_retro["retro_date"], "2026-06-16")
        self.assertFalse(
            (self.vault / "wiki" / "insights" / "2026-06-17_trade-retro_fixture-stage15-breakout.md").exists()
        )
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(learnings.count(f"stage15-retro `{CLOSURE_ID}`"), 1)
        self.assertIn("### L-2026-06-16-001", learnings)

    def test_existing_retro_learning_id_mismatch_is_repaired(self) -> None:
        self._copy_fixture()
        first = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        retro_path = self.vault / first["retro_path"]
        (self.memory_dir / "self_improve_learnings.md").write_text(
            "# Self Improve Learnings\n\n"
            "## 2026-06-16 -- Trade retro: fixture-stage15-breakout stopped out\n\n"
            "### L-2026-06-16-009\n"
            'distilled-rule: "Manual repair marker."\n\n'
            "- **Area:** workflow\n"
            "- **Distilled rule:** Manual repair marker.\n"
            "- **Learning:** Manual repair marker.\n"
            f"- **Source:** stage15-retro `{CLOSURE_ID}`\n",
            encoding="utf-8",
        )

        second = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(second["learn"]["learning_id"], "L-2026-06-16-009")
        stored_retro = retro._load_retro_json(retro_path)
        self.assertIsNotNone(stored_retro)
        self.assertEqual(stored_retro["learn"]["learning_id"], "L-2026-06-16-009")
        self.assertIn(
            "learn_id: L-2026-06-16-009",
            retro_path.read_text(encoding="utf-8"),
        )

    def test_existing_retro_with_malformed_date_refuses_repair(self) -> None:
        self._copy_fixture()
        first = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        retro_path = self.vault / first["retro_path"]
        stored_retro = retro._load_retro_json(retro_path)
        self.assertIsNotNone(stored_retro)
        stored_retro["retro_date"] = "not-a-date"
        retro_path.write_text(retro._build_markdown(stored_retro), encoding="utf-8")
        (self.memory_dir / "self_improve_learnings.md").write_text(
            "# Self Improve Learnings\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(retro.TradeRetroError, "malformed retro_date"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 17),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)

    def test_repair_learning_not_written_if_retro_rewrite_fails(self) -> None:
        self._copy_fixture()
        first = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        retro_path = self.vault / first["retro_path"]
        retro_text = retro_path.read_text(encoding="utf-8")
        retro_path.write_text(
            retro_text.replace('"learning_id": "L-2026-06-16-001"', '"learning_id_missing": "L-2026-06-16-001"'),
            encoding="utf-8",
        )
        (self.memory_dir / "self_improve_learnings.md").write_text(
            "# Self Improve Learnings\n",
            encoding="utf-8",
        )

        with mock.patch.object(retro, "_write_retro", side_effect=OSError("retro boom")):
            with self.assertRaisesRegex(OSError, "retro boom"):
                retro.run_retro(
                    strategy_id=STRATEGY_ID,
                    vault_root=self.vault,
                    repo_root=self.repo,
                    as_of=date(2026, 6, 16),
                )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertFalse(retro._pending_learning_path(self.vault, CLOSURE_ID).exists())

    def test_source_journal_files_include_all_scanned_jsonl(self) -> None:
        self._copy_fixture()
        unrelated = {
            "ts": "2026-06-12T12:00:00+00:00",
            "schema_version": 2,
            "event_type": "note",
            "journal_entry_id": "01RETROUNRELATED000000000",
            "strategy": "different-strategy",
            "payload": {"note": "scanned but not part of the trade"},
        }
        (self.journal_dir / "2026-06-12.jsonl").write_text(
            json.dumps(unrelated) + "\n",
            encoding="utf-8",
        )

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(
            result["source"]["journal_files"],
            ["raw/journal/2026-06-12.jsonl", "raw/journal/2026-06-16.jsonl"],
        )

    def test_malformed_unrelated_journal_line_is_skipped(self) -> None:
        self._copy_fixture()
        (self.journal_dir / "2026-06-12.jsonl").write_text(
            '{"event_type": "note"\n',
            encoding="utf-8",
        )

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(result["source"]["closure_journal_entry_id"], CLOSURE_ID)
        self.assertEqual(
            result["source"]["journal_files"],
            ["raw/journal/2026-06-12.jsonl", "raw/journal/2026-06-16.jsonl"],
        )

    def test_newest_closure_is_selected_by_timestamp_not_file_order(self) -> None:
        self._copy_fixture()
        older_closure = self._fixture_records()[2]
        older_closure["journal_entry_id"] = "01RETROEXITOLDER0000000000"
        older_closure["ts"] = "2026-06-15T15:00:00.000000+00:00"
        payload = older_closure["payload"]
        self.assertIsInstance(payload, dict)
        payload["stopped_out_at"] = "2026-06-15T15:00:00+00:00"
        payload["fill_price"] = "95.00"
        (self.journal_dir / "2026-06-99.jsonl").write_text(
            json.dumps(older_closure) + "\n",
            encoding="utf-8",
        )

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(result["source"]["closure_journal_entry_id"], CLOSURE_ID)

    def test_entry_fill_selected_by_timestamp_not_file_order(self) -> None:
        records = self._fixture_records()
        entry = records[0]
        closure = records[2]
        (self.journal_dir / "2026-06-16.jsonl").write_text(
            json.dumps(closure) + "\n",
            encoding="utf-8",
        )
        (self.journal_dir / "2026-06-99.jsonl").write_text(
            json.dumps(entry) + "\n",
            encoding="utf-8",
        )

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertTrue(result["entry"]["found"])
        self.assertEqual(result["entry"]["journal_entry_id"], "01RETROENTRY0000000000000")
        self.assertEqual(result["outcome"]["realized_pnl_usd"], "-80.00")

    def test_entry_fill_matches_payload_strategy_id(self) -> None:
        records = self._fixture_records()
        entry_payload = records[0]["payload"]
        self.assertIsInstance(entry_payload, dict)
        entry_payload["strategy_id"] = STRATEGY_ID
        records[0].pop("strategy", None)
        self._write_records(records[0], records[1], records[2])

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertTrue(result["entry"]["found"])
        self.assertEqual(result["entry"]["journal_entry_id"], "01RETROENTRY0000000000000")
        self.assertEqual(result["outcome"]["realized_pnl_usd"], "-80.00")

    def test_equal_timestamp_entry_is_not_prior_fill(self) -> None:
        records = self._fixture_records()
        entry = records[0]
        closure = records[2]
        payload = entry["payload"]
        self.assertIsInstance(payload, dict)
        payload["filled_at"] = "2026-06-16T15:00:00+00:00"
        entry["ts"] = "2026-06-16T15:00:00.000000+00:00"
        self._write_records(entry, records[1], closure)

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertFalse(result["entry"]["found"])
        self.assertIsNone(result["outcome"]["realized_pnl_usd"])

    def test_missing_prices_remain_null_not_string_none(self) -> None:
        records = self._fixture_records()
        entry_payload = records[0]["payload"]
        exit_payload = records[2]["payload"]
        self.assertIsInstance(entry_payload, dict)
        self.assertIsInstance(exit_payload, dict)
        entry_payload.pop("fill_price", None)
        exit_payload.pop("fill_price", None)
        self._write_records(records[0], records[1], records[2])

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertIsNone(result["entry"]["price"])
        self.assertIsNone(result["exit"]["price"])
        self.assertIsNone(result["outcome"]["realized_pnl_usd"])
        self.assertIsNone(result["outcome"]["return_pct"])

    def test_string_none_entry_price_refuses_retro(self) -> None:
        records = self._fixture_records()
        entry_payload = records[0]["payload"]
        self.assertIsInstance(entry_payload, dict)
        entry_payload["fill_price"] = "None"
        self._write_records(records[0], records[1], records[2])

        with self.assertRaisesRegex(retro.TradeRetroError, "invalid entry price"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_non_finite_exit_price_refuses_retro(self) -> None:
        records = self._fixture_records()
        exit_payload = records[2]["payload"]
        self.assertIsInstance(exit_payload, dict)
        exit_payload["fill_price"] = "NaN"
        self._write_records(records[0], records[1], records[2])

        with self.assertRaisesRegex(retro.TradeRetroError, "invalid exit price"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_run_refuses_closed_trade_without_closure_ticker(self) -> None:
        records = self._fixture_records()
        closure = records[2]
        payload = closure["payload"]
        self.assertIsInstance(payload, dict)
        payload.pop("ticker", None)
        closure.pop("ticker", None)
        self._write_records(records[0], records[1], closure)

        with self.assertRaisesRegex(retro.TradeRetroError, "closed trade missing ticker"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_run_refuses_closed_trade_without_closure_timestamp(self) -> None:
        records = self._fixture_records()
        closure = records[2]
        closure.pop("ts", None)
        payload = closure["payload"]
        self.assertIsInstance(payload, dict)
        payload.pop("stopped_out_at", None)
        self._write_records(records[0], records[1], closure)

        with self.assertRaisesRegex(retro.TradeRetroError, "closed trade missing timestamp"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_conflicting_entry_quantities_refuse_retro(self) -> None:
        records = self._fixture_records()
        records[0]["qty"] = 9
        payload = records[0]["payload"]
        self.assertIsInstance(payload, dict)
        payload["fill_qty"] = 10
        self._write_records(records[0], records[1], records[2])

        with self.assertRaisesRegex(retro.TradeRetroError, "conflicting entry quantity"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_non_numeric_entry_quantity_refuses_retro(self) -> None:
        records = self._fixture_records()
        records[0]["qty"] = "abc"
        payload = records[0]["payload"]
        self.assertIsInstance(payload, dict)
        payload["fill_qty"] = "abc"
        self._write_records(records[0], records[1], records[2])

        with self.assertRaisesRegex(retro.TradeRetroError, "invalid entry quantity"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_single_source_non_numeric_entry_quantity_refuses_retro(self) -> None:
        records = self._fixture_records()
        records[0]["qty"] = "abc"
        payload = records[0]["payload"]
        self.assertIsInstance(payload, dict)
        payload.pop("fill_qty", None)
        self._write_records(records[0], records[1], records[2])

        with self.assertRaisesRegex(retro.TradeRetroError, "invalid entry quantity"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_retro_write_refuses_path_outside_insights(self) -> None:
        self._copy_fixture()
        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        result["retro_path"] = "../evil.md"

        with self.assertRaisesRegex(retro.TradeRetroError, "retro path escapes"):
            retro._write_retro(self.vault, result)

        self.assertFalse((self.vault.parent / "evil.md").exists())

    def test_retro_write_refuses_symlinked_insights_directory(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        insights = self.vault / "wiki" / "insights"
        insights.parent.mkdir(parents=True)
        insights.symlink_to(outside, target_is_directory=True)
        self._copy_fixture()

        with self.assertRaisesRegex(retro.TradeRetroError, "retro path escapes"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        self.assertEqual(list(outside.iterdir()), [])
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)

    def test_retro_write_refuses_symlinked_vault_root(self) -> None:
        linked_vault = self.base / "Linked-Vault"
        linked_vault.symlink_to(self.vault, target_is_directory=True)
        self._copy_fixture()

        with self.assertRaisesRegex(retro.TradeRetroError, "retro path escapes"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=linked_vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)

    def test_lock_file_with_extra_hard_link_refuses_retro(self) -> None:
        self._copy_fixture()
        lock_path = self.memory_dir / ".stage15_trade_retro.lock"
        lock_path.write_text("", encoding="utf-8")
        os.link(lock_path, self.memory_dir / "stage15-lock-hardlink")

        with self.assertRaisesRegex(retro.TradeRetroError, "unexpected hard links"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_retro_schema_validation_requires_price_keys(self) -> None:
        self._copy_fixture()
        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        result["entry"].pop("price")

        with self.assertRaisesRegex(retro.TradeRetroError, "entry.price is required"):
            retro._write_retro(self.vault, result)

    def test_retro_schema_validation_requires_ticker(self) -> None:
        self._copy_fixture()
        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )
        result["ticker"] = ""

        with self.assertRaisesRegex(retro.TradeRetroError, "ticker is required"):
            retro._write_retro(self.vault, result)

    def test_learning_rolls_back_if_retro_write_fails(self) -> None:
        self._copy_fixture()

        with mock.patch.object(retro, "_write_retro", side_effect=OSError("boom")):
            with self.assertRaisesRegex(OSError, "boom"):
                retro.run_retro(
                    strategy_id=STRATEGY_ID,
                    vault_root=self.vault,
                    repo_root=self.repo,
                    as_of=date(2026, 6, 16),
                )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_existing_retro_repairs_learning_after_learning_write_failure(self) -> None:
        self._copy_fixture()

        with mock.patch.object(retro, "_write_learning_text", side_effect=OSError("learn boom")):
            with self.assertRaisesRegex(OSError, "learn boom"):
                retro.run_retro(
                    strategy_id=STRATEGY_ID,
                    vault_root=self.vault,
                    repo_root=self.repo,
                    as_of=date(2026, 6, 16),
                )

        self.assertEqual(len(self._retro_files()), 1)
        self.assertTrue(retro._pending_learning_path(self.vault, CLOSURE_ID).exists())
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)

        repaired = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(repaired["learn"]["learning_id"], "L-2026-06-16-001")
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(learnings.count(f"stage15-retro `{CLOSURE_ID}`"), 1)
        self.assertFalse(retro._pending_learning_path(self.vault, CLOSURE_ID).exists())

    def test_pending_marker_remains_if_learning_write_does_not_persist(self) -> None:
        self._copy_fixture()

        with mock.patch.object(retro, "_write_learning_text", return_value=None):
            with self.assertRaisesRegex(retro.TradeRetroError, "learning write verification failed"):
                retro.run_retro(
                    strategy_id=STRATEGY_ID,
                    vault_root=self.vault,
                    repo_root=self.repo,
                    as_of=date(2026, 6, 16),
                )

        self.assertEqual(len(self._retro_files()), 1)
        self.assertTrue(retro._pending_learning_path(self.vault, CLOSURE_ID).exists())
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)

    def test_existing_learning_without_retro_is_reused(self) -> None:
        self._copy_fixture()
        (self.memory_dir / "self_improve_learnings.md").write_text(
            "# Self Improve Learnings\n\n"
            "## 2026-06-16 -- Trade retro: fixture-stage15-breakout stopped out\n\n"
            "### L-2026-06-16-007\n"
            'distilled-rule: "Existing learning from a prior partial run."\n\n'
            "- **Area:** workflow\n"
            "- **Distilled rule:** Existing learning from a prior partial run.\n"
            "- **Learning:** Existing learning from a prior partial run.\n"
            f"- **Source:** stage15-retro `{CLOSURE_ID}`\n",
            encoding="utf-8",
        )

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertEqual(result["learn"]["learning_id"], "L-2026-06-16-007")
        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertEqual(learnings.count(f"stage15-retro `{CLOSURE_ID}`"), 1)

    def test_run_refuses_when_no_closed_trade_exists(self) -> None:
        entry_only = (
            json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[0])
        )
        (self.journal_dir / "2026-06-16.jsonl").write_text(
            json.dumps(entry_only) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(retro.TradeRetroError, "no closed trade"):
            retro.run_retro(
                strategy_id=STRATEGY_ID,
                vault_root=self.vault,
                repo_root=self.repo,
                as_of=date(2026, 6, 16),
            )

        learnings = (self.memory_dir / "self_improve_learnings.md").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("stage15-retro", learnings)
        self.assertEqual(self._retro_files(), [])

    def test_missing_entry_fill_still_retroes_but_marks_pnl_unknown(self) -> None:
        closure_only = (
            json.loads(FIXTURE.read_text(encoding="utf-8").splitlines()[2])
        )
        (self.journal_dir / "2026-06-16.jsonl").write_text(
            json.dumps(closure_only) + "\n",
            encoding="utf-8",
        )

        result = retro.run_retro(
            strategy_id=STRATEGY_ID,
            vault_root=self.vault,
            repo_root=self.repo,
            as_of=date(2026, 6, 16),
        )

        self.assertFalse(result["entry"]["found"])
        self.assertIsNone(result["outcome"]["realized_pnl_usd"])
        self.assertIsNone(result["outcome"]["return_pct"])
        self.assertIn(
            "entry-fill linkage",
            result["concrete_changes"][0]["change"],
        )


if __name__ == "__main__":
    unittest.main()
