---
tags: [feature, execution, ibkr, cleanup, backlog]
date: 2026-05-11
type: feature
status: backlog
priority: medium
effort: S
impact: medium
mvp: "given a detected orphan order's placing clientId from reqAllOpenOrders, surgically cancel via a temporary per-clientId connection without affecting other clients' orders; binary MVP test: simulate orphan on clientId 88, run cleanup tool with --clientId=88 flag, verify only the named orphan is cancelled, other clientId 88 orders untouched, other clientIds untouched"
origin: k2bi-generate
up: "[[index]]"
---

# Feature: orphan-order-cleanup-tool

## Goal

Build a small operator tool for surgical orphan-order cleanup after Spec B.

## Context

Spec B §5 live testing on 2026-05-11 falsified the assumption that MasterClientID=99 can cancel another client ID's individual orders. It can see cross-client open orders through `reqAllOpenOrders()`, but `cancelOrder()` remains bound to the original placing clientId. `reqGlobalCancel()` exists, but it is too broad for per-order cleanup because it cancels all open orders.

## MVP

Given a detected orphan order's placing clientId from `reqAllOpenOrders()`, the tool opens a temporary `ib_async` connection on that clientId, matches the exact orphan order, cancels it, and disconnects.

## Interface Contract

Planned CLI:

```bash
scripts/orphan-order-cleanup.py --client-id 88 --perm-id 123456789 --confirm
```

Required arguments:

- `--client-id`: original placing clientId reported by `reqAllOpenOrders()`.
- `--perm-id`: broker permId for the exact orphan order to cancel. If permId is unavailable, use `--order-id` plus `--order-ref`.
- `--confirm`: required for live cancellation. Without it, the tool runs dry-run only.

Input source:

- The operator supplies values from a `reqAllOpenOrders()` visibility query.
- Minimum required fields: symbol, action, orderType, totalQuantity, auxPrice or lmtPrice, tif, permId, orderId, orderRef, clientId, status.

Exit codes:

- `0`: exact order matched, cancel submitted, terminal cancelled state observed or order absent on follow-up.
- `1`: exact order not found, no cancellation submitted.
- `2`: ambiguous match, no cancellation submitted.
- `3`: broker/API failure after cancellation attempt; operator must re-query before retry.

Binary MVP test:

1. Simulate an orphan on clientId 88.
2. Run the cleanup tool with `--clientId=88` and an exact order match.
3. Verify only the named orphan is cancelled.
4. Verify other clientId 88 orders are untouched.
5. Verify other clientIds are untouched.

## Non-Goals

- No `reqGlobalCancel()` for normal cleanup.
- No engine re-enable dependency.
- No automatic orphan cancellation without operator confirmation.
