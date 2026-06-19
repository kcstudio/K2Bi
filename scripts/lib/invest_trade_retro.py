"""Stage 15 trade retro generation.

This module is intentionally broker-free. It reads K2Bi's append-only journal
files and writes vault/memory artifacts only.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import random
import re
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Iterator

from scripts.lib.strategy_frontmatter import atomic_write_bytes


MAX_JOURNAL_FILE_BYTES = 100 * 1024 * 1024
MAX_JOURNAL_FILE_LINES = 1_000_000
_LOCK_RANDOM = random.Random(0)


class TradeRetroError(RuntimeError):
    """Raised when a trade retro cannot be generated."""


@dataclass(frozen=True)
class JournalEvent:
    record: dict[str, Any]
    path: Path
    index: int
    line_no: int


def _parse_iso_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decimal(raw: Any) -> Decimal | None:
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _price_text(raw: Any, label: str) -> str | None:
    if raw is None:
        return None
    value = _decimal(raw)
    if value is None or not value.is_finite():
        raise TradeRetroError(f"invalid {label} price")
    return str(raw)


def _money(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _pct(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip())
    slug = slug.strip("-")
    if not slug:
        raise TradeRetroError("strategy id produced an empty slug")
    return slug


def _read_journal_events(journal_dir: Path) -> tuple[list[JournalEvent], list[Path]]:
    if not journal_dir.exists():
        raise TradeRetroError(f"journal dir does not exist: {journal_dir}")
    events: list[JournalEvent] = []
    counter = 0
    journal_paths = sorted(journal_dir.glob("*.jsonl"))
    for path in journal_paths:
        try:
            size = path.stat().st_size
            if size > MAX_JOURNAL_FILE_BYTES:
                raise TradeRetroError(
                    f"journal file too large: {path} ({size} bytes)"
                )
            with path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(handle, start=1):
                    if line_no > MAX_JOURNAL_FILE_LINES:
                        raise TradeRetroError(
                            f"journal file has too many lines: {path}"
                        )
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        print(
                            f"warning: skipping malformed journal JSON at "
                            f"{path}:{line_no}: {exc}",
                            file=sys.stderr,
                        )
                        continue
                    if not isinstance(record, dict):
                        print(
                            f"warning: skipping non-object journal JSON at {path}:{line_no}",
                            file=sys.stderr,
                        )
                        continue
                    events.append(
                        JournalEvent(
                            record=record,
                            path=path,
                            index=counter,
                            line_no=line_no,
                        )
                    )
                    counter += 1
        except OSError as exc:
            raise TradeRetroError(f"cannot read journal file {path}: {exc}") from exc
    return events, journal_paths


def _record_strategy(record: dict[str, Any]) -> str:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return str(payload.get("strategy_id") or record.get("strategy") or "")
    return str(record.get("strategy") or "")


def _closure_timestamp(event: JournalEvent) -> datetime | None:
    payload = event.record.get("payload")
    stopped_out_at = payload.get("stopped_out_at") if isinstance(payload, dict) else None
    raw = stopped_out_at or event.record.get("ts")
    return _parse_iso_datetime(str(raw)) if raw else None


def _entry_timestamp(event: JournalEvent) -> datetime | None:
    payload = event.record.get("payload")
    filled_at = payload.get("filled_at") if isinstance(payload, dict) else None
    raw = filled_at or event.record.get("ts")
    return _parse_iso_datetime(str(raw)) if raw else None


def _find_closure(strategy_id: str, events: list[JournalEvent]) -> JournalEvent:
    closures = [
        event
        for event in events
        if event.record.get("event_type") == "strategy_stopped_out"
        and _record_strategy(event.record) == strategy_id
    ]
    if not closures:
        raise TradeRetroError(f"no closed trade found for strategy {strategy_id}")
    fallback = datetime.min.replace(tzinfo=timezone.utc)
    return max(
        closures,
        key=lambda event: (
            _closure_timestamp(event) is not None,
            _closure_timestamp(event) or fallback,
            event.index,
        ),
    )


def _event_ticker(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("ticker"):
        return str(payload["ticker"]).upper()
    ticker = record.get("ticker")
    if ticker:
        return str(ticker).upper()
    return None


def _find_entry(strategy_id: str, closure: JournalEvent, events: list[JournalEvent]) -> JournalEvent | None:
    closure_ticker = _event_ticker(closure.record)
    closure_time = _closure_timestamp(closure)
    fallback = datetime.min.replace(tzinfo=timezone.utc)
    candidates: list[JournalEvent] = []
    for event in events:
        if event.record.get("event_type") != "order_filled":
            continue
        if _record_strategy(event.record) != strategy_id:
            continue
        payload = event.record.get("payload")
        payload_side = payload.get("side") if isinstance(payload, dict) else None
        side = str(payload_side or event.record.get("side") or "").lower()
        if side != "buy":
            continue
        if closure_ticker and _event_ticker(event.record) != closure_ticker:
            continue
        entry_time = _entry_timestamp(event)
        if closure_time is not None:
            if entry_time is None or entry_time >= closure_time:
                continue
        elif event.index >= closure.index:
            continue
        candidates.append(event)
    return max(
        candidates,
        key=lambda event: (_entry_timestamp(event) or fallback, event.index),
    ) if candidates else None


def _rel_to_vault(vault_root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(vault_root))
    except ValueError:
        return str(path)


def _quantities_conflict(record_qty: Any, payload_qty: Any) -> bool:
    if record_qty is None or payload_qty is None:
        return False
    record_decimal = _decimal(record_qty)
    payload_decimal = _decimal(payload_qty)
    if record_decimal is None and payload_decimal is None:
        return str(record_qty) != str(payload_qty)
    if record_decimal is None or payload_decimal is None:
        return True
    return record_decimal != payload_decimal


def _entry_payload(entry: JournalEvent | None) -> dict[str, Any]:
    if entry is None:
        return {
            "found": False,
            "journal_entry_id": None,
            "filled_at": None,
            "side": None,
            "qty": None,
            "price": None,
            "trade_id": None,
        }
    record = entry.record
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    price = _price_text(payload.get("fill_price"), "entry")
    record_qty = record.get("qty")
    payload_qty = payload.get("fill_qty")
    if _quantities_conflict(record_qty, payload_qty):
        raise TradeRetroError("conflicting entry quantity between journal record and payload")
    qty = record_qty if record_qty is not None else payload_qty
    if qty is not None and _decimal(qty) is None:
        raise TradeRetroError("invalid entry quantity")
    return {
        "found": True,
        "journal_entry_id": record.get("journal_entry_id"),
        "filled_at": payload.get("filled_at") or record.get("ts"),
        "side": payload.get("side") or record.get("side"),
        "qty": qty,
        "price": price,
        "trade_id": record.get("trade_id"),
    }


def _exit_payload(closure: JournalEvent) -> dict[str, Any]:
    payload = closure.record.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    price = _price_text(payload.get("fill_price"), "exit")
    return {
        "closed_at": payload.get("stopped_out_at") or closure.record.get("ts"),
        "exit_type": "stopped_out",
        "price": price,
        "fill_perm_id": payload.get("fill_perm_id"),
    }


def _holding_days(entry: dict[str, Any], exit_: dict[str, Any]) -> int | None:
    start = _parse_iso_datetime(str(entry.get("filled_at") or ""))
    end = _parse_iso_datetime(str(exit_.get("closed_at") or ""))
    if start is None or end is None:
        return None
    return max(0, (end.date() - start.date()).days)


def _outcome(entry: dict[str, Any], exit_: dict[str, Any]) -> dict[str, Any]:
    if not entry.get("found"):
        return {
            "realized_pnl_usd": None,
            "return_pct": None,
            "holding_days": None,
        }
    entry_price = _decimal(entry.get("price"))
    exit_price = _decimal(exit_.get("price"))
    qty = _decimal(entry.get("qty"))
    if entry_price is None or exit_price is None or qty is None or entry_price == 0:
        return {
            "realized_pnl_usd": None,
            "return_pct": None,
            "holding_days": _holding_days(entry, exit_),
        }
    pnl = (exit_price - entry_price) * qty
    return_pct = ((exit_price - entry_price) / entry_price) * Decimal("100")
    return {
        "realized_pnl_usd": _money(pnl),
        "return_pct": _pct(return_pct),
        "holding_days": _holding_days(entry, exit_),
    }


def _change_and_rule(entry: dict[str, Any], exit_: dict[str, Any]) -> tuple[dict[str, str], str]:
    if not entry.get("found"):
        change = {
            "target": "journal",
            "change": (
                "Add complete entry-fill linkage before using trade retros "
                "for P&L (profit and loss) judgment."
            ),
            "rationale": "The close event had no prior matching entry fill in the journal scan.",
            "priority": "medium",
        }
        rule = (
            "After a closed trade, confirm entry-fill linkage before using "
            "the retro for profit and loss judgment."
        )
        return change, rule

    entry_price = _decimal(entry.get("price"))
    exit_price = _decimal(exit_.get("price"))
    if entry_price is not None and exit_price is not None and exit_price < entry_price:
        change = {
            "target": "strategy",
            "change": "Review entry timing and stop distance before re-approval.",
            "rationale": "The stopped-out exit closed below the recorded entry fill.",
            "priority": "medium",
        }
        rule = (
            "After a stopped-out trade, review entry timing and stop distance "
            "before re-approving the same strategy."
        )
        return change, rule

    change = {
        "target": "strategy",
        "change": "Document whether the stop was loss-protection or profit-protection before re-approval.",
        "rationale": "The stopped-out exit did not close below the recorded entry fill.",
        "priority": "medium",
    }
    rule = (
        "After a profitable stopped-out trade, document whether the stop was "
        "loss-protection or profit-protection before re-approval."
    )
    return change, rule


def _next_learning_id(text: str, as_of: date) -> str:
    prefix = f"L-{as_of.isoformat()}-"
    max_n = 0
    for match in re.finditer(r"###\s+L-(\d{4}-\d{2}-\d{2})-(\d{3})", text):
        if match.group(1) == as_of.isoformat():
            max_n = max(max_n, int(match.group(2)))
    return f"{prefix}{max_n + 1:03d}"


def _existing_learning_id(text: str, closure_id: str) -> str | None:
    source = f"**Source:** stage15-retro `{closure_id}`"
    source_index = text.find(source)
    if source_index == -1:
        return None
    before = text[:source_index]
    matches = list(re.finditer(r"###\s+(L-\d{4}-\d{2}-\d{2}-\d{3})", before))
    return matches[-1].group(1) if matches else None


def _learning_id_exists(text: str, learning_id: str) -> bool:
    pattern = rf"###\s+{re.escape(learning_id)}(?:\s|$)"
    return re.search(pattern, text) is not None


def _learning_path(vault_root: Path) -> Path:
    return vault_root / "System" / "memory" / "self_improve_learnings.md"


def _pending_learning_path(vault_root: Path, closure_id: str) -> Path:
    return (
        vault_root
        / "System"
        / "memory"
        / f".stage15_pending_{_safe_slug(closure_id)}.json"
    )


def _learning_text(vault_root: Path) -> str:
    path = _learning_path(vault_root)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


@contextmanager
def _artifact_lock(vault_root: Path, timeout_s: float = 30.0) -> Iterator[None]:
    path = _learning_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One local POSIX flock covers the full Stage 15 retro transaction:
    # journal scan, retro reuse/repair, learning append, and retro write.
    lock_path = path.parent / ".stage15_trade_retro.lock"
    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    try:
        parent_fd = os.open(lock_path.parent, parent_flags)
    except OSError as exc:
        raise TradeRetroError(f"cannot open trade retro lock parent {lock_path.parent}: {exc}") from exc
    parent_mode = stat.S_IMODE(os.fstat(parent_fd).st_mode)
    if parent_mode & 0o002:
        os.close(parent_fd)
        raise TradeRetroError(f"trade retro lock parent is world-writable: {lock_path.parent}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(lock_path.name, flags | os.O_EXCL, 0o600, dir_fd=parent_fd)
    except FileExistsError:
        read_flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            read_flags |= os.O_NOFOLLOW
        try:
            fd = os.open(lock_path.name, read_flags, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            os.close(parent_fd)
            raise TradeRetroError(f"cannot open trade retro lock {lock_path}: {exc}") from exc
    except FileNotFoundError:
        fd = os.open(lock_path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
    except OSError as exc:
        os.close(parent_fd)
        raise TradeRetroError(f"cannot open trade retro lock {lock_path}: {exc}") from exc
    os.close(parent_fd)
    lock_stat = os.fstat(fd)
    if not stat.S_ISREG(lock_stat.st_mode):
        os.close(fd)
        raise TradeRetroError(f"trade retro lock path is not a regular file: {lock_path}")
    if getattr(lock_stat, "st_nlink", 1) != 1:
        os.close(fd)
        raise TradeRetroError(f"trade retro lock path has unexpected hard links: {lock_path}")
    os.fchmod(fd, 0o600)
    deadline = time.monotonic() + timeout_s
    delay_s = 0.05
    attempts = 0
    with os.fdopen(fd, "r+", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError as exc:
                attempts += 1
                if time.monotonic() >= deadline:
                    raise TradeRetroError(
                        f"timed out waiting for trade retro learning lock: {lock_path}"
                    ) from exc
                if attempts in {1, 5, 10} or attempts > 10:
                    elapsed = timeout_s - max(0.0, deadline - time.monotonic())
                    print(
                        "warning: waiting for trade retro learning lock "
                        f"path={lock_path} attempts={attempts} elapsed_s={elapsed:.2f}",
                        file=sys.stderr,
                    )
                time.sleep(delay_s + _LOCK_RANDOM.uniform(0, delay_s / 4))
                delay_s = min(delay_s * 2, 1.0)
        try:
            lock.seek(0)
            lock.truncate(0)
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _learning_block(
    *,
    as_of: date,
    strategy_id: str,
    closure_id: str,
    learning_id: str,
    distilled_rule: str,
    change: dict[str, str],
) -> str:
    return (
        f"\n## {as_of.isoformat()} -- Trade retro: {strategy_id} stopped out\n\n"
        f"### {learning_id}\n"
        f"distilled-rule: {json.dumps(distilled_rule)}\n\n"
        "- **Area:** workflow\n"
        f"- **Distilled rule:** {distilled_rule}\n"
        f"- **Learning:** {change['change']} {change['rationale']}\n"
        f"- **Context:** Stage 15 trade retro for `{strategy_id}`; "
        f"source closure journal entry `{closure_id}`.\n"
        f"- **Source:** stage15-retro `{closure_id}`\n"
        "- **Reinforced:** 1\n"
        "- **Confidence:** low\n"
        f"- **Date:** {as_of.isoformat()}\n"
        "- **Status:** pending\n"
    )


def _plan_learning_locked(
    *,
    vault_root: Path,
    as_of: date,
    strategy_id: str,
    closure_id: str,
    distilled_rule: str,
    change: dict[str, str],
    preferred_learning_id: str | None = None,
) -> tuple[str, str | None]:
    path = _learning_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = _learning_text(vault_root)
    existing = _existing_learning_id(text, closure_id)
    if existing:
        return existing, None
    if preferred_learning_id and _learning_id_exists(text, preferred_learning_id):
        return preferred_learning_id, None
    learning_id = preferred_learning_id or _next_learning_id(text, as_of)
    block = _learning_block(
        as_of=as_of,
        strategy_id=strategy_id,
        closure_id=closure_id,
        learning_id=learning_id,
        distilled_rule=distilled_rule,
        change=change,
    )
    return learning_id, text + block


def _write_learning_text(vault_root: Path, text: str) -> None:
    path = _learning_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, text.encode("utf-8"))


def _verify_learning_written(
    vault_root: Path,
    closure_id: str,
    learning_id: str,
) -> None:
    actual = _existing_learning_id(_learning_text(vault_root), closure_id)
    if actual != learning_id:
        raise TradeRetroError(
            f"learning write verification failed for {closure_id}: expected {learning_id}"
        )


def _write_pending_learning_marker(
    *,
    vault_root: Path,
    closure_id: str,
    learning_id: str,
    retro_path: str,
) -> Path:
    path = _pending_learning_path(vault_root, closure_id)
    payload = {
        "closure_journal_entry_id": closure_id,
        "learning_id": learning_id,
        "retro_path": retro_path,
        "status": "pending_learning_write",
    }
    atomic_write_bytes(path, json.dumps(payload, sort_keys=True).encode("utf-8"))
    return path


def _clear_pending_learning_marker(vault_root: Path, closure_id: str) -> None:
    try:
        _pending_learning_path(vault_root, closure_id).unlink()
    except FileNotFoundError:
        return


def _ensure_learning_locked(
    *,
    vault_root: Path,
    as_of: date,
    strategy_id: str,
    closure_id: str,
    distilled_rule: str,
    change: dict[str, str],
) -> str:
    learning_id, new_text = _plan_learning_locked(
        vault_root=vault_root,
        as_of=as_of,
        strategy_id=strategy_id,
        closure_id=closure_id,
        distilled_rule=distilled_rule,
        change=change,
    )
    if new_text is not None:
        _write_learning_text(vault_root, new_text)
    return learning_id


def _ensure_learning(
    *,
    vault_root: Path,
    as_of: date,
    strategy_id: str,
    closure_id: str,
    distilled_rule: str,
    change: dict[str, str],
) -> str:
    with _artifact_lock(vault_root):
        return _ensure_learning_locked(
            vault_root=vault_root,
            as_of=as_of,
            strategy_id=strategy_id,
            closure_id=closure_id,
            distilled_rule=distilled_rule,
            change=change,
        )


def _build_markdown(retro: dict[str, Any]) -> str:
    body_json = json.dumps(retro, indent=2, sort_keys=True)
    changes = "\n".join(
        f"- **{item['target']} ({item['priority']}):** {item['change']} {item['rationale']}"
        for item in retro["concrete_changes"]
    )
    outcome = retro["outcome"]
    return (
        "---\n"
        "tags: [insight, retro, trade-retro, k2bi]\n"
        f"date: {retro['retro_date']}\n"
        "type: insight\n"
        "origin: k2bi-generate\n"
        'up: "[[../planning/index]]"\n'
        f"retro_version: {retro['retro_version']}\n"
        f"strategy_id: {retro['strategy_id']}\n"
        f"ticker: {retro['ticker']}\n"
        f"closure_journal_entry_id: {retro['source']['closure_journal_entry_id']}\n"
        f"learn_id: {retro['learn']['learning_id']}\n"
        "---\n\n"
        f"# Trade Retro: {retro['strategy_id']}\n\n"
        "## Outcome\n\n"
        f"- Ticker: `{retro['ticker']}`\n"
        f"- Exit type: `{retro['exit']['exit_type']}`\n"
        f"- Realized P&L (profit and loss): `{outcome['realized_pnl_usd']}`\n"
        f"- Return: `{outcome['return_pct']}` percent\n"
        f"- Holding days: `{outcome['holding_days']}`\n\n"
        "## Concrete Changes\n\n"
        f"{changes}\n\n"
        "## Learned\n\n"
        f"- {retro['learn']['distilled_rule']}\n"
        f"- Learning id: `{retro['learn']['learning_id']}`\n\n"
        "## Structured Retro\n\n"
        "```json\n"
        f"{body_json}\n"
        "```\n"
    )


def _retro_rel_path(as_of: date, strategy_id: str) -> Path:
    return (
        Path("wiki")
        / "insights"
        / f"{as_of.isoformat()}_trade-retro_{_safe_slug(strategy_id)}.md"
    )


def _write_retro(vault_root: Path, retro: dict[str, Any]) -> str:
    _validate_retro_schema(retro)
    rel_path = Path(str(retro["retro_path"]))
    out_path = _resolve_retro_output_path(vault_root, rel_path)
    # atomic_write_bytes writes a temp file in the validated directory and
    # os.replace() swaps the final path. POSIX replace does not follow a final
    # path symlink; it replaces the link itself.
    atomic_write_bytes(out_path, _build_markdown(retro).encode("utf-8"))
    return str(rel_path)


def _resolve_retro_output_path(vault_root: Path, rel_path: Path) -> Path:
    if rel_path.is_absolute():
        raise TradeRetroError("retro path escapes wiki/insights")
    if vault_root.is_symlink():
        raise TradeRetroError("retro path escapes wiki/insights")
    vault_resolved = vault_root.resolve()
    parts = rel_path.parts
    if len(parts) != 3 or parts[0] != "wiki" or parts[1] != "insights":
        raise TradeRetroError("retro path escapes wiki/insights")
    filename = parts[2]
    if not filename or filename in {".", ".."}:
        raise TradeRetroError("retro path escapes wiki/insights")
    wiki_path = vault_root / "wiki"
    insights_path = vault_root / "wiki" / "insights"
    if wiki_path.is_symlink() or insights_path.is_symlink():
        raise TradeRetroError("retro path escapes wiki/insights")
    expected_insights = vault_resolved / "wiki" / "insights"
    out_path = expected_insights / filename
    if out_path.exists() and out_path.is_symlink():
        raise TradeRetroError("retro path escapes wiki/insights")
    return out_path


def _require_dict(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise TradeRetroError(f"retro schema invalid: {field} must be an object")
    return value


def _validate_price(value: Any, field: str) -> None:
    if value is not None and not isinstance(value, str):
        raise TradeRetroError(f"retro schema invalid: {field} must be string or null")


def _validate_retro_schema(retro: dict[str, Any]) -> None:
    required = {
        "retro_version",
        "retro_date",
        "strategy_id",
        "ticker",
        "source",
        "entry",
        "exit",
        "outcome",
        "concrete_changes",
        "learn",
        "retro_path",
    }
    missing = sorted(required - set(retro))
    if missing:
        raise TradeRetroError(f"retro schema invalid: missing {', '.join(missing)}")
    if not isinstance(retro["ticker"], str) or not retro["ticker"].strip():
        raise TradeRetroError("retro schema invalid: ticker is required")
    source = _require_dict(retro["source"], "source")
    entry = _require_dict(retro["entry"], "entry")
    exit_ = _require_dict(retro["exit"], "exit")
    learn = _require_dict(retro["learn"], "learn")
    if not isinstance(source.get("closure_journal_entry_id"), str) or not source["closure_journal_entry_id"]:
        raise TradeRetroError("retro schema invalid: closure_journal_entry_id is required")
    if "price" not in entry:
        raise TradeRetroError("retro schema invalid: entry.price is required")
    if "price" not in exit_:
        raise TradeRetroError("retro schema invalid: exit.price is required")
    _validate_price(entry.get("price"), "entry.price")
    _validate_price(exit_.get("price"), "exit.price")
    if not isinstance(learn.get("learning_id"), str) or not learn["learning_id"]:
        raise TradeRetroError("retro schema invalid: learn.learning_id is required")


def _load_retro_json(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(
        r"## Structured Retro\s+```json\s+(?P<body>.*?)\s+```",
        text,
        re.DOTALL,
    )
    if not match:
        return None
    try:
        parsed = json.loads(match.group("body"))
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _find_existing_retro(
    vault_root: Path,
    strategy_id: str,
    closure_id: str,
) -> dict[str, Any] | None:
    # Caller must hold _artifact_lock; retro discovery participates in the
    # idempotent retro+learning transaction.
    insights_dir = vault_root / "wiki" / "insights"
    if not insights_dir.exists():
        return None
    pattern = f"*_trade-retro_{_safe_slug(strategy_id)}.md"
    for path in sorted(insights_dir.glob(pattern)):
        retro = _load_retro_json(path)
        if retro is None:
            continue
        source = retro.get("source")
        if not isinstance(source, dict):
            continue
        if source.get("closure_journal_entry_id") != closure_id:
            continue
        retro["retro_path"] = _rel_to_vault(vault_root, path)
        return retro
    return None


def _retro_date_or_default(retro: dict[str, Any], default: date) -> date:
    raw = retro.get("retro_date")
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise TradeRetroError("malformed retro_date in existing retro") from exc
    raise TradeRetroError("malformed retro_date in existing retro")


def _learning_inputs_from_retro(
    retro: dict[str, Any],
    fallback_change: dict[str, str],
    fallback_rule: str,
) -> tuple[dict[str, str], str]:
    change = fallback_change
    changes = retro.get("concrete_changes")
    if isinstance(changes, list) and changes and isinstance(changes[0], dict):
        candidate = changes[0]
        if all(isinstance(candidate.get(key), str) for key in ("target", "change", "rationale", "priority")):
            change = {
                "target": candidate["target"],
                "change": candidate["change"],
                "rationale": candidate["rationale"],
                "priority": candidate["priority"],
            }

    rule = fallback_rule
    learn = retro.get("learn")
    if isinstance(learn, dict) and isinstance(learn.get("distilled_rule"), str):
        rule = learn["distilled_rule"]
    return change, rule


def _set_learning_fields(
    retro: dict[str, Any],
    *,
    learning_id: str,
    distilled_rule: str,
) -> None:
    learn = retro.get("learn")
    if not isinstance(learn, dict):
        learn = {}
        retro["learn"] = learn
    learn["learning_id"] = learning_id
    learn.setdefault("path", "System/memory/self_improve_learnings.md")
    learn.setdefault("distilled_rule", distilled_rule)
    learn.setdefault("policy_ledger_added", False)


def _retro_learning_id(retro: dict[str, Any]) -> str | None:
    learn = retro.get("learn")
    if isinstance(learn, dict) and isinstance(learn.get("learning_id"), str):
        value = learn["learning_id"].strip()
        return value or None
    return None


def run_retro(
    *,
    strategy_id: str,
    vault_root: str | Path,
    repo_root: str | Path,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Generate a Stage 15 retro for a closed strategy trade."""
    if not strategy_id.strip():
        raise TradeRetroError("strategy id is required")
    vault = Path(vault_root)
    repo = Path(repo_root)
    if not repo.exists():
        raise TradeRetroError(f"repo root does not exist: {repo}")
    run_date = as_of or date.today()

    with _artifact_lock(vault):
        journal_dir = vault / "raw" / "journal"
        events, journal_paths = _read_journal_events(journal_dir)
        closure = _find_closure(strategy_id, events)
        closure_record = closure.record
        closure_id = str(closure_record.get("journal_entry_id") or "")
        if not closure_id:
            raise TradeRetroError("closure event missing journal_entry_id")
        if _closure_timestamp(closure) is None:
            raise TradeRetroError("closed trade missing timestamp")

        ticker = _event_ticker(closure_record)
        if not ticker:
            raise TradeRetroError("closed trade missing ticker")

        existing_retro = _find_existing_retro(vault, strategy_id, closure_id)
        existing_learning = _existing_learning_id(_learning_text(vault), closure_id)
        if existing_retro is not None and existing_learning:
            learn = existing_retro.get("learn")
            previous_learning_id = (
                learn.get("learning_id") if isinstance(learn, dict) else None
            )
            distilled_rule = (
                learn.get("distilled_rule")
                if isinstance(learn, dict) and isinstance(learn.get("distilled_rule"), str)
                else "Existing Stage 15 trade retro learning."
            )
            _set_learning_fields(
                existing_retro,
                learning_id=existing_learning,
                distilled_rule=distilled_rule,
            )
            if previous_learning_id != existing_learning:
                _write_retro(vault, existing_retro)
            return existing_retro

        entry = _find_entry(strategy_id, closure, events)
        entry_data = _entry_payload(entry)
        exit_data = _exit_payload(closure)
        outcome = _outcome(entry_data, exit_data)
        change, distilled_rule = _change_and_rule(entry_data, exit_data)

        if existing_retro is not None:
            pending_path = _pending_learning_path(vault, closure_id)
            if pending_path.exists():
                print(
                    f"warning: repairing pending Stage 15 learning marker: {pending_path}",
                    file=sys.stderr,
                )
            repair_change, repair_rule = _learning_inputs_from_retro(
                existing_retro,
                fallback_change=change,
                fallback_rule=distilled_rule,
            )
            learning_id, new_learning_text = _plan_learning_locked(
                vault_root=vault,
                as_of=_retro_date_or_default(existing_retro, run_date),
                strategy_id=strategy_id,
                closure_id=closure_id,
                distilled_rule=repair_rule,
                change=repair_change,
                preferred_learning_id=_retro_learning_id(existing_retro),
            )
            previous_learning_id = _retro_learning_id(existing_retro)
            _set_learning_fields(
                existing_retro,
                learning_id=learning_id,
                distilled_rule=repair_rule,
            )
            _resolve_retro_output_path(vault, Path(str(existing_retro["retro_path"])))
            _validate_retro_schema(existing_retro)
            if new_learning_text is not None:
                _write_pending_learning_marker(
                    vault_root=vault,
                    closure_id=closure_id,
                    learning_id=learning_id,
                    retro_path=str(existing_retro["retro_path"]),
                )
                try:
                    if previous_learning_id != learning_id:
                        _write_retro(vault, existing_retro)
                except Exception:
                    _clear_pending_learning_marker(vault, closure_id)
                    raise
                _write_learning_text(vault, new_learning_text)
                _verify_learning_written(vault, closure_id, learning_id)
                _clear_pending_learning_marker(vault, closure_id)
            elif previous_learning_id != learning_id:
                _write_retro(vault, existing_retro)
            return existing_retro

        source: dict[str, Any] = {
            "closure_event_type": "strategy_stopped_out",
            "closure_journal_entry_id": closure_id,
            "journal_files": [_rel_to_vault(vault, path) for path in journal_paths],
            "closure_source_file": _rel_to_vault(vault, closure.path),
            "closure_source_line": closure.line_no,
            "closure_source_index": closure.index,
            "entry_source_file": _rel_to_vault(vault, entry.path) if entry else None,
            "entry_source_line": entry.line_no if entry else None,
            "entry_source_index": entry.index if entry else None,
        }
        retro_path = str(_retro_rel_path(run_date, strategy_id))
        _resolve_retro_output_path(vault, Path(retro_path))
        learning_id, new_learning_text = _plan_learning_locked(
            vault_root=vault,
            as_of=run_date,
            strategy_id=strategy_id,
            closure_id=closure_id,
            distilled_rule=distilled_rule,
            change=change,
        )
        retro_doc: dict[str, Any] = {
            "retro_version": 1,
            "retro_date": run_date.isoformat(),
            "strategy_id": strategy_id,
            "ticker": ticker,
            "source": source,
            "entry": entry_data,
            "exit": exit_data,
            "outcome": outcome,
            "concrete_changes": [change],
            "learn": {
                "learning_id": learning_id,
                "path": "System/memory/self_improve_learnings.md",
                "distilled_rule": distilled_rule,
                "policy_ledger_added": False,
            },
            "retro_path": retro_path,
        }
        _write_retro(vault, retro_doc)
        if new_learning_text is not None:
            _write_pending_learning_marker(
                vault_root=vault,
                closure_id=closure_id,
                learning_id=learning_id,
                retro_path=retro_path,
            )
            _write_learning_text(vault, new_learning_text)
            _verify_learning_written(vault, closure_id, learning_id)
            _clear_pending_learning_marker(vault, closure_id)
        return retro_doc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a Stage 15 trade retro")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="generate a retro for one strategy")
    run.add_argument("--strategy", required=True)
    run.add_argument("--vault-root", default=str(Path.home() / "Projects" / "K2Bi-Vault"))
    run.add_argument("--repo-root", default=str(Path.home() / "Projects" / "K2Bi"))
    run.add_argument("--as-of", default=None)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        as_of = date.fromisoformat(args.as_of) if args.as_of else None
        result = run_retro(
            strategy_id=args.strategy,
            vault_root=args.vault_root,
            repo_root=args.repo_root,
            as_of=as_of,
        )
    except (TradeRetroError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
