---
name: invest-regime
description: Manually classify the current market regime (crash, bear, neutral, bull, euphoria) and atomically update wiki/regimes/current.md. Auto-detection using feed data and scheduled regime detection are Phase 4+ scope. Use when Keith says /regime, "classify regime", "update regime to X", "what's the current regime".
tier: analyst
routines-ready: yes
phase: 2
status: shipped
---

# invest-regime

Manual market-regime classification MVP. The operator picks one band and supplies a one-paragraph reasoning. The skill atomically writes `wiki/regimes/current.md` with frontmatter + body. Overwrites prior state (no history archive in MVP).

## Command

```bash
python3 -m scripts.lib.invest_regime classify <band> --reason "<text>" [--indicators '{"fear_greed": 32, "vix": 18.4, ...}']
```

- `<band>`: one of `crash`, `bear`, `neutral`, `bull`, `euphoria` (required, positional)
- `--reason`: one-paragraph reasoning for the classification (required)
- `--indicators`: optional JSON dict of indicator readings. Recognized keys:
  - `fear_greed` -> Fear & Greed Index
  - `vix` -> VIX
  - `vvix` -> VVIX
  - `sector_breadth` -> Sector Breadth
  Missing keys render as `n/a` in the output table.

## Band definitions

| Band | One-line description |
|------|----------------------|
| crash | Broad panic-selling; circuit breakers likely; cash-preservation priority |
| bear | Sustained downtrend; risk-off warranted; tighten position sizes |
| neutral | No strong directional edge; range-bound or mixed signals |
| bull | Uptrend intact; risk-on acceptable; normal position sizing |
| euphoria | Excessive optimism; crowded positioning; consider taking risk off |

## Reasoning template

Supply a single paragraph (2-4 sentences) covering:
1. What price action or macro signal triggered the call.
2. Which indicators (VIX, breadth, credit spreads, etc.) support it.
3. What posture change (if any) it implies for open strategies.

Example:
```bash
python3 -m scripts.lib.invest_regime classify bull --reason "SPY holding above 50-day MA with expanding volume. VIX at 14, credit spreads tight. No reason to reduce exposure." --indicators '{"vix": 14.0, "fear_greed": 65}'
```

## Output

Atomically writes `K2Bi-Vault/wiki/regimes/current.md`:

```yaml
---
tags: [regime, k2bi]
date: YYYY-MM-DD
type: regime
origin: keith
up: "[[index]]"
regime: crash | bear | neutral | bull | euphoria
classified_date: YYYY-MM-DD
reasoning_summary: "<first-sentence-of-reasoning>"
---

# Current Regime: <band>

## Reasoning

<full reasoning text>

## Indicator Readings

| Indicator | Value |
|-----------|-------|
| Fear & Greed Index | <value or n/a> |
| VIX | <value or n/a> |
| VVIX | <value or n/a> |
| Sector Breadth | <value or n/a> |
```

## Phase 4 expansion stub (not in MVP)

- **Auto-fetch indicators**: pull VIX, Fear & Greed, breadth from data APIs rather than manual `--indicators` JSON.
- **History archive**: on each re-classification, copy the prior `current.md` to `wiki/regimes/YYYY-MM-DD_<band>.md` before overwrite.
- **Scheduled detection**: cron-driven stale-regime alerter when `classified_date` is older than a threshold.
- **Cross-skill consumption**: `invest-screen` reading regime to weight Quick Score; `invest-thesis` referencing regime in asymmetry block.

These are deferred until burn-in surfaces regime-mismatched trades or operator asks for automation.

## Routines-Ready discipline (Analyst tier)

- **Stateless**: each run reads + writes regime files only
- **Vault-in/vault-out**: `current.md` round-trip
- **Schedulable**: future stale-regime alerter (Phase 4) runs as cron
- **JSON I/O**: indicators are JSON-serializable
- **Self-contained prompts**: regime taxonomy lives in this skill body
