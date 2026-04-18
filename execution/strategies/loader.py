# cash-only invariant: no sell-side paths in this module (file I/O +
# YAML parsing). Runtime enforcement owned by runner.py, which imports
# execution.risk.cash_only.check_sell_covered.
"""Parse wiki/strategies/<name>.md into typed dataclasses.

File shape (architect Q2-refined):

    ---
    name: spy-rotational
    status: approved
    strategy_type: hand_crafted
    risk_envelope_pct: 0.01
    regime_filter:
      - risk_on
    approved_at: 2026-05-01T10:00:00Z
    approved_commit_sha: abc1234
    order:
      ticker: SPY
      side: buy
      qty: 10
      limit_price: 500.00
      stop_loss: 490.00
      time_in_force: DAY
    ---

    ## How This Works

    Plain-English explanation of the strategy (Teach Mode gate).

Post-approval drift: the loader computes sha256 of the whole file at
load time and stores it on the snapshot. The engine re-hashes on every
tick and compares; mismatch triggers strategy_file_modified_post_approval
without affecting runtime behavior (snapshot remains source of truth).

`load_approved()` is the canonical entry point used by the engine at
startup. `load_document()` is the lower-level function used by Bundle
3 (invest-propose-limits) + Bundle 4 (invest-bear-case) to read
proposed or retired strategies.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .types import (
    ALLOWED_STATUSES,
    ALLOWED_STRATEGY_TYPES,
    ApprovedStrategySnapshot,
    STATUS_APPROVED,
    STRATEGY_TYPE_HAND_CRAFTED,
    StrategyDocument,
    StrategyFileModifiedError,
    StrategyLoaderError,
    StrategyOrderSpec,
)


FRONTMATTER_DELIM = "---"


def load_document(path: Path) -> StrategyDocument:
    """Parse a strategy file into the full document dataclass.

    Does NOT require status == approved. Callers that need the
    runtime-only shape call load_approved() instead.
    """
    raw = _read_text(path)
    frontmatter, body = _split_frontmatter(raw, path)
    # Codex R17 P1: yaml.safe_load raises yaml.YAMLError on syntax
    # issues, which would bypass load_all_approved's
    # StrategyLoaderError handler and abort engine startup from a
    # malformed draft file. Convert to the contract's error class
    # so the "quiet skip drafts, fail loud on approved" discipline
    # holds for YAML syntax errors as well as structural ones.
    try:
        data = yaml.safe_load(frontmatter) or {}
    except yaml.YAMLError as exc:
        raise StrategyLoaderError(
            f"{path}: YAML syntax error in frontmatter: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise StrategyLoaderError(
            f"{path}: YAML frontmatter must be a mapping, got {type(data).__name__}"
        )

    name = _required_str(data, "name", path)
    status = _required_str(data, "status", path)
    if status not in ALLOWED_STATUSES:
        raise StrategyLoaderError(
            f"{path}: status={status!r} not in {sorted(ALLOWED_STATUSES)}"
        )

    strategy_type = _required_str(data, "strategy_type", path)
    if strategy_type not in ALLOWED_STRATEGY_TYPES:
        raise StrategyLoaderError(
            f"{path}: strategy_type={strategy_type!r} not in "
            f"{sorted(ALLOWED_STRATEGY_TYPES)}"
        )

    risk_envelope_pct = _required_decimal(data, "risk_envelope_pct", path)

    order_spec = None
    if strategy_type == STRATEGY_TYPE_HAND_CRAFTED:
        order_raw = data.get("order")
        if order_raw is None:
            raise StrategyLoaderError(
                f"{path}: hand_crafted strategies require an `order:` block"
            )
        if not isinstance(order_raw, dict):
            raise StrategyLoaderError(
                f"{path}: `order:` must be a mapping"
            )
        order_spec = _parse_order_spec(order_raw, path)

    regime_filter = _parse_regime_filter(data.get("regime_filter"), path)
    approved_at = _parse_optional_ts(data.get("approved_at"), path, field="approved_at")
    approved_commit_sha = _optional_str(data.get("approved_commit_sha"))
    how_this_works = _extract_how_this_works(body)

    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise StrategyLoaderError(f"{path}: stat failed: {exc}") from exc

    sha256 = _hash_bytes(raw.encode("utf-8"))

    return StrategyDocument(
        name=name,
        status=status,
        strategy_type=strategy_type,
        risk_envelope_pct=risk_envelope_pct,
        order_spec=order_spec,
        approved_at=approved_at,
        approved_commit_sha=approved_commit_sha,
        regime_filter=regime_filter,
        how_this_works=how_this_works,
        source_path=str(path),
        source_mtime=mtime,
        source_sha256=sha256,
        raw_frontmatter=data,
    )


def load_approved(path: Path) -> ApprovedStrategySnapshot:
    """Return a runtime snapshot for an approved strategy file.

    Raises StrategyLoaderError if:
        - status != "approved"
        - approved_at / approved_commit_sha missing
        - order_spec missing (hand_crafted only for Phase 2)
    """
    doc = load_document(path)
    if doc.status != STATUS_APPROVED:
        raise StrategyLoaderError(
            f"{path}: load_approved() requires status={STATUS_APPROVED!r}, "
            f"got {doc.status!r}. Use load_document() for proposed/retired."
        )
    if doc.approved_at is None:
        raise StrategyLoaderError(
            f"{path}: approved strategy missing `approved_at`"
        )
    if not doc.approved_commit_sha:
        raise StrategyLoaderError(
            f"{path}: approved strategy missing `approved_commit_sha`"
        )
    if doc.order_spec is None:
        raise StrategyLoaderError(
            f"{path}: approved strategy missing `order:` spec"
        )
    return ApprovedStrategySnapshot(
        name=doc.name,
        strategy_type=doc.strategy_type,
        risk_envelope_pct=doc.risk_envelope_pct,
        order_spec=doc.order_spec,
        approved_at=doc.approved_at,
        approved_commit_sha=doc.approved_commit_sha,
        regime_filter=doc.regime_filter,
        source_path=doc.source_path,
        source_mtime=doc.source_mtime,
        source_sha256=doc.source_sha256,
    )


def detect_drift(snapshot: ApprovedStrategySnapshot) -> bool:
    """Return True if the approved file has been modified post-approval.

    Engine calls this each tick; mismatch prompts a
    strategy_file_modified_post_approval journal event. False means the
    on-disk file still matches what was approved.

    A missing file is reported as drift (True) rather than an error --
    the engine's caller logs it; snapshot stays in effect until a
    follow-up /invest-ship retires the strategy.
    """
    path = Path(snapshot.source_path)
    if not path.exists():
        return True
    try:
        raw = path.read_bytes()
    except OSError:
        return True
    return _hash_bytes(raw) != snapshot.source_sha256


def assert_file_unchanged(snapshot: ApprovedStrategySnapshot) -> None:
    """Raise StrategyFileModifiedError on drift.

    Engine uses this when it wants to halt on tampered strategy files
    (paranoid mode). The default engine behavior is to log + continue
    via detect_drift(), NOT this stricter path.
    """
    path = Path(snapshot.source_path)
    if not path.exists():
        raise StrategyFileModifiedError(
            f"{snapshot.name}: approved file missing at {snapshot.source_path}",
            name=snapshot.name,
            approved_sha256=snapshot.source_sha256,
            current_sha256="<missing>",
        )
    current = _hash_bytes(path.read_bytes())
    if current != snapshot.source_sha256:
        raise StrategyFileModifiedError(
            f"{snapshot.name}: approved file sha256 drift "
            f"(approved={snapshot.source_sha256[:12]}, "
            f"current={current[:12]})",
            name=snapshot.name,
            approved_sha256=snapshot.source_sha256,
            current_sha256=current,
        )


def load_all_approved(strategies_dir: Path) -> list[ApprovedStrategySnapshot]:
    """Scan a directory of strategy files, return snapshots for those
    with status=approved.

    Codex round-11 P1: only files that claim status=approved have to
    parse cleanly. An in-progress draft (status=proposed / retired)
    that fails parse is NOT a runtime concern -- runtime only consumes
    approved strategies -- so we skip it with a log-worthy warning
    rather than aborting engine startup. Approved strategies that fail
    parse or fail the stricter load_approved() contract still raise,
    because those are the ones the engine is about to run money
    against.
    """
    if not strategies_dir.exists():
        return []
    out: list[ApprovedStrategySnapshot] = []
    errors: list[tuple[Path, Exception]] = []
    for path in sorted(strategies_dir.glob("*.md")):
        if path.name == "index.md":
            continue
        try:
            doc = load_document(path)
        except StrategyLoaderError as exc:
            # Codex round-12 P2: a parse failure whose intent we
            # cannot see from full YAML might STILL be an approved
            # strategy. Peek at the raw status line to decide:
            #   approved-intent + parse fails  -> raise (runtime gap)
            #   draft/retired + parse fails    -> quietly skip
            peek = _peek_status(path)
            if peek == STATUS_APPROVED:
                errors.append((path, exc))
            continue
        if doc.status != STATUS_APPROVED:
            continue
        try:
            snap = load_approved(path)
        except StrategyLoaderError as exc:
            errors.append((path, exc))
            continue
        out.append(snap)
    if errors:
        raise StrategyLoaderError(
            "approved strategy load errors: "
            + "; ".join(f"{p.name}: {e}" for p, e in errors)
        )
    return out


def _peek_status(path: Path) -> str | None:
    """Best-effort read of the `status:` field without full YAML parse.

    Used when load_document() fails: we still want to know if the
    file's INTENT was approved (and therefore a load failure should
    be loud) or draft/retired (quiet skip). Returns the trimmed
    status value, or None when the file has no readable status line.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = raw.splitlines()
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        return None
    for line in lines[1:]:
        if line.strip() == FRONTMATTER_DELIM:
            return None
        if line.lstrip().startswith("status:"):
            value = line.split(":", 1)[1].strip()
            if value and value[0] in "'\"" and value[-1] in "'\"":
                value = value[1:-1].strip()
            return value or None
    return None


