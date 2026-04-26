---
name: invest-screen
description: Stage-2 watchlist enricher. Reads Stage-1 entries (promoted by invest-narrative Ship 2 or manual-promote) and adds Quick Score 0-100, 14 sub-factor absolute-band breakdown, and rating band A/B/C/D/F. Use when Keith says /screen <SYMBOL>, "add X to watchlist", "screen X", or automatically after a narrative candidate is promoted to the watchlist.
tier: analyst
routines-ready: yes
status: ready
---

# invest-screen

Stage-2 watchlist enricher. Closes the gap between "Keith has a watchlist entry" and "Keith has a scored, ranked candidate ready for first-domain thesis." Runs AFTER invest-narrative Ship 2 `--promote` in the pipeline.

## When to Trigger

- Keith says `/screen <SYMBOL>` or `/screen <SYMBOL> --reason "..."`
- Keith says "add X to watchlist", "screen X", "put X on the watchlist"
- A narrative candidate was just promoted to the watchlist and needs Stage-2 enrichment
- Re-evaluation: `/screen <SYMBOL> --re-enrich` to overwrite an existing screened entry

## When NOT to Use

- Keith wants top-of-funnel ticker discovery from a narrative -- route to `/invest-narrative` instead
- Keith wants deep due diligence on one company -- route to `/thesis <SYMBOL>` instead
- Keith wants backtesting -- route to `/backtest <strategy>` instead
- Execution or order placement -- route to `/execute` instead

## Input

Two entry paths:

### 1. `--enrich <SYMBOL>` (narrative-driven)

Reads an existing Stage-1 watchlist entry at `K2Bi-Vault/wiki/watchlist/<SYMBOL>.md` that was written by invest-narrative Ship 2 (`--promote`).

Required Stage-1 frontmatter fields (Ship 2 owns these; m2.13 never mutates them):
- `symbol`
- `status: promoted`
- `narrative_provenance`
- `reasoning_chain`
- `citation_url`
- `order_of_beneficiary`
- `ark_6_metric_initial_scores`

If any required Stage-1 field is missing or `status != promoted`, m2.13 fails loud. Ship 2 owns Stage-1; corruption is a Ship-2-side concern.

### 2. `--manual-promote <SYMBOL> [--reason "free text"]` (operator-driven)

Writes a minimal Stage-1 stub + full Stage-2 enrichment in a SINGLE atomic write, collapsing the intermediate `promoted` state straight to `screened`.

Stub fields (nullable):
- `narrative_provenance: null`
- `reasoning_chain: null`
- `citation_url: null`
- `order_of_beneficiary: null`
- `ark_6_metric_initial_scores: null`

The `--reason` text is included in the LLM scoring prompt. Without it, the LLM scores against just the symbol.

### Flags

| Flag | Purpose |
|---|---|
| `--enrich SYMBOL` | Enrich an existing promoted watchlist entry |
| `--manual-promote SYMBOL` | Write a new screened entry from scratch |
| `--reason TEXT` | Optional context for `--manual-promote` |
| `--re-enrich` | Force overwrite of an already-screened entry (use with `--enrich`) |

## Output

A single Markdown file at `K2Bi-Vault/wiki/watchlist/<SYMBOL>.md` with Stage-2 frontmatter injected.

### Stage-2 frontmatter keys (exact set)

```yaml
quick_score: int                    # 0-100
quick_score_breakdown:
  technical: int                    # 0-40
  fundamentals: int                 # 0-35
  catalyst: int                     # 0-25
sub_factors:
  trend_alignment: int              # 0-10
  momentum: int                     # 0-8
  volume_pattern: int               # 0-7
  pattern_quality: int              # 0-8
  key_level_proximity: int          # 0-7
  valuation: int                    # 0-8
  growth: int                       # 0-8
  profitability: int                # 0-7
  balance_sheet: int                # 0-6
  analyst: int                      # 0-6
  catalyst_clarity: int             # 0-8
  timeline: int                     # 0-6
  sentiment: int                    # 0-5
  rr_setup: int                     # 0-6
rating_band: "A" | "B" | "C" | "D" | "F"
band_definition_version: 1
status: screened                    # flipped from promoted (or set directly for manual)
```

### Band definitions (band_definition_version: 1)

| Band | Quick Score Range |
|---|---|
| A | 80-100 |
| B | 65-79 |
| C | 50-64 |
| D | 35-49 |
| F | <35 |

Sub-factor max scores live in `scripts/lib/data/invest_screen_bands_v1.json` (machine-readable; version bump means file shape change).

### Atomic write

All writes use tempfile + `os.replace` in the target directory. A mid-write failure leaves the original file untouched.

### Index update

After writing the watchlist entry, append or update a row in `K2Bi-Vault/wiki/watchlist/index.md`:

```markdown
| [[SYMBOL]] | YYYY-MM-DD | screened |
```

## LLM scoring contract

Single LLM call routed through `scripts.lib.minimax_common.chat_completion` (default provider Kimi per `K2B_LLM_PROVIDER`).

Prompt template (locked verbatim):

