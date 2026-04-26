---
name: invest-bear-case
description: Run a single adversarial Claude Code call against a thesis before any order ticket. Returns VETO (>70% conviction bear) or PROCEED (with top-3 counter-points). Adapted from AI Investing Lab's 2026 pattern; single call, not a standing agent, per agent-topology.md decision. Use when Keith says /bear <SYMBOL>, "bear-case this", "poke holes in this thesis", or automatically as gate before /invest-ship approves a strategy whose first trade is a specific ticker.
tier: analyst
routines-ready: true
phase: 2
status: mvp
---

# invest-bear-case -- MVP (Bundle 4 cycle 2)

Single adversarial Claude Code pass against an existing thesis. Returns VETO (conviction > 70) or PROCEED (conviction <= 70) plus top-3 counterpoints + 2-5 invalidation scenarios. Writes to the thesis file's frontmatter (5 bear_* fields) and appends a dated `## Bear Case (YYYY-MM-DD)` body section.

## Architecture

**ONE Claude inference call per run (spec §0.3 constraint 1).** The adversarial pass is the Claude call itself; reviewers audit the code around it, not the inference.

Split:
- This SKILL.md body orchestrates: parse args -> read thesis -> build prompt -> call Claude -> parse JSON -> validate -> retry once on malformed shape -> hand to Python helper.
- `scripts/lib/invest_bear_case.py` validates schema strictly, merges frontmatter, appends body section, atomic-writes via `scripts/lib/strategy_frontmatter.atomic_write_bytes`.

All vault mutation lives in Python. All inference lives in Claude.

## Invocation

```
/invest bear-case <SYMBOL>
                  [--thesis <path>]             default: wiki/tickers/<SYMBOL>.md
                  [--refresh]                   force re-run regardless of freshness
                  [--position-size-hkd <N>]     Keith-provided; enables Teach Mode footer
```

## Pipeline (what Claude does on invocation)

### Step 1 -- Read learning-stage dial

```bash
LEARNING_STAGE=$(grep -E '^learning-stage:' ~/Projects/K2Bi-Vault/System/memory/active_rules.md 2>/dev/null | sed 's/learning-stage: *//' | tr -d '[:space:]')
LEARNING_STAGE=${LEARNING_STAGE:-novice}
```

If the dial is unset, default `novice`. Skills NEVER fail because the dial is missing.

### Step 2 -- Resolve thesis path + precondition check

Default: `~/Projects/K2Bi-Vault/wiki/tickers/<SYMBOL>.md`. Override via `--thesis <path>`.

If the thesis file does not exist OR has no `thesis_score:` field in frontmatter: refuse with `run /invest thesis <SYMBOL> first; bear-case requires existing thesis` and exit 1.

Emit a non-blocking warning if `thesis-last-verified` is more than 30 days old (Q2 answer):

```
WARNING: thesis last verified <date>, consider /invest thesis <SYMBOL> --refresh first
```

Do NOT auto-trigger thesis refresh. Keith decides whether to refresh first or proceed.

### Step 3 -- Same-day / fresh-within-30d short-circuit

If the thesis file already has `bear-last-verified:` within `FRESH_DAYS` (30) of today AND `--refresh` was NOT passed: skip with an informational message and exit 0. The Python helper also performs this check (authoritative) so either layer catching the skip is fine.

Same-day message: `bear-case already run today for <SYMBOL>; use --refresh to force re-run`.
Within-30d message: `bear-case fresh (<date>); use --refresh to force re-run`.

### Step 4 -- Build the adversarial prompt

Excerpt the bull-thesis material that makes the adversarial argument structurally meaningful. Minimum:

- thesis_score + sub_scores breakdown from frontmatter
- body phases 1-4 (business model, competitive moat, financial quality, risks + valuation)
- bull_case.reasons list
- asymmetry analysis table (if present)

If this skill was invoked from `/invest-ship --approve-strategy` as a pre-approval gate, include `Strategy proposing the trade: <strategy slug + one-line how-this-works summary>`; otherwise `Strategy proposing the trade: general thesis review`.

Prompt template:

