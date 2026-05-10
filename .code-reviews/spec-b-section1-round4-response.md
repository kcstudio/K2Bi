# Spec B §1 Round 4 Review Response

Source review: `.code-reviews/2026-05-10T15-08-36Z_876640.log`

## Finding 1 [HIGH]

Resolution: ACCEPT.

Action: Added `abort_phase` to `cycle_skipped_position_query_failed`. Decision-phase failures use `abort_phase: "decision"`. Pre-submit recheck failures use `abort_phase: "pre_submit_recheck"`, preserve the same trade_id as the already-journaled proposal, and fail closed before any broker submit.

## Finding 2 [HIGH]

Resolution: ARCHITECT OVERRIDE.

Residual TOCTOU window (~50-100ms between second `get_positions()` and broker `placeOrder()`) is qualitatively different from the 5/8 incident root cause. The 5/8 incident was the ABSENCE of any position check, not a race condition. §1 closes the absence. The residual window is closed by Spec B's defense-in-depth: §2 (journaled order_id dedup) + §3 (rapid-fire circuit breaker). Hardening the residual window inside §1 alone (e.g. via client_order_id idempotency token) would either duplicate §2's dedup mechanism or force ib_async-side broker-API features that are out of §1 scope. §1 ship discipline: close the named bug, leave defense-in-depth to layered defenses. Architect override of Kimi finding 2; reviewer was technically correct but scope-bounded to §1, finding belongs to §2.

## Finding 3 [MEDIUM]

Resolution: ACCEPT.

Action: Renamed `cycle_skipped_position_at_target` to `cycle_skipped_existing_position`. The event type now means "skip happened"; `payload.position_state` carries `at_target` or `partial`.

## Finding 4 [MEDIUM]

Resolution: ACCEPT.

Action: Added `test_g4b_pre_submit_position_query_fails_closed`. The test covers the first position query returning `[]`, the pre-submit recheck raising `ConnectorError`, no broker submit, no pending order, CONNECTED_IDLE retryable state, same trade_id as the proposal, and a `cycle_skipped_position_query_failed` event with `abort_phase: "pre_submit_recheck"`.
