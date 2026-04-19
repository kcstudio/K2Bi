---
name: invest-backtest
description: Yfinance sanity-check backtest for a strategy spec. 2-year window, basic Sharpe / max-DD / win-rate. Explicitly NOT walk-forward (walk-forward harness is Phase 4 only, triggered by overfit signs during burn-in or when a second strategy needs it). Use when Keith says /backtest <strategy>, "backtest this", "sanity check the strategy on 2 years".
tier: Analyst
routines-ready: true
phase: 2
status: mvp
---

# invest-backtest -- MVP (Bundle 4 cycle 3 / m2.15)

Pulls 2 years of daily yfinance bars for the strategy's primary ticker, runs a lag-1 SMA(20)/SMA(50) crossover baseline, computes Sharpe / Sortino / max-DD / win-rate / total-return / n-trades / avg-holding-days, applies the 500% / -2% / 85% sanity gate, and atomic-writes an immutable per-run capture to `raw/backtests/YYYY-MM-DD_<slug>_backtest.md`.

**The strategy file is never touched.** Check D content-immutability (Bundle 3 cycle 4) holds. Backtest results live in `raw/backtests/`; `/invest-ship --approve-strategy` scans them via the spec §3.5 LOCKED algorithm before any approval.

## Architecture

Split:
- This SKILL.md body: parse args -> check for strategy file -> call the Python helper -> surface result.
- `scripts/lib/invest_backtest.py`: strategy read + yfinance pull + vectorized pandas sim + metrics + sanity gate + atomic write to `raw/backtests/`.
- `scripts/lib/invest_ship_strategy.py::scan_backtests_for_slug`: approval-gate consumer. SKILL.md is write-only; it does not call the scanner itself.

All vault mutation lives in Python through `scripts/lib/strategy_frontmatter.atomic_write_bytes`. No bash tempfile juggling. No direct writes.

## Invocation

```
/invest backtest <slug>
                 [--window-start YYYY-MM-DD]
                 [--window-end YYYY-MM-DD]
                 [--reference-symbol SYM]      default: SPY
```

`<slug>` resolves to `~/Projects/K2Bi-Vault/wiki/strategies/strategy_<slug>.md`. The strategy must exist; create it via hand-authoring or `/invest propose-limits` flow first.

## Pipeline (what Claude does on invocation)

### Step 1 -- Resolve strategy + sanity-check path

Default vault: `~/Projects/K2Bi-Vault`. Strategy must live at `wiki/strategies/strategy_<slug>.md`. If the file is missing, refuse with:

```
strategy_<slug>.md not found at <path>; author the spec first or run /screen
```

and exit 1. Keith authors strategy specs; this skill does not create them.

### Step 2 -- Call the Python backtest helper

```bash
python3 -c "
from pathlib import Path
from scripts.lib import invest_backtest as ib
result = ib.run_backtest(
    slug='<slug>',
    vault_root=Path('$HOME/Projects/K2Bi-Vault'),
    # window_start / window_end / reference_symbol passed through from CLI args
)
print(result.path)
print(result.look_ahead_check)
print(result.look_ahead_check_reason)
"
```

The helper owns: reading the strategy, pulling yfinance bars, running the simulation, computing metrics, applying the sanity gate, rendering + atomic-writing the capture file, and creating `raw/backtests/index.md` if missing.

### Step 3 -- Surface the result to Keith

Output format:

```
Backtest captured: raw/backtests/2026-04-19_spy-rotational_backtest.md

Sanity gate: passed      (or: suspicious -- <reason>)

Metrics:
- Sharpe:         1.42
- Sortino:        1.86
- Max drawdown: -8.5%
- Win rate:     58.0%
- Avg winner:   +2.3%
- Avg loser:    -1.8%
- Total return: +34.5%
- Trades:       87
- Avg holding:  9.2 days
```

If `look_ahead_check: suspicious`, add a trailing line:

