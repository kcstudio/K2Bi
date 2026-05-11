# Spec B §3 Round 1 Kimi Disposition

Round 1 review log: `.code-reviews/2026-05-11T02-12-15Z_1ec180.log`

Round 2 re-review log: `.code-reviews/2026-05-11T02-18-51Z_570259.log`

Kimi round 1 verdict: NEEDS-ATTENTION.

Kimi round 2 verdict: APPROVE after reviewing this disposition doc.

Codex disposition: all four findings are rejected as non-bugs for §3. No code change required.

## Finding 1

Status: REJECTED

Claim: Non-atomic rapid-fire trip detection allows concurrent submitters to miss a halt.

Concrete code anchor:

- `execution/engine/main.py:1209`: `for snap in self._strategies:`
- `execution/engine/main.py:1398`: `await self._submit(`
- `execution/engine/main.py:1406`: `# Phase 2 MVP: one order per tick.`
- `execution/engine/main.py:1408`: `if self._pending_order is not None:`
- `execution/engine/main.py:1409`: `break`
- `execution/engine/main.py:1551`: `window = self._rapid_fire_window.setdefault(key, deque())`
- `execution/engine/main.py:1556`: `if len(window) <= max_orders or key in self._rapid_fire_halted:`

Safety reasoning:

The claimed race assumes concurrent strategy submitters for the same `(strategy_id, symbol)`. The engine does not have that execution model. `_process_strategies()` iterates strategies serially on one asyncio task and breaks after the first pending order is created. `_record_rapid_fire_submission()` is called only after `_submit()` journals a successful broker submit, and there is no task fan-out around it. The 4th successful submission is expected to be the trip edge under §3, not a pre-submit block.

Existing test coverage:

- `tests/test_engine_rapid_fire_breaker.py:259` tests R1. It creates three prior successful submissions, lets the 4th submit, asserts one `circuit_breaker_tripped_rapid_fire`, and asserts the next cycle skips with `cycle_skipped_rapid_fire_halt`.
- `tests/test_engine_rapid_fire_breaker.py:320` tests R4 cross-strategy isolation. It proves the halt key is `(strategy_id, symbol)`, not a global order throttle.

Why this is not a §3 named-bug gap:

§3 closes the absence of a same-strategy/same-symbol rate gate. It does not introduce a multi-worker submit scheduler or a thread-safe broker-submit pool. The live engine path is serialized, so the interleaving Kimi described is not reachable.

## Finding 2

Status: REJECTED

Claim: Sentinel deletion can happen before the clear journal event is durable.

Concrete code anchor:

- `execution/engine/main.py:1674`: `self.journal.append(`
- `execution/engine/main.py:1686`: `self._delete_rapid_fire_clear_sentinel(path)`
- `execution/journal/writer.py:170`: `self._atomic_append(self._path_for(when), record)`
- `execution/journal/writer.py:521`: `data_fd = os.open(`
- `execution/journal/writer.py:537`: `os.fsync(data_fd)`
- `execution/journal/writer.py:547`: `def _atomic_append(self, path: Path, record: dict[str, Any]) -> None:`

Safety reasoning:

`JournalWriter.append()` is synchronous. It validates the record, enters `_atomic_append()`, writes with `O_APPEND`, and calls `os.fsync(data_fd)` before returning to the engine. The sentinel delete is after `journal.append()` returns, so the clear event is durable before `path.unlink()` can run. Kimi's premise that append is asynchronous or buffered is false for this codebase.

Existing test coverage:

- `tests/test_engine_rapid_fire_breaker.py:347` tests R5. The test overrides sentinel deletion and asserts deletion is invoked only after `circuit_breaker_cleared` is visible in the journal.
- The durability mechanism itself is covered by the journal writer contract at `execution/journal/writer.py:5`, which states `O_APPEND + fsync`, and the implementation at `execution/journal/writer.py:521-537`.

Why this is not a §3 named-bug gap:

§3 requires journal-first ordering before sentinel deletion. The code has journal-first ordering, and the journal append path fsyncs before returning.

## Finding 3

Status: REJECTED

Claim: A consumed nonce should always be ignored, even when replayed against a newer active trip.

Concrete code anchor:

