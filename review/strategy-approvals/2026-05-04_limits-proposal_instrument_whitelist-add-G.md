---
tags: [review, strategy-approvals, limits-proposal]
date: 2026-05-04
type: limits-proposal
origin: keith
status: approved
applies-to: execution/validators/config.yaml
up: "[[index]]"
approved_at: '2026-05-08T03:51:29.437799+00:00'
approved_commit_sha: f3f9a47
---

# Limits Proposal: add G to symbols

## Change

```yaml
rule: instrument_whitelist
change_type: add
ticker: G
field: symbols
before: [SPY]
after: [SPY, G]
```

## Rationale (Keith's)

Phase 3.8b first coach-driven paper trade; Genpact via cross-vendor GPT DR + Kimi DR convergence; mid-cap non-tech BPO; 0.25% NAV-at-risk fractional sizing.

## Safety Impact (skill's assessment)

Neutral on aggregate risk. This only ENABLES trading G; no order fires until the strategy-approval flow signs off on a strategy that references it. Existing validators (position_size, trade_risk, leverage, market_hours) still apply.

## YAML Patch

before:

```yaml
  symbols:
    - SPY
```

after:

```yaml
  symbols:
    - SPY
    - G
```

## Approval

Pending Keith's review. Apply via `/invest-ship --approve-limits <path>`.
