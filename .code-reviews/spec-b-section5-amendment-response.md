# Spec B §5 Amendment Disposition

## Falsified Assumption

The original §5 spec assumed MasterClientID=99 would give clientId=99 cross-client `cancelOrder()` authority.

Live paper test on 2026-05-11 disproved the cancel half:

- clientId 88 placed a non-marketable G BUY LMT test order.
- clientId 99 saw that order through `reqAllOpenOrders()`.
- clientId 99 `cancelOrder()` failed with IBKR error 10147.
- Cleanup through the original placing clientId 88 succeeded.
- Follow-up query showed no Spec B test orders remained open.

## IBKR Documentation

The live result matches the IBKR API contract:

- `cancelOrder()` cancels orders placed by the same API client.
- `reqGlobalCancel()` cancels all open orders, which is too broad for surgical orphan cleanup.
- Master Client ID is useful for cross-client order visibility/status, not per-order cancellation authority.

Sources:

- https://interactivebrokers.github.io/tws-api/cancel_order.html
- https://interactivebrokers.github.io/tws-api/open_orders.html
- https://www.interactivebrokers.com/campus/ibkr-api-page/twsapi-doc/

## Architect Amendment Scope

Accepted. §5 is now docs + config only:

- MasterClientID=99 is a visibility-only facility.
- Engine code change is none.
- The cross-client cancel red test from `da63664` was reverted at `f6aa240`.
- The live VPS IBC config uses `OverrideTwsMasterClientID=99`, which sets the Gateway Master Client ID to 99.
- The §7 checklist now verifies the VPS config and Gateway uptime after the config edit.
- Amended config was reapplied on 2026-05-11 at 04:44:08 UTC and verified by `grep MasterClientID /home/ibgateway/ibc/config.ini`.

## Deferred Follow-Up

Surgical orphan cleanup is deferred to `wiki/concepts/feature_orphan-order-cleanup-tool.md`.

The follow-up tool will accept an orphan's placing clientId from `reqAllOpenOrders()` output, spawn a temporary connection on that clientId, cancel the exact orphan order, and disconnect. It must not use `reqGlobalCancel()` for normal cleanup.

## Current Safety State

Post-test cleanup was verified through `scripts/gateway-query.sh`:

- no Spec B test orders open
- G qty 71
- avgCost 32.7840873
- exactly one G open STP SELL qty 71 @30
- `k2bi-engine` inactive and disabled

Audit trail:

- `wiki/log.md` `2026-05-11 11:47` recorded the failed cross-client cancel test and cleanup.
- `wiki/log.md` `2026-05-11 11:48` recorded rollback of the wrong config attempt.
- `wiki/log.md` `2026-05-11 12:47` recorded the amended visibility-only config reapply and fresh §0 broker state.
