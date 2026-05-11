# Spec B Section 2 Round 5 Review Response

Reviewer artifact: `.code-reviews/2026-05-11T00-56-46Z_dee169.log`

Codex disposition: one strict-replay consistency issue accepted, three findings rejected after checking current recovery and replay contracts.

## Finding 1

Status: REJECTED.

The claimed broker-acceptance crash window is already covered by the existing recovery design. The normal cycle writes `order_proposed` before calling `_submit()`. Recovery treats `order_proposed` as pending intent, indexes broker open orders and status events by the trade id embedded in `client_tag`, and matches broker state even when `order_submitted` was never written.

This is not inferred behavior. Existing recovery tests cover the exact scenario:

- `TradeIdFallbackMatchTests.test_proposed_only_journal_matches_via_client_tag`
- `TradeIdStatusMatchTests.test_terminal_status_matched_by_trade_id`
- `Q31` crash-window coverage for recovery-discovered fills

The review finding only looked at `_submit()` and missed the durable pre-submit `order_proposed` record plus recovery's trade-id fallback path. Adding a second provisional submit-intent event would duplicate the existing recovery contract.

## Finding 2

Status: REJECTED.

Strict replay errors intentionally propagate. Spec §2 requires malformed JSON, unknown event type, truncated final line, schema-version mismatch, and malformed lifecycle records to fail closed with `JournalReplayError` subclasses. The D4 tests assert this behavior through `tick_once()`.

Catching the exception and installing an in-memory "block all" sentinel would be a new state-machine behavior, not a required §2 fix. The current behavior aborts before any new broker submit, which is the safety property §2 needs.

## Finding 3

Status: REJECTED.

For the normal resume path, `_refresh_pending_orders_from_journal()` runs before `_pick_resumable_awaiting()`, so an `order_submitted` record is already represented in `_pending_orders` before `_pending_order` is set. For the crash window where only `order_proposed` exists, `_pending_order` is the active single-flight guard after recovery resumes the broker-live order.

The finding's manual-intervention scenario, clearing `_pending_order` without journaling terminal state, is not a current code path. It would be a separate operator or corruption incident, not a §2 implementation bug.

## Finding 4

Status: ACCEPTED.

`order_filled` without `broker_order_id` is malformed lifecycle data under strict §2 replay. The implementation now raises `JournalReplayMalformedJsonError` instead of silently ignoring the fill.

Regression test: `test_d4i_order_filled_missing_broker_order_id_fails_closed`.
