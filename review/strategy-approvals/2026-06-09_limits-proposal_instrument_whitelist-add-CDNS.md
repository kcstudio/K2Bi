---
tags: [review, strategy-approvals, limits-proposal]
date: 2026-06-09
type: limits-proposal
origin: keith
status: proposed
applies-to: execution/validators/config.yaml
up: "[[index]]"
---

# Limits Proposal: add CDNS to symbols

## Change

```yaml
rule: instrument_whitelist
change_type: add
ticker: CDNS
field: symbols
before: [SPY, G]
after: [SPY, G, CDNS]
```

## Rationale (Keith's)

CDNS is the orchestrator's first end-to-end candidate: the A1-A3 analyst chain ran on it (T7-verified thesis, bear-case PROCEED, strategy backtested Sharpe 0.36 / max-DD -20.6% / win 57%, look_ahead_check passed). Adding CDNS to the engine instrument_whitelist enables the engine to accept the CDNS paper bracket order once the approved strategy ships. Fractional 0.25% NAV-at-risk sizing; all other validators (position_size, trade_risk, leverage, market_hours) still apply.

## Safety Impact (skill's assessment)

Neutral on aggregate risk. This only ENABLES trading CDNS; no order fires until the strategy-approval flow signs off on a strategy that references it. Existing validators (position_size, trade_risk, leverage, market_hours) still apply.

## YAML Patch

before:

```yaml
  symbols:
    - SPY
    - G
```

after:

```yaml
  symbols:
    - SPY
    - G
    - CDNS
```

## Approval

Pending Keith's review. Apply via `/invest-ship --approve-limits <path>`.
