"""Deterministic strategy spec integrity preflight for ship adapters."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from execution.strategies import loader
from scripts.lib import strategy_frontmatter as sf


@dataclass(frozen=True)
class StrategySpecIntegrityResult:
    """Stable result for deterministic strategy spec preflight checks."""

    ok: bool
    findings: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable deterministic representation."""
        return {
            "ok": self.ok,
            "findings": [dict(finding) for finding in self.findings],
        }


def strategy_spec_integrity(strategy_path: Path) -> StrategySpecIntegrityResult:
    """Validate a strategy spec through deterministic K2Bi contracts.

    The check is read-only. It refuses on parse, forward-guidance, loader,
    or malformed present stop_loss errors. It never calls a model, reads a
    clock, or mutates broker or engine state.
    """

    try:
        return _strategy_spec_integrity(strategy_path)
    except Exception as exc:
        return _refusal(
            "integrity_exception",
            f"unexpected {type(exc).__name__} during integrity check",
        )


def _strategy_spec_integrity(strategy_path: Path) -> StrategySpecIntegrityResult:
    """Run integrity checks; caller owns broad fail-closed exception guard."""

    findings: list[dict[str, str]] = []
    try:
        content = strategy_path.read_bytes()
    except OSError as exc:
        return _refusal(
            "read_error",
            f"{strategy_path}: read failed: {exc}",
        )

    try:
        frontmatter = sf.parse(content)
    except ValueError as exc:
        return _refusal("frontmatter_parse_error", str(exc))

    try:
        sf.validate_forward_guidance_check(
            sf.extract_forward_guidance_check(frontmatter)
        )
    except ValueError as exc:
        findings.append(_finding("forward_guidance_error", str(exc)))

    stop_loss_error = _validate_present_stop_loss(frontmatter)
    if stop_loss_error is not None:
        findings.append(_finding("invalid_stop_loss", stop_loss_error))

    try:
        loader.load_document(strategy_path)
    except loader.StrategyLoaderError as exc:
        findings.append(_finding("loader_validation_error", str(exc)))

    return StrategySpecIntegrityResult(ok=not findings, findings=tuple(findings))


def _validate_present_stop_loss(frontmatter: dict[str, Any]) -> str | None:
    order = frontmatter.get("order")
    if not isinstance(order, dict) or "stop_loss" not in order:
        return None
    raw = order.get("stop_loss")
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "order.stop_loss must be a valid number, got bool"
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        return f"order.stop_loss must be a valid number, got {raw!r}"
    if not value.is_finite():
        return f"order.stop_loss must be finite, got {raw!r}"
    return None


def _finding(code: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "severity": "refusal",
        "message": message,
    }


def _refusal(code: str, message: str) -> StrategySpecIntegrityResult:
    return StrategySpecIntegrityResult(
        ok=False,
        findings=(_finding(code, message),),
    )