```
Here is a bull thesis on <SYMBOL> (thesis_score: <N>, sub_scores: <breakdown>):

<thesis body excerpts: Phase 1-4 + Asymmetry Analysis + bull_case.reasons>

Strategy proposing the trade: <strategy context OR "general thesis review">

Your job: build the strongest case AGAINST this thesis.
- Use only verifiable claims (cite source or mark as inference).
- Identify the strongest STRUCTURAL reasons the thesis is wrong (not stylistic or cosmetic).
- Identify scenarios that would INVALIDATE the thesis (specific, testable conditions).
- Rate your conviction 0-100 on how strong the bear case is.
- If conviction > 70: return VETO with the top-3 strongest counterpoints.
- Else: return PROCEED with top-3 counterpoints Keith should monitor.

Return your response as JSON with this exact schema:
{
  "bear_conviction": <int 0-100>,
  "bear_top_counterpoints": [<str>, <str>, <str>],
  "bear_invalidation_scenarios": [<str>, ...] (2 to 5 items),
  "bear_verdict": "VETO" | "PROCEED",
  "reasoning_summary": "<2-4 sentence narrative>"
}
```

### Step 5 -- Execute ONE Claude call

Single inference. Do NOT dispatch subagents. Do NOT run multiple adversarial passes. The single call IS the bear-case (agent-topology.md MONOLITHIC decision -- adversarial debate explicitly rejected).

### Step 6 -- Parse JSON response

Try `json.loads` on the response. If `JSONDecodeError`:
1. `mkdir -p .bear-case-debug` first (idempotent -- MiniMax R5 defensive).
2. Write raw output to `.bear-case-debug/<timestamp>.txt` for inspection.
3. Fall back to regex extraction of `bear_conviction: <N>`, `bear_verdict: VETO|PROCEED`, enumerated counterpoints from `1. / 2. / 3.` lines.
4. If regex fallback also fails: exit 1 with `failed to parse adversarial response; see .bear-case-debug/<timestamp>.txt for raw output`.

### Step 7 -- Validate counterpoints count + invalidation-scenario range

Expected:
- Exactly 3 counterpoints.
- 2 to 5 invalidation scenarios.

If **>3 counterpoints**: truncate to first 3 (deterministic).

If **<3 counterpoints** OR **scenarios out of 2..5**: retry the inference ONCE with appended text:

```
CRITICAL: return exactly 3 counterpoints in bear_top_counterpoints AND 2-5 items in bear_invalidation_scenarios.
```

If the retry still fails validation: exit 1 with `malformed adversarial output: <specific-violation>; see .bear-case-debug/<timestamp>.txt`.

Retry happens HERE (orchestration-side). The Python helper validates strictly and raises ValueError on the same conditions -- that is the final gate if somehow malformed data still reaches it.

### Step 8 -- Hand to Python helper

The Python helper derives `bear_verdict` from `bear_conviction` via the strict `> 70 = VETO` rule. Ignore the inference's own verdict field if present -- the module constant `VETO_THRESHOLD = 70` is the single source of truth.

```python
from scripts.lib.invest_bear_case import (
    BearCaseInput,
    run_bear_case,
)

bear_input = BearCaseInput(
    bear_conviction=parsed["bear_conviction"],
    bear_top_counterpoints=parsed["bear_top_counterpoints"][:3],  # already truncated
    bear_invalidation_scenarios=parsed["bear_invalidation_scenarios"],
)

result = run_bear_case(
    symbol="<SYMBOL>",
    bear_input=bear_input,
    vault_root=pathlib.Path.home() / "Projects" / "K2Bi-Vault",
    refresh=args.refresh,
    learning_stage=LEARNING_STAGE,
    position_size_hkd=args.position_size_hkd,  # None if not provided
)
```

The helper:
- Validates symbol format + schema (conviction range, counterpoints count, scenarios range).
- Checks thesis file exists + has `thesis_score`.
- Re-checks freshness (skips if fresh + not refresh).
- Merges bear_* fields into existing frontmatter, preserving ALL thesis fields.
- Appends `## Bear Case (YYYY-MM-DD)` body section (with Teach Mode footer when `learning_stage in {novice, intermediate}` AND `position_size_hkd` is provided).
- Atomic-writes via `strategy_frontmatter.atomic_write_bytes`.

### Step 9 -- Log append

On successful write only:

```bash
~/Projects/K2Bi/scripts/wiki-log-append.sh \
    "invest-bear-case" \
    "wiki/tickers/<SYMBOL>.md" \
    "verdict=<VERDICT> conviction=<N> date=<TODAY>"
```

Do not call this on the fresh-skip path (nothing changed).