- `execution/engine/main.py:1614`: `if (trip_id, clear_nonce) in self._rapid_fire_consumed_clears:`
- `execution/engine/main.py:1620`: `if newer_active:`
- `execution/engine/main.py:1622`: `"circuit_breaker_cleared_stale_sentinel_rejected",`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:155`: `If a sentinel's nonce or trip_id mismatches the active halt, fail closed`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:170`: `R9: consumed clear nonce cannot clear a new trip.`

Safety reasoning:

Kimi's requested behavior contradicts the locked §3 R9 acceptance test. A stale consumed sentinel for `trip_id=1` reappearing while the same key is actively halted at `trip_id=2` must not be silently ignored, because the operator-authored file is now stale relative to a newer safety halt. The fail-closed behavior is to leave the newer halt in place, journal `circuit_breaker_cleared_stale_sentinel_rejected`, alert the operator, and delete the stale sentinel so it cannot repeatedly alarm.

Existing test coverage:

- `tests/test_engine_rapid_fire_breaker.py:414` tests R9. It trips `trip_id=1`, clears with `clear_nonce=nonce-A`, trips `trip_id=2`, recreates the old sentinel, then asserts no submit, one `circuit_breaker_cleared_stale_sentinel_rejected`, `telegram_alert_required`, and a remaining `cycle_skipped_rapid_fire_halt` for `trip_id=2`.
- `tests/test_engine_rapid_fire_breaker.py:396` tests R8. It covers the non-active leftover sentinel case and asserts `circuit_breaker_cleared_stale_sentinel_ignored`.

Why this is not a §3 named-bug gap:

The implementation separates the two required cases: leftover consumed sentinel with no active newer trip is ignored; stale consumed sentinel targeting a key with a newer active trip is rejected and alerted. That matches Spec B §3 lines 155 and 170.

## Finding 4

Status: REJECTED

Claim: Dedup-suppressed attempts should count toward rapid-fire windows, or replay should process `cycle_skipped_rapid_fire_halt`.

Concrete code anchor:

- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:147`: `Track order submission timestamps`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md:153`: `On every order_submitted, append timestamp to deque`
- `execution/engine/main.py:1278`: `if pending_order_ids:`
- `execution/engine/main.py:1280`: `"cycle_skipped_pending_prior_submission",`
- `execution/engine/main.py:1949`: `self.journal.append(`
- `execution/engine/main.py:1950`: `"order_submitted",`
- `execution/engine/main.py:1963`: `self._record_rapid_fire_submission(`

Safety reasoning:

§3 is an order-submission breaker, not an intent-attempt counter. §2 dedup suppresses broker submits before the broker call, so there is no `order_submitted` event and no broker-side rapid-fire exposure to count. Counting dedup skips as submissions would change §3's named defense from "submitted orders at ~2/second" to "strategy decisions at ~2/second", which is outside §3 and would create false trips when §2 is already fail-closed.

Existing test coverage:

- `tests/test_engine_order_dedup.py:221` tests D1. It seeds a pending prior submission, runs a tick, asserts `orders_submitted == 0`, asserts no connector submit, and asserts `cycle_skipped_pending_prior_submission`.
- `tests/test_engine_rapid_fire_breaker.py:259` tests R1 on successful broker submissions. It is deliberately keyed to `order_submitted` history, matching §3 lines 147 and 153.

Why this is not a §3 named-bug gap:

The named incident was excessive broker submissions. Dedup-suppressed attempts are not broker submissions and are already blocked by §2. Replay of `cycle_skipped_rapid_fire_halt` is also unnecessary for halt persistence because `circuit_breaker_tripped_rapid_fire` persists the halt and `circuit_breaker_cleared` removes it.

## Closure Note

No §3 code change was required from round 1. The implementation commit remains `00e7aea`, full pytest was green before commit (`1555 passed, 1 skipped, 33 subtests passed`), and the post-commit §0 recheck was logged at `2026-05-11T02:03:30.239872+00:00`.

K2B PM checkpoint draft:

§3 closed clean — initial impl `00e7aea`, fixes none, disposition doc this commit. Pytest `1555 passed, 1 skipped, 33 subtests passed`. Codex self-judge rejected 4 round-1 findings, all rationale captured in `.code-reviews/spec-b-section3-round1-response.md`; Kimi round 2 APPROVE. Awaiting architect audit.
