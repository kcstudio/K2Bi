# Spec B Section 2 Round 2 Kimi Response

Reviewer artifact: `.code-reviews/2026-05-10T16-26-39Z_a9b268.log`

## Finding 1 - HIGH - Journal append / memory update crash window

Resolution: REJECT.

Rationale: The scenario requires the process to continue evaluating strategies after a crash or SIGKILL between two synchronous Python statements. That cannot happen. `_submit()` also breaks after a successful submit, so there is no later same-tick strategy evaluation after a successful parent order. `_journal_order_terminal()` has no async yield between journal append and in-memory discard; if the process dies, restart replay rebuilds from the journal. If `journal.append()` raises, the in-memory mutation is not reached. This is not a concrete §2 duplicate-submit risk.

## Finding 2 - HIGH - Legacy `order_filled` zero variants leave ghost pending entries

Resolution: ACCEPT.

Fix: `pending_order_map_from_journal()` now treats terminal full fills as terminal when `remaining_qty` parses to zero via `Decimal`, including `"0.0"` and `0.0`. For older fill records missing `remaining_qty`, replay falls back to `cumulative_filled_qty >=` the original submitted order quantity recorded on `order_submitted`.

Tests: Added D3b and D3c coverage in `tests/test_engine_order_dedup.py`.

## Finding 3 - MEDIUM - `sorted()` in pending helper

Resolution: REJECT.

Rationale: Deterministic ordering is intentional for stable journal payloads and stable tests. The expected set size is tiny because §2 is a duplicate-submit gate for one strategy/symbol pair, so the cost is not material. The finding does not identify an execution-safety bug.
