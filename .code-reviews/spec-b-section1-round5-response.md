# Spec B §1 Round 5 Review Response

Source review: `.code-reviews/2026-05-10T15-27-15Z_bf267f.log`

## Finding 1 [HIGH]

Resolution: REJECT.

Rationale: The finding says the decision-phase `ConnectorError` is unhandled and leaves the engine in a poisoned intermediate state. That is not the actual call stack. `_skip_buy_for_existing_position(..., abort_phase="decision")` re-raises after journaling, `_process_strategies()` propagates, and `_run_tick_body()` catches `(DisconnectedError, ConnectorError)` and calls `_enter_disconnected()`, which sets `self.state = EngineState.DISCONNECTED`. This is a defined retryable state, not a wedge. The test now asserts this explicitly: decision-phase position-query failure yields `tick.state_after == EngineState.DISCONNECTED`, no pending order, no `order_proposed`, no `order_submitted`, and one `cycle_skipped_position_query_failed` event with `abort_phase: "decision"`.

## Finding 2 [HIGH]

Resolution: PARTIAL ACCEPT.

Rationale: The duplicate-buy bypass claim is false. The helper already compares `position.ticker.upper() == symbol.upper()`, so a lowercase strategy ticker and uppercase broker ticker still skip. The real narrow issue is journal hygiene: skip events used the raw strategy ticker. Fixed by normalizing the helper symbol once and using the uppercase value for broker comparison, journal payload, and journal metadata. Added `test_existing_position_skip_journals_normalized_symbol` to prove the lowercase-strategy / uppercase-broker case skips and journals `SPY`.
