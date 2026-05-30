"""CLI tests for .claude/hooks/journal-sync-lag-detector.py."""

from __future__ import annotations

import importlib.util
import io
import json
import re
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / ".claude" / "hooks" / "journal-sync-lag-detector.py"
NOW = datetime(2026, 5, 30, 8, 30, tzinfo=timezone.utc)


def _marker_from_remote_script(script: str) -> str:
    match = re.search(r'^MARKER = "([^"]+)"$', script, flags=re.MULTILINE)
    if match is None:
        raise AssertionError("remote script does not define a per-invocation MARKER")
    return match.group(1)


def _framed_payload_line(marker: str, payload: dict[str, Any], length_delta: int = 0) -> str:
    raw_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{marker} {len(raw_payload) + length_delta} {raw_payload}\n"


def _ssh_run_with_payload(
    payload: dict[str, Any],
    *,
    stdout_prefix: str = "",
    stdout_suffix: str = "",
    stderr: str = "",
    returncode: int = 0,
    duplicate: bool = False,
    length_delta: int = 0,
) -> Any:
    def run(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        marker = _marker_from_remote_script(kwargs["input"])
        line = _framed_payload_line(marker, payload, length_delta=length_delta)
        stdout = stdout_prefix + line
        if duplicate:
            stdout += line
        stdout += stdout_suffix
        return subprocess.CompletedProcess(
            args=args,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return run


def _load_module() -> Any:
    if not HOOK_PATH.exists():
        raise AssertionError(".claude/hooks/journal-sync-lag-detector.py is missing")
    spec = importlib.util.spec_from_file_location(
        "journal_sync_lag_detector_cli",
        HOOK_PATH,
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load journal-sync-lag-detector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(entry_id: str, ts: datetime) -> dict[str, Any]:
    return {
        "ts": ts.isoformat(),
        "schema_version": 2,
        "event_type": "engine_started",
        "trade_id": None,
        "journal_entry_id": entry_id,
        "strategy": None,
        "git_sha": "test",
        "payload": {},
    }


def _seed_journal(vault_root: Path, date_str: str, rows: list[dict[str, Any]]) -> None:
    journal_dir = vault_root / "raw" / "journal"
    journal_dir.mkdir(parents=True, exist_ok=True)
    path = journal_dir / f"{date_str}.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


class JournalSyncLagDetectorCliTests(unittest.TestCase):
    def test_step0_precheck_cli_prints_classification_without_telegram_or_state_file(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as vault_td, tempfile.TemporaryDirectory() as state_td:
            vault_root = Path(vault_td)
            state_dir = Path(state_td)
            local_ts = NOW - timedelta(seconds=1800)
            vps_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-old", local_ts)])
            remote_payload = {
                "vps_now": NOW.isoformat(),
                "latest_event_ts": vps_ts.isoformat(),
                "latest_journal_entry_id": "vps-current",
                "source_file": "/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            }
            stdout = io.StringIO()
            with patch.object(module.subprocess, "run", side_effect=_ssh_run_with_payload(remote_payload)) as run:
                with redirect_stdout(stdout):
                    code = module.main(
                        [
                            "step0",
                            "--vault-root",
                            str(vault_root),
                            "--now-utc",
                            NOW.isoformat(),
                            "--threshold-seconds",
                            "900",
                        ]
                    )

            self.assertEqual(list(state_dir.iterdir()), [])

        self.assertEqual(code, 1)
        out = stdout.getvalue()
        self.assertIn("status=WARN", out)
        self.assertIn("classification=syncthing_lag", out)
        self.assertIn("threshold_seconds=900", out)
        self.assertIn("local_journal_entry_id=local-old", out)
        self.assertIn("vps_journal_entry_id=vps-current", out)
        self.assertNotIn("send-telegram.sh", " ".join(str(arg) for call in run.call_args_list for arg in call.args))

    def test_step0_pass_does_not_write_state_cache(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as vault_td, tempfile.TemporaryDirectory() as state_td:
            vault_root = Path(vault_td)
            state_dir = Path(state_td)
            local_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-current", local_ts)])
            remote_payload = {
                "vps_now": NOW.isoformat(),
                "latest_event_ts": local_ts.isoformat(),
                "latest_journal_entry_id": "local-current",
                "source_file": "/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            }
            stdout = io.StringIO()
            with patch.object(module.subprocess, "run", side_effect=_ssh_run_with_payload(remote_payload)):
                with redirect_stdout(stdout):
                    code = module.main(
                        [
                            "step0",
                            "--vault-root",
                            str(vault_root),
                            "--now-utc",
                            NOW.isoformat(),
                        ]
                    )

            self.assertEqual(list(state_dir.iterdir()), [])

        self.assertEqual(code, 0)
        self.assertIn("status=PASS", stdout.getvalue())

    def test_pretool_use_cli_mode_is_not_available(self) -> None:
        module = _load_module()
        stderr = io.StringIO()
        with patch.object(module.sys, "stderr", stderr):
            with self.assertRaises(SystemExit) as raised:
                module.main(
                    [
                        "pretool-use",
                        "--now-utc",
                        NOW.isoformat(),
                    ]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", stderr.getvalue())

    def test_state_dir_cli_option_is_not_available(self) -> None:
        module = _load_module()
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as vault_td, tempfile.TemporaryDirectory() as state_td:
            with patch.object(module.sys, "stderr", stderr):
                with self.assertRaises(SystemExit) as raised:
                    module.main(
                        [
                            "step0",
                            "--vault-root",
                            vault_td,
                            "--state-dir",
                            state_td,
                            "--now-utc",
                            NOW.isoformat(),
                        ]
                    )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("unrecognized arguments: --state-dir", stderr.getvalue())

    def test_session_start_cli_fail_exits_nonzero(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as vault_td:
            vault_root = Path(vault_td)
            _seed_journal(vault_root, "2026-05-30", [_event("local-current", NOW - timedelta(seconds=60))])
            completed = subprocess.CompletedProcess(
                args=["scripts/ssh-vps.sh"],
                returncode=78,
                stdout="stdout clue",
                stderr="blocked by test",
            )
            stdout = io.StringIO()
            with patch.object(module.subprocess, "run", return_value=completed):
                with redirect_stdout(stdout):
                    code = module.main(
                        [
                            "session-start",
                            "--vault-root",
                            str(vault_root),
                            "--now-utc",
                            NOW.isoformat(),
                        ]
                    )

        self.assertEqual(code, 2)
        out = stdout.getvalue()
        self.assertIn("status=FAIL", out)
        self.assertIn("classification=ssh_evidence_collection_failed", out)
        self.assertIn("stderr=blocked by test", out)
        self.assertIn("stdout=stdout clue", out)

    def test_session_start_cli_warn_prints_and_does_not_block(self) -> None:
        module = _load_module()
        with tempfile.TemporaryDirectory() as vault_td:
            vault_root = Path(vault_td)
            local_ts = NOW - timedelta(seconds=1800)
            vps_ts = NOW - timedelta(seconds=60)
            _seed_journal(vault_root, "2026-05-30", [_event("local-old", local_ts)])
            remote_payload = {
                "vps_now": NOW.isoformat(),
                "latest_event_ts": vps_ts.isoformat(),
                "latest_journal_entry_id": "vps-current",
                "source_file": "/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
            }
            stdout = io.StringIO()
            with patch.object(module.subprocess, "run", side_effect=_ssh_run_with_payload(remote_payload)):
                with redirect_stdout(stdout):
                    code = module.main(
                        [
                            "session-start",
                            "--vault-root",
                            str(vault_root),
                            "--now-utc",
                            NOW.isoformat(),
                        ]
                    )

        self.assertEqual(code, 0)
        self.assertIn("status=WARN", stdout.getvalue())
        self.assertIn("classification=syncthing_lag", stdout.getvalue())

    def test_step0_help_marks_deploy_closeout_and_evidence_sessions(self) -> None:
        module = _load_module()
        help_text = module.build_parser().format_help().lower()
        self.assertIn("manual step 0 pre-check required", help_text)
        self.assertIn("deploy", help_text)
        self.assertIn("closeout", help_text)
        self.assertIn("evidence-collection", help_text)

    def test_remote_ssh_uses_stdin_payload_and_framed_random_marker_json(self) -> None:
        module = _load_module()
        remote_payload = {
            "vps_now": NOW.isoformat(),
            "latest_event_ts": (NOW - timedelta(seconds=60)).isoformat(),
            "latest_journal_entry_id": "vps-current",
            "source_file": "/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
        }
        static_marker_pollution = (
            "ssh banner\n"
            "K2BI_JOURNAL_SYNC_JSON "
            '{"vps_now":"2001-01-01T00:00:00+00:00","latest_event_ts":null}\n'
        )
        with patch.object(
            module.subprocess,
            "run",
            side_effect=_ssh_run_with_payload(
                remote_payload,
                stdout_prefix=static_marker_pollution,
                stdout_suffix="trailing notice\n",
            ),
        ) as run:
            snapshot = module.fetch_remote_journal_snapshot(
                ssh_script=Path("scripts/ssh-vps.sh"),
                timeout_seconds=15,
            )

        self.assertEqual(snapshot.latest_journal_entry_id, "vps-current")
        args, kwargs = run.call_args
        self.assertEqual(args[0][1], "python3 -")
        self.assertNotEqual(kwargs["input"], module.REMOTE_JOURNAL_PY)
        self.assertIn("MARKER =", kwargs["input"])
        self.assertIn("PAYLOAD_JSON =", kwargs["input"])

    def test_remote_payload_duplicate_dynamic_marker_fails_closed(self) -> None:
        module = _load_module()
        remote_payload = {
            "vps_now": NOW.isoformat(),
            "latest_event_ts": (NOW - timedelta(seconds=60)).isoformat(),
            "latest_journal_entry_id": "vps-current",
            "source_file": "/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
        }
        with patch.object(
            module.subprocess,
            "run",
            side_effect=_ssh_run_with_payload(remote_payload, duplicate=True),
        ):
            with self.assertRaises(module.RemoteEvidenceError) as raised:
                module.fetch_remote_journal_snapshot(
                    ssh_script=Path("scripts/ssh-vps.sh"),
                    timeout_seconds=15,
                )

        self.assertIn("multiple evidence payloads", str(raised.exception))

    def test_remote_payload_length_mismatch_fails_closed(self) -> None:
        module = _load_module()
        remote_payload = {
            "vps_now": NOW.isoformat(),
            "latest_event_ts": (NOW - timedelta(seconds=60)).isoformat(),
            "latest_journal_entry_id": "vps-current",
            "source_file": "/home/k2bi/Projects/K2Bi-Vault/raw/journal/2026-05-30.jsonl",
        }
        with patch.object(
            module.subprocess,
            "run",
            side_effect=_ssh_run_with_payload(remote_payload, length_delta=1),
        ):
            with self.assertRaises(module.RemoteEvidenceError) as raised:
                module.fetch_remote_journal_snapshot(
                    ssh_script=Path("scripts/ssh-vps.sh"),
                    timeout_seconds=15,
                )

        self.assertIn("length mismatch", str(raised.exception))

    def test_ssh_nonzero_diagnostics_include_truncated_stderr_and_stdout(self) -> None:
        module = _load_module()
        completed = subprocess.CompletedProcess(
            args=["scripts/ssh-vps.sh"],
            returncode=255,
            stdout="stdout-" + ("x" * 5000),
            stderr="stderr-" + ("y" * 5000),
        )
        with patch.object(module.subprocess, "run", return_value=completed):
            with self.assertRaises(module.RemoteEvidenceError) as raised:
                module.fetch_remote_journal_snapshot(
                    ssh_script=Path("scripts/ssh-vps.sh"),
                    timeout_seconds=15,
                )

        message = str(raised.exception)
        self.assertIn("ssh exited 255", message)
        self.assertIn("stderr=stderr-", message)
        self.assertIn("stdout=stdout-", message)
        self.assertIn("<truncated>", message)
        self.assertNotIn("y" * 3000, message)
        self.assertNotIn("x" * 3000, message)

    def test_remote_payload_scans_bounded_tail_not_entire_journal_file(self) -> None:
        module = _load_module()

        self.assertIn("MAX_TAIL_BYTES", module.REMOTE_JOURNAL_PY)
        self.assertIn(".seek(", module.REMOTE_JOURNAL_PY)
        self.assertNotIn(".read_text(", module.REMOTE_JOURNAL_PY)
        self.assertNotIn('.decode("utf-8", "replace")', module.REMOTE_JOURNAL_PY)
        self.assertIn('line.decode("utf-8")', module.REMOTE_JOURNAL_PY)
        self.assertIn("except UnicodeDecodeError", module.REMOTE_JOURNAL_PY)

    def test_no_cron_or_background_install_path(self) -> None:
        module = _load_module()
        commands = set(module.CLI_COMMANDS)
        self.assertEqual(commands, {"session-start", "step0"})
        help_text = module.build_parser().format_help().lower()
        forbidden_help = ("cron", "launchagent", "scheduler", "install")
        for word in forbidden_help:
            with self.subTest(word):
                self.assertNotIn(word, help_text)

        script_text = HOOK_PATH.read_text(encoding="utf-8")
        forbidden_source = (
            "crontab",
            "LaunchAgent",
            "StartInterval",
            "KeepAlive",
            "RunAtLoad",
            "install-cron",
        )
        for word in forbidden_source:
            with self.subTest(word):
                self.assertNotIn(word, script_text)


if __name__ == "__main__":
    unittest.main()
