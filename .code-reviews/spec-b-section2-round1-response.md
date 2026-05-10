# Spec B Section 2 Round 1 Kimi Response

Reviewer artifact: `.code-reviews/2026-05-10T16-15-00Z_db7020.log`

## Finding 1 - CRITICAL - Crash window / split brain

Resolution: REJECT.

Rationale: The finding contradicts itself. It first says a crash between journal append and memory update would lose dedup, then notes restart rebuild reads the journal and is safe. It then claims journal append failure updates memory anyway, but `_submit()` calls `self.journal.append(...)` before mutating `_pending_orders`; if append raises, the memory update is not reached. If the append partially writes and raises, strict replay detects the truncated line and fails closed. The real adjacent issue in this finding was the repeated per-strategy replay cost, handled under Finding 4.

## Finding 2 - HIGH - Strict replay rejects valid v1 legacy lines

Resolution: REJECT.

Rationale: Valid `schema_version=1` lines are intentionally accepted. Spec §2 says pre-Spec-B legacy lines without `schema_version` must be migrated or explicitly normalized to `schema_version=1`; accepting normalized v1 is the intended migration path and matches the existing journal evolution rule that readers handle every known version. Missing, boolean, zero, unknown, and greater-than-current versions still fail closed.

## Finding 3 - HIGH - Malformed order records silently dropped

Resolution: ACCEPT.

Fix: `pending_order_map_from_journal()` now raises `JournalReplayMalformedJsonError` for `order_submitted` records missing `broker_order_id`, `strategy`, or `ticker`, and for `order_terminal` records missing `broker_order_id` or carrying an unknown `terminal_status`.

Tests: Added D4e and D4f coverage in `tests/test_engine_order_dedup.py`.

## Finding 4 - MEDIUM - Per-strategy disk replay

Resolution: ACCEPT.

Fix: `_process_strategies()` now refreshes the strict pending-order replay once per tick. `_pending_orders_for_strategy()` reads the in-memory map for the rest of that tick, and submit/terminal paths update the map synchronously.

Tests: Added D6 coverage asserting a two-strategy tick performs one three-day replay pass, not one pass per strategy.

## Finding 5 - MEDIUM - `order_terminal` schema added but not emitted

Resolution: ACCEPT.

Fix: The engine now emits `order_terminal` for full fills and broker terminal statuses observed through `_reconcile_terminal()`. The pending map also treats `order_filled` with `remaining_qty=0` as terminal for replay compatibility.

Tests: Added D2b coverage proving a full fill emits `order_terminal` and clears the pending map.
