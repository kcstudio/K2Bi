"""Tests for the K2Bi journal sync lag detector."""

from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / ".claude" / "hooks" / "journal-sync-lag-detector.py"
LEGACY_SCRIPT_PATH = ROOT / "scripts" / "journal-sync-lag-detector.py"
SETTINGS_PATH = ROOT / ".claude" / "settings.json"
DEPLOY_CONFIG_PATH = ROOT / "scripts" / "deploy-config.yml"
NOW = datetime(2026, 5, 30, 8, 30, tzinfo=timezone.utc)


def _load_module() -> Any:
    if not HOOK_PATH.exists():
        raise AssertionError(".claude/hooks/journal-sync-lag-detector.py is missing")
    spec = importlib.util.spec_from_file_location(
        "journal_sync_lag_detector",
        HOOK_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load journal-sync-lag-detector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(entry_id: str, ts: datetime | str, event_type: str = "engine_started") -> dict[str, Any]:
    ts_text = ts.isoformat() if isinstance(ts, datetime) else ts
    return {
        "ts": ts_text,
        "schema_version": 2,
        "event_type": event_type,
        "trade_id": None,
        "journal_entry_id": entry_id,
        "strategy": None,
        "git_sha": "test",
        "payload": {},
    }


def _seed_journal(vault_root: Path, date_str: str, rows: list[dict[str, Any] | str]) -> None:
    journal_dir = vault_root / "raw" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{date_str}.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            if isinstance(row, str):
                handle.write(row + "\n")
            else:
                handle.write(json.dumps(row) + "\n")


class JournalSyncLagDetectorTests(unittest.TestCase):
    def test_detector_lives_in_local_claude_hook_not_scripts_deploy_lane(self) -> None:
        self.assertTrue(HOOK_PATH.exists())
        self.assertFalse(LEGACY_SCRIPT_PATH.exists())
        script_lane_matches = [
            path
            for path in (ROOT / "scripts").rglob("journal-sync-lag-detector.py")
        ]
        self.assertEqual(script_lane_matches, [])

        deploy_config = DEPLOY_CONFIG_PATH.read_text(encoding="utf-8")
        self.assertIn("- .claude/hooks", deploy_config)
        self.assertNotIn("path: .claude/hooks", deploy_config)

    def test_session_start_hook_invokes_detector(self) -> None:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        session_start = hooks.get("SessionStart")
        self.assertIsInstance(session_start, list)
        commands = [
            hook.get("command")
            for item in session_start
            for hook in item.get("hooks", [])
        ]
        self.assertIn(
            "${CLAUDE_PROJECT_DIR}/.claude/hooks/journal-sync-lag-detector.py session-start",
            commands,
        )

    def test_no_pretool_step0_state_cache_gate_exists(self) -> None:
        settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        hooks = settings.get("hooks", {})
        pre_tool = hooks.get("PreToolUse")
        self.assertIsInstance(pre_tool, list)
        commands = [
            hook.get("command")
            for item in pre_tool
            for hook in item.get("hooks", [])
        ]
        self.assertNotIn(
            "${CLAUDE_PROJECT_DIR}/.claude/hooks/journal-sync-lag-detector.py pretool-use",
            commands,
        )
        hook_text = HOOK_PATH.read_text(encoding="utf-8")
        forbidden_cache_terms = (
            "pretool-use",
            "STATE_FILE_NAME",
            "STEP0_STATE_MAX_AGE_SECONDS",
            "K2BI_JOURNAL_SYNC_STATE_DIR",
            "write_step0_state",
            "run_pretool_guard",
            "_recent_passing_step0",
        )
        for term in forbidden_cache_terms:
            with self.subTest(term):
                self.assertNotIn(term, hook_text)

    def test_detector_lock_path_matches_journal_writer_sidecar_protocol(self) -> None:
        module = _load_module()
        from execution.journal.writer import JournalWriter

        journal_path = Path("x.jsonl")
        self.assertEqual(module._lock_path_for(journal_path), Path("x.jsonl.lock"))
        self.assertEqual(
            module._lock_path_for(journal_path),
            JournalWriter._lock_path_for(journal_path),
        )

    def test_syncthing_lag_when_vps_current_local_stale(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            local_ts = NOW - timedelta(seconds=1800)
            vps_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-old", local_ts)])
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=vps_ts,
                latest_journal_entry_id="vps-current",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            result = module.check_journal_sync(
                vault_root=vault_root,
                now_utc=NOW,
                threshold_seconds=900,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(result.classification, "syncthing_lag")
        self.assertEqual(result.status, "WARN")
        self.assertGreater(result.local_lag_seconds, 900)
        output = module.format_result(result)
        self.assertIn(local_ts.isoformat(), output)
        self.assertIn(vps_ts.isoformat(), output)

    def test_vps_journaling_stopped_when_vps_tail_stale_on_vps_clock(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            stale_ts = NOW - timedelta(seconds=1800)
            _seed_journal(vault_root, "2026-05-30", [_event("local-stale", stale_ts)])
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=stale_ts,
                latest_journal_entry_id="vps-stale",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            result = module.check_journal_sync(
                vault_root=vault_root,
                now_utc=NOW,
                threshold_seconds=900,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(result.classification, "vps_journaling_stopped")
        self.assertEqual(result.status, "FAIL")
        self.assertNotEqual(result.classification, "syncthing_lag")

    def test_ssh_failure_classified_without_syncthing_guess(self) -> None:
        module = _load_module()
        failures = [
            ("timeout", module.RemoteEvidenceError("ssh timed out")),
            ("exit_78", module.RemoteEvidenceError("ssh exited 78")),
            ("exit_255", module.RemoteEvidenceError("ssh exited 255")),
        ]
        for _label, exc in failures:
            with self.subTest(str(exc)):
                with tempfile.TemporaryDirectory() as td:
                    vault_root = Path(td)
                    _seed_journal(
                        vault_root,
                        "2026-05-30",
                        [_event("local-current", NOW - timedelta(seconds=60))],
                    )

                    def raise_error() -> Any:
                        raise exc

                    result = module.check_journal_sync(
                        vault_root=vault_root,
                        now_utc=NOW,
                        threshold_seconds=900,
                        fetch_remote=raise_error,
                    )

                self.assertEqual(result.classification, "ssh_evidence_collection_failed")
                self.assertEqual(result.status, "FAIL")
                rendered = module.format_result(result)
                self.assertNotIn("syncthing_lag", rendered)
                self.assertNotIn("vps_journaling_stopped", rendered)

    def test_threshold_boundary_900_seconds_is_pass_and_901_seconds_warns(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            vps_ts = NOW - timedelta(seconds=60)
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=vps_ts,
                latest_journal_entry_id="vps-current",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )
            _seed_journal(
                vault_root,
                "2026-05-30",
                [_event("local-boundary", vps_ts - timedelta(seconds=900))],
            )

            boundary = module.check_journal_sync(
                vault_root=vault_root,
                now_utc=NOW,
                threshold_seconds=900,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(boundary.status, "PASS")
        self.assertEqual(boundary.classification, "local_journal_current")
        self.assertEqual(boundary.local_lag_seconds, 900)

        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-30",
                [_event("local-warn", vps_ts - timedelta(seconds=901))],
            )

            over = module.check_journal_sync(
                vault_root=vault_root,
                now_utc=NOW,
                threshold_seconds=900,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(over.status, "WARN")
        self.assertEqual(over.classification, "syncthing_lag")
        self.assertEqual(over.local_lag_seconds, 901)

    def test_session_start_output_is_transcript_ready_and_has_no_telegram_side_effect(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            local_ts = NOW - timedelta(seconds=120)
            vps_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-current", local_ts)])
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=vps_ts,
                latest_journal_entry_id="vps-current",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            result = module.run_precheck(
                mode="session-start",
                vault_root=vault_root,
                threshold_seconds=900,
                now_utc=NOW,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(result.exit_code, 0)
        self.assertIn("K2Bi journal sync pre-check", result.output)
        self.assertIn("status=PASS", result.output)
        self.assertIn("classification=local_journal_current", result.output)
        self.assertIn("threshold_seconds=900", result.output)
        self.assertIn(f"local_latest_ts={local_ts.isoformat()}", result.output)
        self.assertIn("local_journal_entry_id=local-current", result.output)
        self.assertIn(f"vps_latest_ts={vps_ts.isoformat()}", result.output)
        self.assertIn("vps_journal_entry_id=vps-current", result.output)
        self.assertNotIn("send-telegram.sh", result.output)

    def test_session_start_warn_surfaces_without_blocking_session(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            local_ts = NOW - timedelta(seconds=1800)
            vps_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-old", local_ts)])
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=vps_ts,
                latest_journal_entry_id="vps-current",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            result = module.run_precheck(
                mode="session-start",
                vault_root=vault_root,
                threshold_seconds=900,
                now_utc=NOW,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(result.result.status, "WARN")
        self.assertEqual(result.result.classification, "syncthing_lag")
        self.assertEqual(result.exit_code, 0)
        self.assertIn("status=WARN", result.output)
        self.assertIn("classification=syncthing_lag", result.output)

    def test_session_start_fail_exits_nonzero(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-30",
                [_event("local-current", NOW - timedelta(seconds=60))],
            )

            def raise_error() -> Any:
                raise module.RemoteEvidenceError("ssh exited 78")

            result = module.run_precheck(
                mode="session-start",
                vault_root=vault_root,
                threshold_seconds=900,
                now_utc=NOW,
                fetch_remote=raise_error,
            )

        self.assertEqual(result.result.status, "FAIL")
        self.assertEqual(result.result.classification, "ssh_evidence_collection_failed")
        self.assertEqual(result.exit_code, 2)
        self.assertIn("status=FAIL", result.output)

    def test_fixture_journals_across_utc_boundary_use_yesterday_and_today(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            yesterday_ts = datetime(2026, 5, 29, 23, 59, tzinfo=timezone.utc)
            today_ts = datetime(2026, 5, 30, 0, 1, tzinfo=timezone.utc)
            _seed_journal(
                vault_root,
                "2026-05-29",
                [_event("yesterday", yesterday_ts)],
            )
            _seed_journal(
                vault_root,
                "2026-05-30",
                [
                    _event("today-newest", today_ts),
                    "{malformed-json",
                    {"ts": "not-a-date", "journal_entry_id": "bad-tail"},
                ],
            )

            latest = module.read_latest_local_event(vault_root, NOW)

        self.assertIsNotNone(latest)
        assert latest is not None
        self.assertEqual(latest.journal_entry_id, "today-newest")
        self.assertEqual(latest.ts, today_ts)

    def test_local_lock_failure_fails_closed_instead_of_guessing_syncthing_lag(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-30",
                [_event("local-current", NOW - timedelta(seconds=60))],
            )
            flags_seen: list[int] = []
            modes_seen: list[int] = []
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=NOW - timedelta(seconds=60),
                latest_journal_entry_id="vps-current",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            def fail_lock_open(_path: str, flags: int, mode: int) -> int:
                flags_seen.append(flags)
                modes_seen.append(mode)
                raise OSError("lock unavailable")

            with (
                patch.object(module.os, "open", side_effect=fail_lock_open),
                patch("builtins.open", side_effect=AssertionError("read without lock")),
            ):
                result = module.check_journal_sync(
                    vault_root=vault_root,
                    now_utc=NOW,
                    threshold_seconds=900,
                    fetch_remote=lambda: remote,
                )

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.classification, "local_journal_unreadable")
        self.assertNotEqual(result.classification, "syncthing_lag")
        self.assertTrue(flags_seen)
        self.assertEqual(modes_seen, [0o644])
        if hasattr(os, "O_CLOEXEC"):
            self.assertTrue(flags_seen[0] & os.O_CLOEXEC)

    def test_local_read_error_fails_closed_instead_of_falling_back_to_yesterday(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            _seed_journal(
                vault_root,
                "2026-05-29",
                [_event("yesterday", NOW - timedelta(hours=12))],
            )
            _seed_journal(
                vault_root,
                "2026-05-30",
                [_event("today", NOW - timedelta(seconds=60))],
            )
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=NOW - timedelta(seconds=60),
                latest_journal_entry_id="vps-current",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            with patch("builtins.open", side_effect=OSError("permission denied")):
                result = module.check_journal_sync(
                    vault_root=vault_root,
                    now_utc=NOW,
                    threshold_seconds=900,
                    fetch_remote=lambda: remote,
                )

        self.assertEqual(result.status, "FAIL")
        self.assertEqual(result.classification, "local_journal_unreadable")

    def test_tied_timestamp_different_entry_id_warns_because_ids_are_unique(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as td:
            vault_root = Path(td)
            shared_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-id", shared_ts)])
            remote = module.RemoteJournalSnapshot(
                vps_now=NOW,
                latest_event_ts=shared_ts,
                latest_journal_entry_id="vps-id",
                source_file="/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            )

            result = module.check_journal_sync(
                vault_root=vault_root,
                now_utc=NOW,
                threshold_seconds=900,
                fetch_remote=lambda: remote,
            )

        self.assertEqual(result.status, "WARN")
        self.assertEqual(result.classification, "syncthing_lag")
        self.assertEqual(result.local_lag_seconds, 0)


if __name__ == "__main__":
    unittest.main()
