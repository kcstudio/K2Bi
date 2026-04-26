"""Invest-screen Stage-2 enricher -- Phase 3.7 m2.13.

Library API:
    enrich(symbol, *, vault_root=None, re_enrich=False, call_fn=None) -> Path
    manual_promote(symbol, *, reason=None, vault_root=None, call_fn=None) -> Path

CLI:
    python3 -m scripts.lib.invest_screen --enrich SYMBOL [--re-enrich]
    python3 -m scripts.lib.invest_screen --manual-promote SYMBOL [--reason TEXT]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from scripts.lib.invest_ship_strategy import resolve_vault_root
from scripts.lib.strategy_frontmatter import atomic_write_bytes, parse as parse_frontmatter
from scripts.lib.watchlist_index import update_watchlist_index

# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

_DATA_DIR = Path(__file__).with_suffix("").parent / "data"
_BANDS_PATH = _DATA_DIR / "invest_screen_bands_v1.json"


def _load_bands() -> dict[str, Any]:
    with _BANDS_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# JSON extraction from LLM responses
# ---------------------------------------------------------------------------


def _extract_json(text: str) -> dict:
    """Strip optional markdown fences and parse JSON."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)


# ---------------------------------------------------------------------------
# Prompt templates (LOCKED VERBATIM -- do not edit)
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = (
    "You are an investment-research analyst scoring a watchlist candidate "
    "for K2Bi (Keith's personal investment system) using the /trade-"
    "watchlist Quick Score rubric. The candidate has already passed "
    "narrative-stage validation (ticker exists, market cap >= $2B, "
    "liquidity >= $10M ADV, citation real). Your job is to score it "
    "across 14 sub-factors per the absolute-band rubric below.\n\n"
    "Output ONE JSON object matching the schema. No prose preamble. No "
    "conclusion. JSON only.\n\n"
    "Sub-factor max scores (band_definition_version: 1):\n"
    "TECHNICAL (sum to 0-40):\n"
    "  trend_alignment: 0-10  (price vs 50/200 MA, higher = aligned)\n"
    "  momentum: 0-8          (RSI / MACD posture, higher = stronger)\n"
    "  volume_pattern: 0-7    (volume confirms trend = higher)\n"
    "  pattern_quality: 0-8   (clean chart pattern = higher)\n"
    "  key_level_proximity: 0-7  (near support entry = higher)\n"
    "FUNDAMENTAL (sum to 0-35):\n"
    "  valuation: 0-8         (cheap on P/E, P/S, EV/EBITDA = higher)\n"
    "  growth: 0-8            (revenue + EPS growth trajectory)\n"
    "  profitability: 0-7     (margins, ROE, ROIC)\n"
    "  balance_sheet: 0-6     (low leverage, strong cash)\n"
    "  analyst: 0-6           (upgrades, target revisions)\n"
    "CATALYST (sum to 0-25):\n"
    "  catalyst_clarity: 0-8  (specific, named, dated catalyst)\n"
    "  timeline: 0-6          (catalyst within 90 days = higher)\n"
    "  sentiment: 0-5         (positive flow, low short interest)\n"
    "  rr_setup: 0-6          (favorable risk/reward at current entry)\n\n"
    "Component sums must hit their max bands exactly (technical sub-"
    "factors sum to a value 0-40, etc.). Do not exceed.\n\n"
    "Output JSON schema:\n"
    '{\n'
    '  "sub_factors": {\n'
    '    "trend_alignment": int, "momentum": int, "volume_pattern": int,\n'
    '    "pattern_quality": int, "key_level_proximity": int,\n'
    '    "valuation": int, "growth": int, "profitability": int,\n'
    '    "balance_sheet": int, "analyst": int,\n'
    '    "catalyst_clarity": int, "timeline": int, "sentiment": int,\n'
    '    "rr_setup": int\n'
    '  },\n'
    '  "quick_score_breakdown": {\n'
    '    "technical": int, "fundamentals": int, "catalyst": int\n'
    '  },\n'
    '  "quick_score": int,\n'
    '  "rating_band": "A" | "B" | "C" | "D" | "F",\n'
    '  "scoring_notes": "one paragraph explaining the score"\n'
    '}'
)