# ---------- internals ----------


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StrategyLoaderError(f"{path}: read failed: {exc}") from exc


def _split_frontmatter(raw: str, path: Path) -> tuple[str, str]:
    lines = raw.splitlines(keepends=False)
    if not lines or lines[0].strip() != FRONTMATTER_DELIM:
        raise StrategyLoaderError(
            f"{path}: file must open with `---` YAML frontmatter fence"
        )
    try:
        end = lines.index(FRONTMATTER_DELIM, 1)
    except ValueError:
        raise StrategyLoaderError(
            f"{path}: unterminated YAML frontmatter (missing closing `---`)"
        )
    frontmatter = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    return frontmatter, body


def _required_str(data: dict[str, Any], key: str, path: Path) -> str:
    value = data.get(key)
    if value is None or not str(value).strip():
        raise StrategyLoaderError(f"{path}: missing required field `{key}`")
    return str(value).strip()


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _required_decimal(data: dict[str, Any], key: str, path: Path) -> Decimal:
    if key not in data:
        raise StrategyLoaderError(f"{path}: missing required field `{key}`")
    try:
        return Decimal(str(data[key]))
    except (InvalidOperation, ValueError) as exc:
        raise StrategyLoaderError(
            f"{path}: `{key}` must be a decimal, got {data[key]!r}"
        ) from exc


