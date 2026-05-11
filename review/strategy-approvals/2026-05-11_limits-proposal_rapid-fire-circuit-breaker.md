---
tags: [review, strategy-approvals, limits-proposal]
date: 2026-05-11
type: limits-proposal
origin: k2bi-generate
status: proposed
applies-to: execution/validators/config.yaml
up: "[[index]]"
approved_at:
approved_commit_sha:
---

# Limits Proposal: rapid-fire circuit breaker

## Change

```yaml
rule: rapid_fire_circuit_breaker
change_type: add
field: top_level_block
before: null
after:
  max_orders_per_window: 3
  window_seconds: 60
```

## Rationale

Spec B §3 requires a same-strategy, same-symbol submission rate gate to prevent a retry loop or queue drain from firing repeated BUY orders faster than the engine's normal cycle cadence.

## Safety Impact

Restrictive. The new block does not expand trading permission. It halts a strategy/symbol key after more than 3 order submissions in 60 seconds and requires an operator-authored re-arm sentinel before that key can submit again.

## YAML Patch

before:

```yaml
instrument_whitelist:
  symbols:
    - SPY
    - G
```

after:

```yaml
instrument_whitelist:
  symbols:
    - SPY
    - G

rapid_fire_circuit_breaker:
  max_orders_per_window: 3
  window_seconds: 60
```

## Approval

Pending Keith's review. Apply with the Spec B §3 implementation commit so the proposal transitions from proposed to approved in the same commit as the `execution/validators/config.yaml` edit.