_LLM_USER_PROMPT_TEMPLATE = (
    "Symbol: {SYMBOL}\n"
    "Stage-1 context (Ship 2 / manual-promote):\n"
    "{STAGE_1_CONTEXT}\n"
    "Additional context from operator (--reason):\n"
    "{OPTIONAL_REASON}\n\n"
    "Score this candidate."
)

# ---------------------------------------------------------------------------
# Rating band derivation
# ---------------------------------------------------------------------------


def _derive_rating_band(quick_score: int, bands: dict) -> str:
    for band, cfg in bands["rating_bands"].items():
        if cfg["min"] <= quick_score <= cfg["max"]:
            return band
    raise ValueError(f"quick_score {quick_score} out of range for any band")


# ---------------------------------------------------------------------------
# LLM output validation
# ---------------------------------------------------------------------------


def _validate_llm_output(data: dict, bands: dict) -> dict:
    """Validate LLM JSON output against band definitions.

    Raises ValueError on any invariant violation.
    """
    sub_factors = data.get("sub_factors")
    if not isinstance(sub_factors, dict):
        raise ValueError(f"'sub_factors' must be dict, got {type(sub_factors).__name__}")

    breakdown = data.get("quick_score_breakdown")
    if not isinstance(breakdown, dict):
        raise ValueError(f"'quick_score_breakdown' must be dict, got {type(breakdown).__name__}")

    quick_score = data.get("quick_score")
    if not isinstance(quick_score, int):
        raise ValueError(f"'quick_score' must be int, got {type(quick_score).__name__}")

    rating_band = data.get("rating_band")
    if not isinstance(rating_band, str):
        raise ValueError(f"'rating_band' must be str, got {type(rating_band).__name__}")

    expected_names = set(bands["sub_factors"].keys())
    actual_names = set(sub_factors.keys())
    if actual_names != expected_names:
        missing = expected_names - actual_names
        extra = actual_names - expected_names
        raise ValueError(f"Sub-factor name mismatch: missing {missing}, extra {extra}")

    # Range checks
    for name, cfg in bands["sub_factors"].items():
        value = sub_factors[name]
        if not isinstance(value, int):
            raise ValueError(f"Sub-factor {name} must be int, got {type(value).__name__}")
        if value < 0 or value > cfg["max"]:
            raise ValueError(f"Sub-factor {name}={value} out of range [0, {cfg['max']}]")

    # Component sums
    technical_sum = sum(
        sub_factors[k] for k, v in bands["sub_factors"].items() if v["component"] == "technical"
    )
    fundamental_sum = sum(
        sub_factors[k] for k, v in bands["sub_factors"].items() if v["component"] == "fundamentals"
    )
    catalyst_sum = sum(
        sub_factors[k] for k, v in bands["sub_factors"].items() if v["component"] == "catalyst"
    )

    if technical_sum != breakdown.get("technical"):
        raise ValueError(
            f"Technical sum mismatch: {technical_sum} != {breakdown.get('technical')}"
        )
    if fundamental_sum != breakdown.get("fundamentals"):
        raise ValueError(
            f"Fundamental sum mismatch: {fundamental_sum} != {breakdown.get('fundamentals')}"
        )
    if catalyst_sum != breakdown.get("catalyst"):
        raise ValueError(
            f"Catalyst sum mismatch: {catalyst_sum} != {breakdown.get('catalyst')}"
        )

    computed_quick = technical_sum + fundamental_sum + catalyst_sum
    if computed_quick != quick_score:
        raise ValueError(f"Quick score mismatch: {computed_quick} != {quick_score}")

    derived_band = _derive_rating_band(quick_score, bands)
    if rating_band != derived_band:
        raise ValueError(
            f"Rating band mismatch: LLM said {rating_band}, derived {derived_band}"
        )

    return {
        "sub_factors": sub_factors,
        "quick_score_breakdown": breakdown,
        "quick_score": quick_score,
        "rating_band": rating_band,
    }


# ---------------------------------------------------------------------------
# LLM call wrapper with retry
# ---------------------------------------------------------------------------

_MAX_RETRIES = 2