```
SYSTEM PROMPT:
You are an investment-research analyst scoring a watchlist candidate
for K2Bi (Keith's personal investment system) using the /trade-
watchlist Quick Score rubric. The candidate has already passed
narrative-stage validation (ticker exists, market cap >= $2B,
liquidity >= $10M ADV, citation real). Your job is to score it
across 14 sub-factors per the absolute-band rubric below.

Output ONE JSON object matching the schema. No prose preamble. No
conclusion. JSON only.

Sub-factor max scores (band_definition_version: 1):
TECHNICAL (sum to 0-40):
  trend_alignment: 0-10  (price vs 50/200 MA, higher = aligned)
  momentum: 0-8          (RSI / MACD posture, higher = stronger)
  volume_pattern: 0-7    (volume confirms trend = higher)
  pattern_quality: 0-8   (clean chart pattern = higher)
  key_level_proximity: 0-7  (near support entry = higher)
FUNDAMENTAL (sum to 0-35):
  valuation: 0-8         (cheap on P/E, P/S, EV/EBITDA = higher)
  growth: 0-8            (revenue + EPS growth trajectory)
  profitability: 0-7     (margins, ROE, ROIC)
  balance_sheet: 0-6     (low leverage, strong cash)
  analyst: 0-6           (upgrades, target revisions)
CATALYST (sum to 0-25):
  catalyst_clarity: 0-8  (specific, named, dated catalyst)
  timeline: 0-6          (catalyst within 90 days = higher)
  sentiment: 0-5         (positive flow, low short interest)
  rr_setup: 0-6          (favorable risk/reward at current entry)

Component sums must hit their max bands exactly (technical sub-
factors sum to a value 0-40, etc.). Do not exceed.

Output JSON schema:
{
  "sub_factors": {
    "trend_alignment": int, "momentum": int, "volume_pattern": int,
    "pattern_quality": int, "key_level_proximity": int,
    "valuation": int, "growth": int, "profitability": int,
    "balance_sheet": int, "analyst": int,
    "catalyst_clarity": int, "timeline": int, "sentiment": int,
    "rr_setup": int
  },
  "quick_score_breakdown": {
    "technical": int, "fundamentals": int, "catalyst": int
  },
  "quick_score": int,
  "rating_band": "A" | "B" | "C" | "D" | "F",
  "scoring_notes": "one paragraph explaining the score"
}

USER PROMPT:
Symbol: {SYMBOL}
Stage-1 context (Ship 2 / manual-promote):
{STAGE_1_CONTEXT}
Additional context from operator (--reason):
{OPTIONAL_REASON}

Score this candidate.
```

### Validation + retry

Math invariants enforced in code (raise on violation, retry LLM up to 2 times before failing):
- `sum(technical sub-factors) == quick_score_breakdown.technical`
- `sum(fundamentals sub-factors) == quick_score_breakdown.fundamentals`
- `sum(catalyst sub-factors) == quick_score_breakdown.catalyst`
- `quick_score_breakdown.fundamentals + technical + catalyst == quick_score`
- `rating_band` derived deterministically from `quick_score` (A=80-100, B=65-79, C=50-64, D=35-49, F=<35)

Out-of-range sub-factor or component-sum overflow triggers retry. After 3 failures total, raise with the LLM's last response in the error for debugging.

## Stage-1 / Stage-2 ownership boundary

| Layer | Owner | Keys |
|---|---|---|
| Stage-1 | invest-narrative Ship 2 | `symbol`, `narrative_provenance`, `reasoning_chain`, `citation_url`, `order_of_beneficiary`, `ark_6_metric_initial_scores`, `schema_version`, `tags`, `date`, `type`, `origin`, `up` |
| Stage-2 | invest-screen m2.13 | `quick_score`, `quick_score_breakdown`, `sub_factors`, `rating_band`, `band_definition_version` |
| Status flip | invest-screen m2.13 | `promoted -> screened` (enrich only) |

m2.13 preserves Stage-1 fields byte-for-byte: no key reorder, no value mutation, no whitespace change. Body content is preserved verbatim.

## Post-run summary

Every invocation prints a one-line summary to stdout:
```
SYMBOL enriched: quick_score=N, band=X
```
or for manual-promote:
```
SYMBOL manually promoted: quick_score=N, band=X
```

Idempotent by default: re-running `--enrich` on a `screened` entry exits 0 with:
```
SYMBOL already screened on YYYY-MM-DD; pass --re-enrich to overwrite
```

## Phase 4 stub

Future Ship 2 of invest-screen would replace LLM scoring with yfinance + technical-indicator library (e.g., ta-lib or pandas-ta) and bump `band_definition_version: 1 -> 2`. The `scripts/lib/data/invest_screen_bands_v1.json` shape would evolve to include indicator-to-sub-factor mappings and lookback parameters. Until then, the LLM contract above is the authoritative scoring source.

## Routines-Ready discipline (Analyst tier)

- **Stateless:** each run reads + writes one watchlist file only
- **Vault-in/vault-out:** watchlist page round-trip
- **Schedulable:** trivially schedulable (batch re-enrich job, Phase 4)
- **JSON I/O:** frontmatter is YAML-serializable; LLM I/O is JSON
- **Self-contained prompts:** no cross-skill dependency at runtime