def _parse_order_spec(data: dict[str, Any], path: Path) -> StrategyOrderSpec:
    ticker = _required_str(data, "ticker", path)
    side = _required_str(data, "side", path).lower()
    if side not in {"buy", "sell"}:
        raise StrategyLoaderError(
            f"{path}: order.side must be 'buy' or 'sell', got {side!r}"
        )
    qty = data.get("qty")
    # Codex R15 P2: bool is a subclass of int in Python, so a YAML
    # `qty: true` would silently pass an isinstance(qty, int) check
    # and become a 1-share order. Reject booleans explicitly.
    if isinstance(qty, bool) or not isinstance(qty, int) or qty <= 0:
        raise StrategyLoaderError(
            f"{path}: order.qty must be a positive int, got {qty!r}"
        )
    limit_price = _required_decimal(data, "limit_price", path)
    stop_loss = None
    if data.get("stop_loss") is not None:
        stop_loss = _required_decimal(data, "stop_loss", path)
    tif = _optional_str(data.get("time_in_force")) or "DAY"
    return StrategyOrderSpec(
        ticker=ticker,
        side=side,
        qty=int(qty),
        limit_price=limit_price,
        stop_loss=stop_loss,
        time_in_force=tif,
    )


def _parse_regime_filter(value: Any, path: Path) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        out = []
        for v in value:
            if not isinstance(v, str):
                raise StrategyLoaderError(
                    f"{path}: regime_filter entries must be strings"
                )
            out.append(v)
        return tuple(out)
    raise StrategyLoaderError(
        f"{path}: regime_filter must be a string or list of strings"
    )


def _parse_optional_ts(value: Any, path: Path, *, field: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise StrategyLoaderError(
                f"{path}: `{field}` must be ISO-8601 timestamp, got {value!r}"
            ) from exc
    if dt.tzinfo is None:
        # Treat naive as UTC -- still raise for callers that care, via
        # the explicit tz check in engine code; here we normalize.
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _extract_how_this_works(body: str) -> str:
    """Grab the "## How This Works" section from the markdown body.

    Teach Mode rule: the section is mandatory regardless of
    learning-stage (CLAUDE.md Teach Mode table). Empty or missing -->
    return "" and let /invest-ship's approval-gate catch it. Loader
    does not enforce because Bundle 3 (invest-propose-limits) needs
    to load proposed-but-unfinished files too.
    """
    lower = body.lower()
    idx = lower.find("## how this works")
    if idx < 0:
        return ""
    section = body[idx:]
    lines = section.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == 0:
            continue  # skip the heading
        stripped = line.strip()
        if stripped.startswith("## ") and i > 0:
            break
        out.append(line)
    return "\n".join(out).strip()


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "assert_file_unchanged",
    "detect_drift",
    "load_all_approved",
    "load_approved",
    "load_document",
]