def _call_llm(
    symbol: str,
    stage1_context: str,
    optional_reason: str,
    call_fn: Callable[[str, str, str], dict] | None,
) -> dict:
    if call_fn is not None:
        return call_fn(symbol, stage1_context, optional_reason)

    from scripts.lib.minimax_common import chat_completion, extract_assistant_text

    user_prompt = _LLM_USER_PROMPT_TEMPLATE.format(
        SYMBOL=symbol,
        STAGE_1_CONTEXT=stage1_context,
        OPTIONAL_REASON=optional_reason,
    )

    resp = chat_completion(
        model="kimi-for-coding",
        messages=[
            {"role": "system", "content": _LLM_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=2048,
        temperature=0.3,
    )
    text = extract_assistant_text(resp)
    return _extract_json(text)


def _score_symbol(
    symbol: str,
    stage1_context: str,
    optional_reason: str,
    call_fn: Callable[[str, str, str], dict] | None,
) -> dict:
    """Call LLM and validate output, retrying up to _MAX_RETRIES times."""
    bands = _load_bands()
    last_error: Exception | None = None
    last_response: dict | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            raw = _call_llm(symbol, stage1_context, optional_reason, call_fn)
            last_response = raw
            validated = _validate_llm_output(raw, bands)
            return validated
        except Exception as exc:
            last_error = exc
            continue

    raise ValueError(
        f"LLM scoring failed after {_MAX_RETRIES + 1} attempts. "
        f"Last response: {json.dumps(last_response, default=str)}. "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Stage-1 validation
# ---------------------------------------------------------------------------

_STAGE1_REQUIRED_KEYS = frozenset({
    "symbol",
    "narrative_provenance",
    "reasoning_chain",
    "citation_url",
    "order_of_beneficiary",
    "ark_6_metric_initial_scores",
})


def _validate_stage1_presence(fm: dict, path: Path) -> None:
    """Fail loud if required Stage-1 fields are missing."""
    missing = _STAGE1_REQUIRED_KEYS - set(fm.keys())
    if missing:
        raise ValueError(
            f"Stage-1 validation failed for {path.name}: missing required fields {sorted(missing)}"
        )


def _validate_stage1_status(fm: dict, path: Path) -> None:
    """Fail loud if Stage-1 status is not 'promoted'."""
    status = str(fm.get("status", "")).strip()
    if status != "promoted":
        raise ValueError(
            f"Stage-1 validation failed for {path.name}: expected status 'promoted', got {status!r}"
        )


def _validate_stage1_values(fm: dict, path: Path) -> None:
    """Fail loud if Ship-2-owned Stage-1 fields are present but malformed.

    Key-presence is checked separately by ``_validate_stage1_presence``.
    This pass guards against null, wrong type, empty string, or
    out-of-range values surviving the Ship 2 ``--promote`` writer and
    being silently canonized into a ``screened`` Stage-2 file.
    """
    errors: list[str] = []

    symbol = fm.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        errors.append(f"symbol: expected non-empty string, got {symbol!r}")
    else:
        expected = path.stem.upper()
        if symbol.strip().upper() != expected:
            errors.append(
                f"symbol: file path stem is {expected!r} but frontmatter has {symbol!r}"
            )

    provenance = fm.get("narrative_provenance")
    if not isinstance(provenance, str) or not provenance.strip():
        errors.append(
            f"narrative_provenance: expected non-empty string, got {provenance!r}"
        )

    reasoning = fm.get("reasoning_chain")
    if not isinstance(reasoning, str) or not reasoning.strip():
        errors.append(
            f"reasoning_chain: expected non-empty string, got {reasoning!r}"
        )

    citation = fm.get("citation_url")
    if not isinstance(citation, str) or not citation.strip():
        errors.append(f"citation_url: expected non-empty string, got {citation!r}")

    order = fm.get("order_of_beneficiary")
    if not isinstance(order, int) or isinstance(order, bool) or order not in (1, 2, 3):
        errors.append(
            f"order_of_beneficiary: expected int in (1, 2, 3), got {order!r}"
        )

    ark_scores = fm.get("ark_6_metric_initial_scores")
    if ark_scores is not None and not isinstance(ark_scores, dict):
        errors.append(
            f"ark_6_metric_initial_scores: expected dict or null, got {type(ark_scores).__name__}"
        )

    if errors:
        raise ValueError(
            f"Stage-1 value validation failed for {path.name}: " + "; ".join(errors)
        )


# ---------------------------------------------------------------------------
# Frontmatter mutation (preserves Stage-1 fields byte-for-byte)
# ---------------------------------------------------------------------------


def _enrich_frontmatter(original_bytes: bytes, stage2_data: dict) -> bytes:
    """Inject Stage-2 keys and flip status to 'screened' while preserving
    all other frontmatter lines exactly as they appear in the original."""
    text = original_bytes.decode("utf-8")
    lines = text.splitlines(keepends=False)

    if not lines or lines[0].strip() != "---":
        raise ValueError("Missing frontmatter opening fence")

    try:
        closing_idx = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError("Missing frontmatter closing fence") from exc

    fm_lines = lines[1:closing_idx]
    body_lines = lines[closing_idx + 1 :]

    new_fm_lines: list[str] = []
    status_replaced = False
    for line in fm_lines:
        stripped = line.strip()
        if stripped.startswith("status:") or stripped == "status":
            new_fm_lines.append("status: screened")
            status_replaced = True
        else:
            new_fm_lines.append(line)

    if not status_replaced:
        new_fm_lines.append("status: screened")

    stage2_yaml = yaml.safe_dump(stage2_data, sort_keys=False, allow_unicode=True)
    new_fm_lines.extend(stage2_yaml.strip().splitlines())

    result_lines = ["---"] + new_fm_lines + ["---"] + body_lines
    return ("\n".join(result_lines) + "\n").encode("utf-8")


# ---------------------------------------------------------------------------
# Manual-promote file builder
# ---------------------------------------------------------------------------


def _build_manual_stub(symbol: str, stage2_data: dict, date: str) -> bytes:
    frontmatter: dict[str, Any] = {
        "tags": ["watchlist", "k2bi"],
        "date": date,
        "type": "watchlist",
        "origin": "keith",
        "up": "[[index]]",
        "symbol": symbol,
        "status": "screened",
        "schema_version": 1,
        "narrative_provenance": None,
        "reasoning_chain": None,
        "citation_url": None,
        "order_of_beneficiary": None,
        "ark_6_metric_initial_scores": None,
        **stage2_data,
    }
    fm_lines = ["---"]
    fm_lines.extend(yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).splitlines())
    fm_lines.append("---")
    body_lines = [
        f"# Watchlist: {symbol}",
        "",
        f"Manually promoted on {date}.",
        "",
        "## Linked notes",
        "",
        "- [[index]]",
        "",
    ]
    full = "\n".join(fm_lines) + "\n" + "\n".join(body_lines) + "\n"
    return full.encode("utf-8")


# ---------------------------------------------------------------------------
# Index updater
# ---------------------------------------------------------------------------


def _update_watchlist_index(vault: Path, symbol: str, date: str, status: str) -> None:
    """Backwards-compatible thin shim. New code should call
    ``scripts.lib.watchlist_index.update_watchlist_index`` directly.
    """
    update_watchlist_index(vault, symbol, date, status)


# ---------------------------------------------------------------------------
# Core API
# ---------------------------------------------------------------------------


def enrich(
    symbol: str,
    *,
    vault_root: Path | None = None,
    re_enrich: bool = False,
    call_fn: Callable[[str, str, str], dict] | None = None,
) -> Path:
    """Enrich a Stage-1 watchlist entry with Stage-2 scores.

    Idempotent by default: if the entry is already ``screened``, exits
    cleanly unless ``re_enrich`` is True.
    """
    vault = resolve_vault_root(vault_root)
    symbol = symbol.upper()
    watchlist_path = vault / "wiki" / "watchlist" / f"{symbol}.md"

    if not watchlist_path.exists():
        raise FileNotFoundError(f"Watchlist entry not found: {watchlist_path}")

    original_bytes = watchlist_path.read_bytes()
    fm = parse_frontmatter(original_bytes)

    current_status = str(fm.get("status", "")).strip()
    if current_status == "screened":
        if not re_enrich:
            date = str(fm.get("date", "unknown"))
            print(f"{symbol} already screened on {date}; pass --re-enrich to overwrite")
            return watchlist_path

    if re_enrich:
        if current_status != "screened":
            raise ValueError(
                f"--re-enrich requires status 'screened', got {current_status!r}"
            )
        _validate_stage1_presence(fm, watchlist_path)
        _validate_stage1_values(fm, watchlist_path)
    else:
        _validate_stage1_presence(fm, watchlist_path)
        _validate_stage1_status(fm, watchlist_path)
        _validate_stage1_values(fm, watchlist_path)

    stage1_context = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
    validated = _score_symbol(symbol, stage1_context, "none provided", call_fn)

    stage2_data = {
        "quick_score": validated["quick_score"],
        "quick_score_breakdown": validated["quick_score_breakdown"],
        "sub_factors": validated["sub_factors"],
        "rating_band": validated["rating_band"],
        "band_definition_version": 1,
    }

    new_bytes = _enrich_frontmatter(original_bytes, stage2_data)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    try:
        atomic_write_bytes(watchlist_path, new_bytes)
        _update_watchlist_index(vault, symbol, today, "screened")
    except Exception as exc:
        # Rollback watchlist to original on index failure to avoid split-brain
        try:
            atomic_write_bytes(watchlist_path, original_bytes)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"Rollback failed after primary failure: {rollback_exc}"
            ) from exc
        raise

    print(
        f"{symbol} enriched: quick_score={validated['quick_score']}, "
        f"band={validated['rating_band']}"
    )
    return watchlist_path


def manual_promote(
    symbol: str,
    *,
    reason: str | None = None,
    vault_root: Path | None = None,
    call_fn: Callable[[str, str, str], dict] | None = None,
) -> Path:
    """Write a minimal Stage-1 stub + Stage-2 enrichment in a single atomic
    write, collapsing the intermediate ``promoted`` state."""
    vault = resolve_vault_root(vault_root)
    symbol = symbol.upper()
    watchlist_path = vault / "wiki" / "watchlist" / f"{symbol}.md"

    if watchlist_path.exists():
        raise FileExistsError(
            f"Watchlist entry {symbol} already exists. "
            f"Use --enrich if it is promoted, or --re-enrich if screened."
        )

    stage1_context = "minimal stub: no narrative provenance, reasoning chain, citation, or ARK scores available"
    optional_reason = reason if reason else "none provided"
    validated = _score_symbol(symbol, stage1_context, optional_reason, call_fn)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stage2_data = {
        "quick_score": validated["quick_score"],
        "quick_score_breakdown": validated["quick_score_breakdown"],
        "sub_factors": validated["sub_factors"],
        "rating_band": validated["rating_band"],
        "band_definition_version": 1,
    }

    content = _build_manual_stub(symbol, stage2_data, today)
    atomic_write_bytes(watchlist_path, content)

    try:
        _update_watchlist_index(vault, symbol, today, "screened")
    except Exception as exc:
        # Rollback: remove newly created watchlist file on index failure
        try:
            watchlist_path.unlink()
        except OSError as rollback_exc:
            raise RuntimeError(
                f"Rollback failed after primary failure: {rollback_exc}"
            ) from exc
        raise

    print(
        f"{symbol} manually promoted: quick_score={validated['quick_score']}, "
        f"band={validated['rating_band']}"
    )
    return watchlist_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Invest-screen Stage-2 enricher")
    parser.add_argument(
        "--enrich",
        metavar="SYMBOL",
        help="Enrich a promoted watchlist entry with Stage-2 scores",
    )
    parser.add_argument(
        "--manual-promote",
        metavar="SYMBOL",
        help="Manually promote a symbol straight to screened",
    )
    parser.add_argument(
        "--reason",
        help="Optional reason text for --manual-promote",
    )
    parser.add_argument(
        "--re-enrich",
        action="store_true",
        help="Force overwrite of an already-screened entry",
    )

    args = parser.parse_args(argv)

    if args.enrich and args.manual_promote:
        parser.error("--enrich and --manual-promote are mutually exclusive")

    if args.enrich:
        try:
            enrich(args.enrich.upper(), re_enrich=args.re_enrich)
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    if args.manual_promote:
        if args.re_enrich:
            parser.error("--re-enrich is not valid with --manual-promote")
        try:
            manual_promote(args.manual_promote.upper(), reason=args.reason)
        except FileExistsError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