### Step 10 -- Return to Keith

Plain-language summary:

- VETO: "Bear-case VETO'd the <SYMBOL> thesis (conviction <N>). Address these counterpoints before approving any strategy on this ticker: ..."
- PROCEED: "Bear-case PROCEED on <SYMBOL> (conviction <N>). Monitor these counterpoints: ..."
- Skip: "Bear-case already fresh on <SYMBOL> (last run <date>). Use --refresh to force re-run."

Include the 3 counterpoints inline. Omit the raw JSON.

## Engine integration contract

`scripts/lib/invest_ship_strategy.handle_approve_strategy` refuses approval when the strategy's primary ticker (`order.ticker`) has:

1. No bear-case run (missing `bear_verdict` field).
2. Stale bear-case (`bear-last-verified` > 30 days).
3. `bear_verdict: VETO`.
4. Malformed thesis frontmatter.

The refusal message quotes the `run /invest bear-case <TICKER> ...` hint so Keith can act without hunting.

PROCEED requires `bear_verdict: PROCEED` AND `bear-last-verified` within 30 days.

This gate is code-enforced in `scan_bear_case_for_ticker`. There is NO CLI flag to bypass it. A fresh bear-case run is the only way to clear a stale / VETO / missing state.

## Locked decisions (do NOT relitigate)

- **VETO threshold 70 (strictly greater).** Spec §5 Q7 LOCK. Conviction exactly 70 = PROCEED. Encoded as `invest_bear_case.VETO_THRESHOLD`.
- **Single Claude call per run.** No subagents, no iterative debate, no cross-vendor adversarial. Agent-topology.md rejected multi-pass explicitly.
- **Append-only body.** Multiple runs (at 30+ day intervals or with --refresh) accumulate multiple dated sections. Existing body NEVER mutated; old sections preserved as audit trail.
- **No position sizing.** Validator-isolation constraint. `--position-size-hkd` is KEITH-PROVIDED input for Teach Mode translation only; the skill never computes sizing.
- **Frontmatter additive only.** The 5 bear_* fields are added/updated. ALL thesis fields preserved byte-equivalent (dict-level; parsed YAML compares equal).
- **Auto-stale warning only; no auto-refresh.** Q2 LOCK. Emit WARNING if thesis > 30 days; proceed regardless.

## Pedagogical layer (Teach Mode)

- **novice / intermediate + position_size_hkd provided** -> append `### Why this matters for your position` footer translating the verdict to HKD impact against the Keith-provided position size.
- **advanced** -> skip footer entirely.
- **novice / intermediate + no position_size_hkd** -> skip footer silently (don't error).

Footer phrasing differs by verdict:
- VETO: "if VETO, do NOT open the position -- address the counterpoints above first. Sizing smaller does not fix a broken thesis."
- PROCEED: "if PROCEED, size for your validator-capped max loss against the bear scenarios above. Treat counterpoints as active monitoring items."

Glossary: first-occurrence trading terms render as `[[glossary#term]]`; unknown terms auto-stub per CLAUDE.md convention (invest-thesis handles stubs on its own runs, so most terms already exist by the time bear-case emits body text).

## Routines-Ready discipline (Analyst tier)

- **Stateless**: each run reads thesis + writes bear block; no process-local state.
- **Vault-in / vault-out**: thesis in, bear block out, same file.
- **Schedulable**: can run as pre-approval gate automation (Phase 4+).
- **JSON I/O**: verdict + conviction + lists all YAML-serializable.
- **Self-contained prompts**: adversarial prompt template lives in this skill body.

## Non-goals (Phase 2 scope)

- NBLM-grounded bear case (Phase 4 conditional per nblm-mvp).
- Multi-round adversarial debate (rejected per agent-topology).
- Automatic re-run on 10-Q drops (Phase 4 conditional).
- Portfolio-aware footer math (Phase 4 conditional; currently single-position HKD input).

## Error paths (exit codes)

- `0` success (wrote a new bear section OR skipped because fresh).
- `1` thesis missing / no thesis_score / validation failed / JSON parse failed after retry.
- `2` unexpected internal error (atomic-write failure, filesystem I/O).

## Archive / debug artifacts

- `.bear-case-debug/<timestamp>.txt` -- raw Claude output on JSON parse failure. Gitignored. Keep the 10 most recent; janitor older files.
- `wiki/log.md` single-writer append per successful run.
