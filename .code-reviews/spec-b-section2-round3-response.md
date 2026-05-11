# Spec B Section 2 Round 3 Kimi Response

Reviewer artifact: `.code-reviews/2026-05-10T16-34-08Z_4085da.log`

## Finding 1 - HIGH - Missing qty on order_submitted creates immortal ghost pending entry

Resolution: ACCEPT.

Fix: `pending_order_map_from_journal()` now raises `JournalReplayMalformedJsonError` when an `order_submitted` record is missing `qty`, has a non-int `qty`, or uses bool as `qty`. Strict replay should fail closed on malformed submit records instead of building a pending order that cannot be cleared by legacy fill replay.

Tests: Added D4g coverage in `tests/test_engine_order_dedup.py`.

## Finding 2 - MEDIUM - Skip journal only surfaces first pending order id

Resolution: ACCEPT.

Fix: `cycle_skipped_pending_prior_submission` keeps the existing `pending_order_id` field for compatibility and now also records `pending_order_ids` plus `pending_order_count` so incident review sees the full pending set.

Tests: Added D1b coverage in `tests/test_engine_order_dedup.py`.
