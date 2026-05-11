# Spec B Section 2 Round 6 Review Response

Reviewer artifact: `.code-reviews/2026-05-11T01-06-03Z_758d58.log`

Codex disposition: no new genuine §2 duplicate-submit defect. Section 2 is closed by Codex judgement after review iteration.

## Finding 1

Status: REJECTED.

The finding describes stale `_pending_orders` after a terminal journal append. A stale pending id blocks new submissions. It is a liveness and ergonomics concern, not a duplicate-submit safety gap. The described external manual terminal write would be picked up at the next tick-start refresh. Within the current tick, stale memory is conservative because it skips rather than submits.

The review also relies on future re-entrant or concurrent strategy evaluation that is not in the current engine. Spec B §2 closes the current duplicate-submit path, not hypothetical future concurrency.

## Finding 2

Status: REJECTED.

This is the same issue as finding 1, framed as journal/memory non-atomicity. If the terminal append succeeds but memory discard does not, the engine retains a pending id and skips. That fails closed for duplicate prevention. Restart replay also rebuilds the correct state from disk.

## Finding 3

Status: REJECTED.

`order_rejected` without `broker_order_id` is a valid current lifecycle event for pre-broker failures, including validator rejection, retired strategy rejection, and connector rejection before an ack exists. Raising on every such record would make strict replay fail on valid journal history.

`order_timeout` without `broker_order_id` would be malformed, but Kimi's proposed combined change is not safe as stated because it includes valid `order_rejected` records. No §2 duplicate-submit gap is present: an un-clearable malformed timeout leaves pending state in place, which skips rather than submits.

## Finding 4

Status: REJECTED.

For valid strict replay, `order_qty` context exists for every `order_submitted` because round 3 made missing or invalid submit quantity fail closed. A fill with no `remaining_qty` and no submitted quantity context is either unrelated history or reordered/manual journal corruption. Treating it as terminal would be unsafe, and raising on harmless unmatched legacy fills would create unnecessary startup failures.

The current policy remains correct: clear only when terminal quantity evidence exists. Otherwise preserve pending state or ignore unmatched fill records.

## Section 2 Closure

Accepted round-4 and round-5 fixes are committed:

- `b5a8c70` aligns startup pending replay with the init clock.
- `3de530c` fails closed on malformed `order_filled` replay without `broker_order_id`.

Full pytest after `3de530c`: `1544 passed, 1 skipped, 2 warnings`.

Latest §0 recheck after `3de530c`: `2026-05-11T01:05:31Z`, G qty 71, avgCost 32.7840873, exactly one G open STP order, engine inactive and disabled.
