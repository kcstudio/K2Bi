#!/usr/bin/env python3
"""Compare local K2Bi journal freshness against direct VPS evidence.

This is an explicit operator pre-check. Continuous safety monitoring remains
owned by the always-on VPS alert and heartbeat stack.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, NamedTuple


DEFAULT_THRESHOLD_SECONDS = 900
DEFAULT_VAULT_ROOT = Path.home() / "Projects" / "K2Bi-Vault"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SSH_SCRIPT = REPO_ROOT / "scripts" / "ssh-vps.sh"
JOURNAL_DIR = "raw/journal"
CLI_COMMANDS = ("session-start", "step0")
REMOTE_JSON_MARKER_PREFIX = "K2BI_JOURNAL_SYNC_JSON_"
SSH_OUTPUT_DETAIL_LIMIT = 1000


REMOTE_JOURNAL_PY = r'''
import datetime as dt
import json
from pathlib import Path

MARKER = "__K2BI_REMOTE_MARKER__"
ROOT = Path("/home/k2bi/Projects/K2Bi-Vault")
JOURNAL_DIR = "raw/journal"
MAX_TAIL_BYTES = 1024 * 1024


def parse_ts(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


now = dt.datetime.now(dt.timezone.utc)
dates = [(now.date() - dt.timedelta(days=1)).isoformat(), now.date().isoformat()]
latest = None
for date_str in dates:
    path = ROOT / JOURNAL_DIR / f"{date_str}.jsonl"
    if not path.exists():
        continue
    try:
        with open(path, "rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - MAX_TAIL_BYTES)
            handle.seek(start)
            if start > 0:
                handle.readline()
            rows = handle.read().splitlines()
    except OSError:
        continue
    for line in rows:
        try:
            raw = line.decode("utf-8")
        except UnicodeDecodeError:
            continue
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        event_ts = parse_ts(record.get("ts"))
        entry_id = record.get("journal_entry_id")
        if event_ts is None or not isinstance(entry_id, str) or not entry_id:
            continue
        candidate = {
            "ts": event_ts,
            "journal_entry_id": entry_id,
            "source_file": str(path),
        }
        if latest is None or event_ts >= latest["ts"]:
            latest = candidate

payload = {"vps_now": now.isoformat()}
if latest is None:
    payload.update({
        "latest_event_ts": None,
        "latest_journal_entry_id": None,
        "source_file": None,
    })
else:
    payload.update({
        "latest_event_ts": latest["ts"].isoformat(),
        "latest_journal_entry_id": latest["journal_entry_id"],
        "source_file": latest["source_file"],
    })
PAYLOAD_JSON = json.dumps(payload, sort_keys=True, separators=(",", ":"))
print(f"{MARKER} {len(PAYLOAD_JSON)} {PAYLOAD_JSON}")
'''


class RemoteEvidenceError(RuntimeError):
    """Raised when direct VPS journal evidence cannot be collected."""


class LocalJournalReadError(RuntimeError):
    """Raised when local journal evidence cannot be read safely."""


class JournalEvent(NamedTuple):
    ts: datetime
    journal_entry_id: str
    source_file: str


class RemoteJournalSnapshot(NamedTuple):
    vps_now: datetime
    latest_event_ts: datetime | None
    latest_journal_entry_id: str | None
    source_file: str | None


class JournalSyncResult(NamedTuple):
    status: str
    classification: str
    threshold_seconds: int
    local_latest: JournalEvent | None
    remote: RemoteJournalSnapshot | None
    local_lag_seconds: int | None
    vps_age_seconds: int | None
    error: str | None = None


class PrecheckResult(NamedTuple):
    exit_code: int
    output: str
    result: JournalSyncResult


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _journal_dates_for_window(now_utc: datetime) -> list[str]:
    today = now_utc.astimezone(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    return [yesterday.isoformat(), today.isoformat()]


def _lock_path_for(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _acquire_shared_lock(path: Path) -> int | None:
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        lock_fd = os.open(str(_lock_path_for(path)), flags, 0o644)
    except OSError as exc:
        raise LocalJournalReadError(f"could not open local journal lock for {path.name}: {exc}") from exc
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
    except OSError as exc:
        os.close(lock_fd)
        raise LocalJournalReadError(f"could not lock local journal file {path.name}: {exc}") from exc
    return lock_fd


def _release_lock(lock_fd: int | None) -> None:
    if lock_fd is None:
        return
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


def _event_from_record(record: dict[str, Any], source_file: Path) -> JournalEvent | None:
    raw_ts = record.get("ts")
    raw_entry_id = record.get("journal_entry_id")
    if not isinstance(raw_ts, str) or not isinstance(raw_entry_id, str):
        return None
    if not raw_entry_id:
        return None
    try:
        event_ts = parse_utc(raw_ts)
    except ValueError:
        return None
    return JournalEvent(
        ts=event_ts,
        journal_entry_id=raw_entry_id,
        source_file=str(source_file),
    )


def read_latest_local_event(vault_root: Path, now_utc: datetime) -> JournalEvent | None:
    latest: JournalEvent | None = None
    for date_str in _journal_dates_for_window(now_utc):
        path = vault_root / JOURNAL_DIR / f"{date_str}.jsonl"
        if not path.exists():
            continue
        lock_fd = _acquire_shared_lock(path)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                for raw in handle:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    event = _event_from_record(record, path)
                    if event is None:
                        continue
                    if latest is None or event.ts >= latest.ts:
                        latest = event
        except OSError as exc:
            raise LocalJournalReadError(f"could not read local journal file {path.name}: {exc}") from exc
        finally:
            _release_lock(lock_fd)
    return latest


def _remote_marker() -> str:
    return f"{REMOTE_JSON_MARKER_PREFIX}{secrets.token_hex(16)}"


def _remote_journal_script(marker: str) -> str:
    return REMOTE_JOURNAL_PY.replace("__K2BI_REMOTE_MARKER__", marker)


def _parse_remote_payload(stdout: str, marker: str) -> RemoteJournalSnapshot:
    marked_frames = [
        line[len(marker) + 1:]
        for line in stdout.splitlines()
        if line.startswith(f"{marker} ")
    ]
    if not marked_frames:
        raise RemoteEvidenceError("ssh returned no marked evidence payload")
    if len(marked_frames) != 1:
        raise RemoteEvidenceError("ssh returned multiple evidence payloads")
    length_text, separator, payload_text = marked_frames[0].partition(" ")
    if not separator or not length_text.isdecimal():
        raise RemoteEvidenceError("ssh returned malformed evidence frame")
    expected_length = int(length_text)
    actual_length = len(payload_text)
    if actual_length != expected_length:
        raise RemoteEvidenceError(
            f"ssh evidence payload length mismatch: expected={expected_length} actual={actual_length}"
        )
    try:
        payload = json.loads(payload_text)
    except json.JSONDecodeError as exc:
        raise RemoteEvidenceError(f"ssh returned invalid evidence JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise RemoteEvidenceError("ssh evidence payload is not an object")
    try:
        vps_now = parse_utc(str(payload["vps_now"]))
    except (KeyError, ValueError) as exc:
        raise RemoteEvidenceError("ssh evidence payload lacks a valid vps_now") from exc
    raw_latest_ts = payload.get("latest_event_ts")
    latest_ts: datetime | None
    if raw_latest_ts is None:
        latest_ts = None
    elif isinstance(raw_latest_ts, str):
        try:
            latest_ts = parse_utc(raw_latest_ts)
        except ValueError as exc:
            raise RemoteEvidenceError("ssh evidence payload has invalid latest_event_ts") from exc
    else:
        raise RemoteEvidenceError("ssh evidence payload has invalid latest_event_ts")

    raw_entry_id = payload.get("latest_journal_entry_id")
    entry_id = raw_entry_id if isinstance(raw_entry_id, str) and raw_entry_id else None
    raw_source = payload.get("source_file")
    source_file = raw_source if isinstance(raw_source, str) and raw_source else None
    if latest_ts is not None and entry_id is None:
        raise RemoteEvidenceError("ssh evidence payload lacks latest_journal_entry_id")
    return RemoteJournalSnapshot(
        vps_now=vps_now,
        latest_event_ts=latest_ts,
        latest_journal_entry_id=entry_id,
        source_file=source_file,
    )


def _truncate_stream(value: str | None) -> str:
    if not value:
        return "<empty>"
    text = value.strip().replace("\r", "\\r").replace("\n", "\\n")
    if not text:
        return "<empty>"
    if len(text) <= SSH_OUTPUT_DETAIL_LIMIT:
        return text
    return f"{text[:SSH_OUTPUT_DETAIL_LIMIT]}...<truncated>"


def fetch_remote_journal_snapshot(
    ssh_script: Path | None = None,
    timeout_seconds: int = 90,
) -> RemoteJournalSnapshot:
    if ssh_script is None:
        ssh_script = DEFAULT_SSH_SCRIPT
    marker = _remote_marker()
    remote_script = _remote_journal_script(marker)
    # ssh-vps.sh delegates SSH policy to ssh-vps-transport.sh. The inline
    # Python keeps P4-2 MacBook-side and avoids adding a VPS helper file.
    try:
        completed = subprocess.run(
            [str(ssh_script), "python3 -"],
            check=False,
            capture_output=True,
            input=remote_script,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RemoteEvidenceError("ssh timed out") from exc
    if completed.returncode == 0:
        return _parse_remote_payload(completed.stdout, marker)

    raise RemoteEvidenceError(
        f"ssh exited {completed.returncode}: "
        f"stderr={_truncate_stream(completed.stderr)} "
        f"stdout={_truncate_stream(completed.stdout)}"
    )


def _seconds_between(later: datetime, earlier: datetime) -> int:
    return int((later - earlier).total_seconds())


def check_journal_sync(
    vault_root: Path,
    now_utc: datetime,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
    fetch_remote: Callable[[], RemoteJournalSnapshot] = fetch_remote_journal_snapshot,
) -> JournalSyncResult:
    try:
        local_latest = read_latest_local_event(vault_root, now_utc)
    except LocalJournalReadError as exc:
        return JournalSyncResult(
            status="FAIL",
            classification="local_journal_unreadable",
            threshold_seconds=threshold_seconds,
            local_latest=None,
            remote=None,
            local_lag_seconds=None,
            vps_age_seconds=None,
            error=str(exc),
        )
    try:
        remote = fetch_remote()
    except RemoteEvidenceError as exc:
        return JournalSyncResult(
            status="FAIL",
            classification="ssh_evidence_collection_failed",
            threshold_seconds=threshold_seconds,
            local_latest=local_latest,
            remote=None,
            local_lag_seconds=None,
            vps_age_seconds=None,
            error=str(exc),
        )

    if remote.latest_event_ts is None:
        return JournalSyncResult(
            status="FAIL",
            classification="vps_journaling_stopped",
            threshold_seconds=threshold_seconds,
            local_latest=local_latest,
            remote=remote,
            local_lag_seconds=None,
            vps_age_seconds=None,
            error="ssh succeeded but no parseable VPS journal event was found",
        )

    vps_age_seconds = max(0, _seconds_between(remote.vps_now, remote.latest_event_ts))
    if vps_age_seconds > threshold_seconds:
        return JournalSyncResult(
            status="FAIL",
            classification="vps_journaling_stopped",
            threshold_seconds=threshold_seconds,
            local_latest=local_latest,
            remote=remote,
            local_lag_seconds=None,
            vps_age_seconds=vps_age_seconds,
            error=None,
        )

    if local_latest is None:
        return JournalSyncResult(
            status="WARN",
            classification="syncthing_lag",
            threshold_seconds=threshold_seconds,
            local_latest=None,
            remote=remote,
            local_lag_seconds=None,
            vps_age_seconds=vps_age_seconds,
            error="local journal has no parseable event in the checked window",
        )

    remote_epoch = remote.latest_event_ts.timestamp()
    local_epoch = local_latest.ts.timestamp()
    local_lag_seconds = max(0, int(remote_epoch - local_epoch))
    tied_timestamp_mismatch = (
        abs(remote_epoch - local_epoch) < 0.000001
        and remote.latest_journal_entry_id != local_latest.journal_entry_id
    )
    if local_lag_seconds > threshold_seconds or tied_timestamp_mismatch:
        return JournalSyncResult(
            status="WARN",
            classification="syncthing_lag",
            threshold_seconds=threshold_seconds,
            local_latest=local_latest,
            remote=remote,
            local_lag_seconds=local_lag_seconds,
            vps_age_seconds=vps_age_seconds,
            error=None,
        )

    return JournalSyncResult(
        status="PASS",
        classification="local_journal_current",
        threshold_seconds=threshold_seconds,
        local_latest=local_latest,
        remote=remote,
        local_lag_seconds=local_lag_seconds,
        vps_age_seconds=vps_age_seconds,
        error=None,
    )


def _display(value: Any) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def format_result(result: JournalSyncResult) -> str:
    local = result.local_latest
    remote = result.remote
    lines = [
        (
            "K2Bi journal sync pre-check: "
            f"status={result.status} "
            f"classification={result.classification} "
            f"threshold_seconds={result.threshold_seconds} "
            f"local_lag_seconds={_display(result.local_lag_seconds)} "
            f"vps_age_seconds={_display(result.vps_age_seconds)}"
        ),
        (
            "local "
            f"local_latest_ts={_display(local.ts if local else None)} "
            f"local_journal_entry_id={_display(local.journal_entry_id if local else None)} "
            f"local_source={_display(local.source_file if local else None)}"
        ),
    ]
    if remote is not None:
        lines.append(
            "vps "
            f"vps_now={_display(remote.vps_now)} "
            f"vps_latest_ts={_display(remote.latest_event_ts)} "
            f"vps_journal_entry_id={_display(remote.latest_journal_entry_id)} "
            f"vps_source={_display(remote.source_file)}"
        )
    else:
        lines.append("vps vps_now=unknown vps_latest_ts=unknown vps_journal_entry_id=unknown vps_source=unknown")
    if result.error:
        lines.append(f"detail={result.error}")
    return "\n".join(lines)


def _exit_code_for_status(status: str) -> int:
    if status == "PASS":
        return 0
    if status == "WARN":
        return 1
    return 2


def run_precheck(
    mode: str,
    vault_root: Path,
    threshold_seconds: int,
    now_utc: datetime,
    fetch_remote: Callable[[], RemoteJournalSnapshot] = fetch_remote_journal_snapshot,
) -> PrecheckResult:
    result = check_journal_sync(
        vault_root=vault_root,
        now_utc=now_utc,
        threshold_seconds=threshold_seconds,
        fetch_remote=fetch_remote,
    )
    output = format_result(result)
    if mode == "session-start":
        exit_code = 2 if result.status == "FAIL" else 0
    else:
        exit_code = _exit_code_for_status(result.status)
    return PrecheckResult(
        exit_code=exit_code,
        output=output,
        result=result,
    )


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--vault-root",
        default=os.environ.get("K2BI_VAULT_ROOT", str(DEFAULT_VAULT_ROOT)),
        help="K2Bi vault root",
    )
    parser.add_argument(
        "--threshold-seconds",
        type=int,
        default=DEFAULT_THRESHOLD_SECONDS,
        help="Maximum accepted local lag in seconds",
    )
    parser.add_argument(
        "--now-utc",
        default=None,
        help="Fixed UTC timestamp for tests",
    )
    parser.add_argument(
        "--ssh-script",
        default=str(DEFAULT_SSH_SCRIPT),
        help="Path to scripts/ssh-vps.sh",
    )
    parser.add_argument(
        "--ssh-timeout-seconds",
        type=int,
        default=90,
        help="SSH evidence collection timeout in seconds",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare local K2Bi journal freshness against direct VPS evidence.",
        epilog=(
            "Manual Step 0 pre-check required for deploy, closeout, and evidence-collection sessions: "
            "run `journal-sync-lag-detector.py step0` before trusting local journal evidence."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in CLI_COMMANDS:
        subparser = subparsers.add_parser(command)
        _add_common_args(subparser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    now_utc = parse_utc(args.now_utc) if args.now_utc else datetime.now(timezone.utc)
    vault_root = Path(args.vault_root).expanduser()
    ssh_script = Path(args.ssh_script).expanduser()

    def fetch_remote() -> RemoteJournalSnapshot:
        return fetch_remote_journal_snapshot(
            ssh_script=ssh_script,
            timeout_seconds=args.ssh_timeout_seconds,
        )

    precheck = run_precheck(
        mode=args.command,
        vault_root=vault_root,
        threshold_seconds=args.threshold_seconds,
        now_utc=now_utc,
        fetch_remote=fetch_remote,
    )
    print(precheck.output)
    return precheck.exit_code


if __name__ == "__main__":
    sys.exit(main())