```
Approval requires a `## Backtest Override` section in the strategy body explaining why these thresholds are acceptable (spec §3.5).
```

### Step 4 -- Append wiki/log.md entry

```bash
"$HOME/Projects/K2Bi/scripts/wiki-log-append.sh" \
  "/backtest" \
  "<slug> <look_ahead_check>" \
  "capture=raw/backtests/<filename>"
```

Never `>>`-append directly. Helper is the single writer (K2Bi audit fix #2 + pre-commit hook block).

## Sanity gate (spec §2.5 LOCK)

Tripped if ANY of:
- `total_return_pct > 500` (>500% over 2 years on a sanity baseline is almost certainly look-ahead)
- `max_dd_pct > -2` (<2% drawdown on 2y of equity is almost certainly look-ahead)
- `win_rate_pct > 85` (>85% 2y win rate is almost certainly look-ahead)

On trip: `look_ahead_check: suspicious` + `look_ahead_check_reason` names ALL tripped thresholds. Capture file is written REGARDLESS (audit trail); approval-gate refusal is the only consequence.

## Approval-gate integration (read-only contract)

When `/invest-ship --approve-strategy <slug>` runs, Step A calls `scan_backtests_for_slug(slug)` AFTER the bear-case scan. The scanner:

1. Globs `raw/backtests/*_<slug>_backtest.md`. Empty => REFUSE "run /backtest first".
2. Filters zero-byte files (defensive against interrupted writes).
3. Filename-descending sort; reads most recent.
4. Parses frontmatter. `look_ahead_check: passed` => PROCEED.
5. `look_ahead_check: suspicious` + `## Backtest Override` section in strategy body => PROCEED.
6. `look_ahead_check: suspicious` + no override => REFUSE with threshold-reason.
7. Unknown enum => REFUSE.

The override-section format Keith writes in the strategy body:

```markdown
## Backtest Override

Backtest run: 2026-04-19 at `raw/backtests/2026-04-19_<slug>_backtest.md`
Suspicious flag reason: total_return=620% > 500%
Why this is acceptable: <Keith's explanation, must be non-empty>
```

## Non-goals (not in Phase 2)

- **Walk-forward harness.** Rolling windows + embargoed k-fold deferred to Phase 4. Trigger: second strategy being added AND sanity-check can't validate it, OR Phase 3 strategy shows overfit signs.
- **Point-in-time data stores.** yfinance returns what it has today; Phase 2 accepts this limitation.
- **Multi-strategy portfolio backtest.** Phase 2 is one strategy at a time.
- **Slippage + commission modeling.** MVP uses mid-price fills. Phase 4 adds realistic cost model if first paper trade reveals meaningful drag.
- **Strategy-rule extraction from `## How This Works` prose.** Phase 2 MVP uses a fixed lag-1 SMA crossover baseline; the rule-extraction path lands alongside the Phase 4 walk-forward harness.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads strategy spec + pulls yfinance, writes a new file in raw/backtests/. No hidden state carried between runs.
- **Vault-in/vault-out:** strategy spec is the input; a new raw/backtests/ file is the output. Nothing else touched.
- **Schedulable:** the skill is pure-function at the vault layer. A nightly cron could refresh metrics without supervision.
- **JSON I/O:** all capture frontmatter is YAML-serializable (spec §2.5 schema).
- **Self-contained prompts:** no cross-skill dependency. The scanner lives in invest_ship_strategy.py but reads the capture file, not this module's runtime state.

## Hard rules

- The sanity gate thresholds (500% / -2% / 85%) are LOCKED in code. Overriding requires a spec change + bump here.
- Strategy file is NEVER written. The capture writer refuses any attempt to edit `wiki/strategies/strategy_<slug>.md` (the atomic-write helper targets `raw/backtests/` exclusively).
- The `## Backtest Override` section in strategy body is Keith's ONLY escape hatch for `suspicious` gate refusals. The skill body never auto-writes this section.
