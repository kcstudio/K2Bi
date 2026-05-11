# Spec B §5 Round 1 Kimi Disposition

Review log: `.code-reviews/2026-05-11T04-54-09Z_bebbce.log`

Kimi verdict: NEEDS-ATTENTION.

Codex disposition: three documentation findings accepted and fixed at `1c99ad3`; one connector-code finding rejected as outside §5 scope.

## Finding 1

Status: REJECTED

Claim: `IBKRConnector.cancel_order()` should add a runtime guard against cross-client cancellation attempts.

Concrete code anchor:

- `execution/connectors/ibkr.py:429`: `Spec B §5: a clientId=99 MasterClientID session can use`
- `execution/connectors/ibkr.py:430`: `` `reqAllOpenOrders()` for cross-client visibility, but IBKR still``
- `execution/connectors/ibkr.py:431`: ``binds `cancelOrder()` to the original placing clientId.``
- `execution/connectors/ibkr.py:1026`: `async def cancel_order(self, broker_order_id: str) -> None:`
- `execution/connectors/ibkr.py:1029`: `trades = self._ib.openTrades()`
- `execution/engine/main.py:2312`: `await self.connector.cancel_order(pending.broker_order_id)`
- `execution/engine/main.py:2444`: `await self.connector.cancel_order(o.broker_order_id)`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:231`: `Engine code change: NONE. §5 is config + docs only.`

Safety reasoning:

§5 is explicitly config + docs only after the architect amendment. The current engine connector is instantiated for the engine's own client path, not as the operator MasterClientID cleanup tool. Its current call sites are engine order-timeout and EOD cancellation paths, both operating on engine-owned pending/order state. Adding a new exception type and clientId authorization branch in `IBKRConnector.cancel_order()` would be an engine code change and would modify behavior outside the §5 visibility-only defense.

The Kimi finding is directionally useful for the deferred orphan cleanup tool, but not a §5 named-bug gap. §5's named bug is the false belief that MasterClientID=99 can surgically cancel cross-client orders. That has already been corrected in the spec, Known limitations, and backlog follow-up. The post-Spec-B cleanup tool should own any per-clientId cancel safeguards when it is implemented.

## Finding 2

Status: ACCEPTED - fixed at `1c99ad3`

Claim: The operator cleanup procedure showed executable raw Python without failure handling, dry-run, or confirmation guards.

Concrete code anchor:

- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:238`: `Do not use `reqGlobalCancel()` for normal cleanup.`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:239`: `Do not use clientId 99 for targeted cancellation.`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:240`: `Record the orphan's symbol, action, order type, quantity, stop or limit price, permId, orderId, orderRef, and placing clientId from `reqAllOpenOrders()`.`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:241`: `Use the deferred cleanup tool once it ships. Until then, cleanup is an operator-run incident procedure that must include exact permId or orderId confirmation, dry-run review, `try/finally` disconnect handling, and a `wiki/log.md` audit line.`

Safety reasoning:

The executable placeholder snippet was removed. The interim procedure now states constraints and required safeguards instead of handing future operators copy-paste broker code.

## Finding 3

Status: ACCEPTED - fixed at `1c99ad3`

Claim: The §7 checklist should explicitly verify engine state and `.killed` before any Gateway restart for §5 config maintenance.

Concrete code anchor:

- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:296`: `Before any `ib-gateway.service` restart for §5 config maintenance: verify `k2bi-engine.service` is inactive AND disabled, and verify `~/Projects/K2Bi-Vault/System/.killed` is absent.`

Safety reasoning:

The checklist now gates §5 gateway maintenance on the same engine-off facts used throughout Spec B: engine inactive, engine disabled, `.killed` absent.

## Finding 4

Status: ACCEPTED - fixed at `1c99ad3`

Claim: The deferred orphan cleanup tool lacked a minimum interface contract.

Concrete code anchor:

- `wiki/concepts/feature_orphan-order-cleanup-tool.md:28`: `## Interface Contract`
- `wiki/concepts/feature_orphan-order-cleanup-tool.md:33`: `scripts/orphan-order-cleanup.py --client-id 88 --perm-id 123456789 --confirm`
- `wiki/concepts/feature_orphan-order-cleanup-tool.md:38`: `` `--client-id`: original placing clientId reported by `reqAllOpenOrders()`. ``
- `wiki/concepts/feature_orphan-order-cleanup-tool.md:44`: `The operator supplies values from a `reqAllOpenOrders()` visibility query.`
- `wiki/concepts/feature_orphan-order-cleanup-tool.md:49`: `exact order matched, cancel submitted, terminal cancelled state observed or order absent on follow-up.`

Safety reasoning:

The backlog note now pins the planned CLI shape, required arguments, input fields from `reqAllOpenOrders()`, and exit codes. That gives the future implementation an explicit compatibility contract without pulling cleanup tooling into §5.

## Current Verification

Post-fix §0 after `1c99ad3`: `2026-05-11T06:27:18.856568+00:00`, G qty 71, avgCost 32.7840873, exactly one G open STP SELL qty 71 @30, no Spec B test orders open, `k2bi-engine` inactive and disabled.
