"""Invest-regime manual classification MVP -- Phase 2 m2.14.

Library API:
    classify(band, reason, *, indicators=None, vault_root=None) -> Path

CLI:
    python3 -m scripts.lib.invest_regime classify <band> --reason "<text>"
        [--indicators '{"fear_greed": 32, "vix": 18.4, ...}']
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from scripts.lib.invest_ship_strategy import resolve_vault_root
from scripts.lib.strategy_frontmatter import atomic_write_bytes

VALID_BANDS = ["crash", "bear", "neutral", "bull", "euphoria"]

_INDICATOR_KEYS = [
    ("fear_greed", "Fear & Greed Index"),
    ("vix", "VIX"),
    ("vvix", "VVIX"),
    ("sector_breadth", "Sector Breadth"),
]


def _first_sentence(text: str) -> str:
    """Return the first sentence of *text*, up to 120 chars, no newlines."""
    text = text.strip().replace("\n", " ")
    if not text:
        return ""
    # Find the earliest sentence-ending punctuation.
    earliest: int | None = None
    for delim in ".!?":
        idx = text.find(delim)
        if idx != -1 and (earliest is None or idx < earliest):
            earliest = idx
    if earliest is not None:
        sentence = text[: earliest + 1].strip()
        if len(sentence) > 120:
            sentence = sentence[:120].rstrip() + "..."
        return sentence
    # No sentence terminator found -- truncate raw text.
    if len(text) > 120:
        return text[:120].rstrip() + "..."
    return text


def _render_indicator_table(indicators: dict[str, Any] | None) -> str:
    """Render a markdown table of indicator readings."""
    lines: list[str] = ["| Indicator | Value |", "|-----------|-------|"]
    for key, label in _INDICATOR_KEYS:
        if indicators and key in indicators:
            val = indicators[key]
            lines.append(f"| {label} | {val} |")
        else:
            lines.append(f"| {label} | n/a |")
    return "\n".join(lines)


def classify(
    band: str,
    reason: str,
    *,
    indicators: dict[str, Any] | None = None,
    vault_root: Path | None = None,
) -> Path:
    """Atomically write wiki/regimes/current.md with the given classification.

    Args:
        band: One of crash, bear, neutral, bull, euphoria.
        reason: Free-form reasoning paragraph.
        indicators: Optional dict of indicator readings.
        vault_root: Override vault root path.

    Returns:
        Path to the written file.

    Raises:
        ValueError: If band is not in VALID_BANDS.
    """
    band = band.strip().lower()
    if band not in VALID_BANDS:
        raise ValueError(
            f"Invalid regime band {band!r}. Valid options: {VALID_BANDS}"
        )

    if not reason or not reason.strip():
        raise ValueError("--reason is required and must not be empty.")

    if vault_root is None:
        vault_root = resolve_vault_root()

    target = vault_root / "wiki" / "regimes" / "current.md"

    today_str = date.today().isoformat()
    frontmatter = {
        "tags": ["regime", "k2bi"],
        "date": today_str,
        "type": "regime",
        "origin": "keith",
        "up": "[[index]]",
        "regime": band,
        "classified_date": today_str,
        "reasoning_summary": _first_sentence(reason),
    }

    body_parts: list[str] = [
        f"# Current Regime: {band}",
        "",
        "## Reasoning",
        "",
        reason.strip(),
        "",
        "## Indicator Readings",
        "",
        _render_indicator_table(indicators),
        "",
    ]

    content = (
        "---\n"
        + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
        + "---\n\n"
        + "\n".join(body_parts)
        + "\n"
    )

    atomic_write_bytes(target, content.encode("utf-8"))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Invest-regime manual classification MVP"
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    classify_parser = sub.add_parser(
        "classify", help="Classify the current market regime"
    )
    classify_parser.add_argument("band", help="Regime band")
    classify_parser.add_argument(
        "--reason", required=True, help="Reasoning paragraph"
    )
    classify_parser.add_argument(
        "--indicators",
        help='Optional JSON dict of indicator readings, e.g. \'{"vix": 18.4}\'',
    )

    args = parser.parse_args(argv)

    if args.cmd == "classify":
        indicators: dict[str, Any] | None = None
        if args.indicators:
            try:
                indicators = json.loads(args.indicators)
            except json.JSONDecodeError as exc:
                print(f"error: invalid JSON in --indicators: {exc}", file=sys.stderr)
                return 1
            if not isinstance(indicators, dict):
                print(
                    "error: --indicators must be a JSON object (dict)",
                    file=sys.stderr,
                )
                return 1

        try:
            path = classify(args.band, args.reason, indicators=indicators)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1

        print(f"Regime written to {path}")
        return 0

    return 2


if __name__ == "__main__":
    sys.exit(main())
