---
tags: [proposal, spec-b, engine, durability, reconciliation, section-8]
date: 2026-05-12
type: proposal
origin: k2b-generate
up: "[[../wiki/planning/index]]"
status: drafted
authored-by: K2B-architect (PM session)
authored-at: 2026-05-12 01:30 HKT
gates-engine-reenable: yes
---

# Spec B §8 — Post-Fill Durability and Reconciliation Symmetry

## Purpose

Close the architectural gap surfaced by the 2026-05-11 regression test: an engine state where `self._pending_order` was nulled WITHOUT a corresponding `order_terminal` event reaching disk. Static code audit (2026-05-12 by Codex) found no single production code path that produces this state cleanly, but identified two structural gaps: (a) no write-side durability assertion guarantees `order_terminal` is on disk before `_pending_order` is nulled; (b) no read-side rebuild path lets the singular `_pending_order` self-heal from a journal missing the terminal event, even though the plural `_pending_orders` map DOES self-heal. §8 closes both gaps + adds the runner-side observability event the regression test needs.

## Tech stack

Existing Python 3.12 + ib_async + JSONL journal + pytest. No new runtime deps.

## Architecture

Three defenses, layered into the existing engine main loop and journal writer:

- §8.1 **Write-side durability** — after every high-stakes journal write (`_journal_order_terminal`, optionally `_journal_order_timeout` and others), read back the last journal line and assert it matches the event just written. On mismatch: raise `JournalDurabilityError`. The caller refuses to mutate state (does NOT null `_pending_order`); engine logs and exits cleanly via existing shutdown path.
- §8.2 **Read-side reconciliation symmetry** — on engine recovery, for every awaiting `_pending_order`, scan the journal for the trade_id's terminal signal. Accept any of (`order_terminal`, `order_filled remaining_qty=0`, `order_timeout`). If found, null `_pending_order` at recovery completion. Mirrors the plural map's existing rebuild path at `runner.py:204-303`.
- §8.3 **Runner-side observability event** — at `runner.py:139` where `SKIP_POSITION_HELD` is returned, journal a `cycle_evaluated_skip_position_held` event with payload `(strategy, symbol, current_qty, target_qty, cycle_id, evaluation_timestamp)`. External observers (regression tests, m2.9 alerts) gain visibility into steady-state "engine cycled and skipped due to position held."

Defenses applied in order: §8.1 prevents the gap on the write path; §8.2 makes the engine self-healing against any residual case §8.1 misses; §8.3 closes the observability gap that hid the actual cycle behavior from the regression test.

## 0. Prerequisite

- Spec B §1-§7 closed (DONE, final commit `f069d6b`).
- Engine re-enable gate E-2026-05-09-001 binding (DONE, in `K2Bi-Vault/System/memory/active_rules.md` § "Engine re-enable gate").
- Diagnostic + Verification + Targeted Audit ALL on record at `K2Bi-Vault/wiki/insights/2026-05-11_post-fill-cycle-loop-silence-diagnostic.md`.
- Fresh §0 broker-state verify within 24h before any §8 commit lands. §0 freshness window per Spec B §0 amendment carries forward unchanged.
- Engine remains `inactive` + `disabled` + `.killed` present throughout §8 build. No re-enable until §8.1+§8.2 land clean + re-run regression test passes per amended Phase D.2 + D.3.

## 1. Defense 1 — Write-Side Durability Read-Back (§8.1)

### Defect

Production state on 2026-05-11 showed `engine_stopped` with `pending_order: null` but NO `order_terminal{Filled}` event on disk for `broker_order_id=77`. Audit identified two write sites that null `_pending_order` (`main.py:2139` in `_reconcile_fill`, `main.py:2278` in `_reconcile_terminal`), both preceded by unconditional journal writes for the terminal event. Yet the disk shows the null happened without the journal write surviving. Three possible root causes (concurrent race, fsync gap under specific kernel state, third-path bug) — all distinct, all undetectable from current static state. Common defense: a read-back assertion makes the gap impossible regardless of root cause.

### Fix

In `execution/journal/writer.py` (or wherever `journal.append` lives), add a `read_back_last_event() -> dict` method that:

- Reads the last newline-terminated line of the current journal file
- Parses it as JSON
- Returns the parsed event dict

In `execution/engine/main.py` (or wherever `_journal_order_terminal` is defined), wrap the write:

```python
def _journal_order_terminal(self, *, strategy, trade_id, ticker, broker_order_id, terminal_status, ...):
    event_payload = {...}  # existing payload assembly
    self.journal.append("order_terminal", payload=event_payload, ...)
    # NEW: durability assertion
    last = self.journal.read_back_last_event()
    if last.get("event_type") != "order_terminal" or last.get("trade_id") != trade_id:
        raise JournalDurabilityError(
            f"order_terminal write durability check failed: "
            f"expected event_type=order_terminal trade_id={trade_id}, "
            f"got event_type={last.get('event_type')} trade_id={last.get('trade_id')}"
        )
```

Add new exception:

```python
class JournalDurabilityError(Exception):
    """Raised when a journal write is not visible on read-back. Triggers clean engine shutdown."""
```

The caller paths that null `_pending_order` (lines 2139 and 2278) execute `_journal_order_terminal` BEFORE the null. If `_journal_order_terminal` raises `JournalDurabilityError`, the null never executes, the exception propagates up through `_run_tick_body` (already-existing path), and the engine main loop catches it at the same handler that catches `DisconnectedError` / `ConnectorError` (`main.py:1177` area) plus a new branch for `JournalDurabilityError` that triggers `_shutdown(reason="journal_durability_failure")`. The next engine start will recover via §8.2.

### Tests

`tests/test_engine_journal_durability.py`:

1. **D8.1-1 (red-then-green): durability check raises on mismatch.** Mock `journal.append` to write the event but `read_back_last_event` to return a different event (e.g. a stale prior event). Run `_reconcile_fill` simulating an order_filled with remaining_qty=0. Expect `JournalDurabilityError` raised; `self._pending_order` remains non-null; no further state mutations. Pre-fix this test must FAIL (no durability check exists; null happens regardless).
2. **D8.1-2: normal path succeeds cleanly.** Mock journal.append + read_back to be self-consistent (real implementation). Run `_reconcile_fill`. Expect order_terminal on disk; `_pending_order` nulled; no exception.
3. **D8.1-3: journal.append raises directly (existing behavior preserved).** Mock journal.append to raise. Expect exception propagates as before; null never happens (unchanged from current behavior).
4. **D8.1-4: durability check applied to _reconcile_terminal path too.** Same as D8.1-1 but for the second null site at `main.py:2278`. Both write paths must be guarded.
5. **D8.1-5: engine shutdown on JournalDurabilityError.** Run a full engine cycle that triggers the error. Expect engine_stopped event with `reason: "journal_durability_failure"` and `pending_order: <trade_id>` (non-null, because durability check refused the null).

## 2. Defense 2 — Read-Side Reconciliation Symmetry (§8.2)

### Defect

The plural `_pending_orders` map (per Spec B §2) self-heals via the rebuild path at `runner.py:204-303` which accepts `order_filled remaining_qty=0` as terminal-equivalent for replay compatibility. The singular `_pending_order` (used for tracking the currently-in-flight engine-submitted order) has NO equivalent rebuild path. On engine restart, if recovery encounters an `awaiting` state but the journal is missing the terminal event for that trade_id, the engine will load `_pending_order` to the in-flight state and never progress — even if the broker confirms the order is long-filled. The 2026-05-11 incident's restart path would have hit this if the operator had restarted the engine instead of stopping it.

### Fix

In the journal reader (likely `execution/journal/reader.py`), add a helper:

```python
def find_terminal_for_trade_id(trade_id: str) -> Optional[dict]:
    """Scan journal for a terminal signal matching trade_id. Returns the event dict or None."""
    for event in reverse_iter_journal_events():  # newest-first scan
        if event.get("trade_id") != trade_id:
            continue
        event_type = event.get("event_type")
        if event_type == "order_terminal":
            return event  # runtime contract terminal
        if event_type == "order_filled" and event.get("payload", {}).get("remaining_qty") == 0:
            return event  # replay-legacy terminal
        if event_type == "order_timeout":
            return event  # timeout terminal
    return None
```

In the engine recovery path (`execution/engine/recovery.py` or equivalent), after building the initial `_pending_order` from awaiting state but BEFORE returning from recovery:

```python
if self._pending_order is not None:
    awaiting_trade_id = self._pending_order.get("trade_id")
    terminal = find_terminal_for_trade_id(awaiting_trade_id)
    if terminal is not None:
        # Self-heal: terminal signal exists, this order is done, null _pending_order
        self.journal.append(
            "recovery_self_healed_pending_order",
            payload={
                "trade_id": awaiting_trade_id,
                "terminal_event_type": terminal.get("event_type"),
                "rationale": "journal-rebuild matched terminal signal not reflected in awaiting state",
            },
            strategy=None,
            trade_id=awaiting_trade_id,
        )
        self._pending_order = None
```

The `recovery_self_healed_pending_order` event creates an audit trail: every time the singular `_pending_order` self-heals, an external observer sees it. This is the canary for §8.1 leaks — if `recovery_self_healed_pending_order` events start appearing in production, that's evidence §8.1's durability check is missing a path.

### Tests

`tests/test_engine_singular_pending_rebuild.py`:

1. **D8.2-1: recovery nulls _pending_order when order_terminal exists.** Seed journal with `order_submitted` + `order_terminal{Filled}` for trade_id=X. Start engine recovery with awaiting state for trade_id=X. Expect `_pending_order` is None post-recovery; `recovery_self_healed_pending_order` event journaled with `terminal_event_type=order_terminal`.
2. **D8.2-2: recovery nulls _pending_order when order_filled remaining_qty=0 exists (no order_terminal).** Replay-legacy compatibility. Seed journal with `order_submitted` + `order_filled remaining_qty=0` for trade_id=X (matching the 2026-05-11 disk shape). Start engine recovery. Expect `_pending_order` is None post-recovery; `recovery_self_healed_pending_order` event journaled with `terminal_event_type=order_filled`. This is the test that would have caught the 2026-05-11 failure at startup time.
3. **D8.2-3: recovery preserves _pending_order when only order_submitted exists.** Seed journal with `order_submitted` only for trade_id=X. Start engine recovery. Expect `_pending_order` IS set to in-flight state; no self-heal event journaled; engine proceeds to poll for terminal status.
4. **D8.2-4: recovery nulls _pending_order when order_timeout exists.** Seed `order_submitted` + `order_timeout`. Expect `_pending_order` null + self-heal event with `terminal_event_type=order_timeout`.
5. **D8.2-5 (red-then-green): pre-fix behavior fails this scenario.** Same setup as D8.2-2 (replay-legacy shape). Pre-§8.2 implementation must FAIL the test (the engine would set `_pending_order` to in-flight and poll forever); post-§8.2 must PASS (engine self-heals).

## 3. Defense 3 — Runner-Side Observability Event (§8.3)

### Defect

At `runner.py:139`, when `strategy_runner.evaluate()` returns `SKIP_POSITION_HELD` (because broker positions already match strategy target), the runner is silent — no journal event emitted. The 2026-05-11 regression test required `cycle_skipped_existing_position` on every post-fill cycle, but that event is emitted ONLY by `_skip_buy_for_existing_position` (the §1 last-line-defense path), which is downstream of the runner gate and never reached in steady-state post-fill. External observers cannot distinguish "engine is alive and skipping correctly" from "engine is silently stuck." This is a Phase 3.10 burn-in observability problem regardless of root cause.

### Fix

At `execution/strategies/runner.py:139` (the line returning `SKIP_POSITION_HELD`), add:

```python
self.journal.append(
    "cycle_evaluated_skip_position_held",
    payload={
        "strategy_id": strategy.name,
        "ticker": strategy.ticker,
        "current_qty": ctx.positions.get(strategy.ticker, 0),
        "target_qty": strategy.order.qty,
        "cycle_id": ctx.cycle_id,
        "evaluation_timestamp": ctx.now.isoformat(),
    },
    strategy=strategy.name,
    trade_id=None,
    ticker=strategy.ticker,
)
```

### Tests

`tests/test_runner_observability.py`:

1. **D8.3-1 (red-then-green): SKIP_POSITION_HELD path emits observability event.** Mock runner with `ctx.positions = {"G": 71}` and strategy target_qty=71. Run `evaluate()`. Expect `cycle_evaluated_skip_position_held` event journaled with correct payload. Pre-fix must FAIL (no journal write at runner.py:139).
2. **D8.3-2: zero-position path does NOT emit the event.** Mock runner with `ctx.positions = {}`. Run `evaluate()`. Expect NO `cycle_evaluated_skip_position_held` event (the runner doesn't gate; flow proceeds to candidate generation).
3. **D8.3-3: partial-position path emits event.** Mock with `ctx.positions = {"G": 30}` (STRICT semantics: skip on any non-zero). Expect event journaled with `current_qty=30, target_qty=71`.

## 4. Regression test plan amendment (§8 closure dependency)

Amend `K2Bi-Vault/proposals/2026-05-10_post-spec-b-regression-test-plan.md`:

**Phase D.2 (replace existing):**
> Expected on EVERY cycle: `cycle_evaluated_skip_position_held` event with `payload.current_qty=71, target_qty=71`. No new `order_submitted` for G in any of these cycles. (Note: §1's `cycle_skipped_existing_position` event is NOT expected here — that's a last-line-defense emitter for stale-candidate recovery scenarios, downstream of the runner gate; in normal post-fill steady-state, the runner gate fires first and §1's path is unreached. This was the regression test's original test-design error 2026-05-11.)

**Phase D.4 (new section, durability check):**
> After Phase C completes (post-fill), verify the journal on disk contains BOTH `order_filled` AND a separate `order_terminal{terminal_status=Filled}` event for the BUY's broker_order_id. Both must be present. The presence of `order_terminal` is the runtime contract per §2 test D2b; its absence on disk after a successful fill indicates §8.1's durability check failed silently OR a residual code path bypassed it. If `order_terminal` is missing, the test FAILS regardless of all other criteria — this is the specific regression of the 2026-05-11 failure mode.

**Phase F frontmatter update:** on PASS, frontmatter goes `outcome: pass` AND new field `spec-b-section-validated: 8`. On FAIL with §8-class failure, file new diagnostic note + escalate.

## 5. §7 engine-re-enable-checklist update

Add to `K2Bi/wiki/runbooks/spec-b-engine-reenable-checklist.md`:

- `pytest tests/test_engine_journal_durability.py -v` → all green (§8.1 wired)
- `pytest tests/test_engine_singular_pending_rebuild.py -v` → all green (§8.2 wired)
- `pytest tests/test_runner_observability.py -v` → all green (§8.3 wired)
- Confirm `JournalDurabilityError` exception class exists in `execution/engine/main.py` or equivalent
- Confirm `find_terminal_for_trade_id` helper exists in `execution/journal/reader.py` or equivalent
- Confirm `read_back_last_event` helper exists in `execution/journal/writer.py` or equivalent

## 6. Engine re-enable gate

E-2026-05-09-001 remains BINDING until:

1. §8.1 SHIPS clean (durability read-back + JournalDurabilityError + 5 tests pass)
2. §8.2 SHIPS clean (find_terminal_for_trade_id + recovery self-heal + 5 tests pass)
3. §8.3 SHIPS clean (1 line + 3 tests pass) — REQUIRED for regression test PASS criterion to be checkable
4. Regression test plan amendment applied (Phase D.2 + new D.4 + Phase F frontmatter notes)
5. Re-run of regression test produces PASS per amended criteria
6. K2B-architect audits the §8 disposition docs + verifies all 3 sub-sections cleanly closed

§8.1 + §8.2 are P0 (correctness/safety). §8.3 is P1 (observability) but required for regression test to checkable, so effectively P0 for unblocking Phase 3.10.

## 7. Process expectations (same as Spec B §1-§7)

- Builder = Codex in K2Bi session; Reviewer = Kimi via `scripts/minimax-review.sh`; Architect = K2B PM session on resume.
- Sequencing: §8.1 → §8.2 → §8.3 → regression-test-plan-amendment → re-run. Red test first per spec G4 guardrail.
- 3-round Kimi NEEDS-ATTENTION cap → Codex self-judge with disposition doc at `.code-reviews/spec-b-section8-<N>-round<N>-response.md`.
- Architect escalation triggers (same as before): scope/spec-level question; Codex uncertain on a finding; Kimi raises a finding requiring spec amendment.
- Pre-closure verification gate per section: full pytest green + fresh §0 within 24h + worktree clean.

## 8. Cross-references

- Spec B (§1-§7): `~/Projects/K2Bi/proposals/2026-05-10_spec-b-engine-discipline-cleanup.md`
- 2026-05-11 incident diagnostic (with verification + targeted audit appended): `~/Projects/K2Bi-Vault/wiki/insights/2026-05-11_post-fill-cycle-loop-silence-diagnostic.md`
- 2026-05-11 FAIL evidence: `~/Projects/K2Bi-Vault/wiki/insights/2026-05-11_spec-b-regression-test-failed.md`
- Regression test plan: `~/Projects/K2Bi-Vault/proposals/2026-05-10_post-spec-b-regression-test-plan.md`
- Engine re-enable gate: `~/Projects/K2Bi-Vault/System/memory/active_rules.md` § "Engine re-enable gate (E-2026-05-09-001)"
- §7 runbook: `~/Projects/K2Bi/wiki/runbooks/spec-b-engine-reenable-checklist.md`
- §1 implementation: `K2Bi/execution/engine/main.py:1745` (`_skip_buy_for_existing_position`)
- §2 implementation: `K2Bi/execution/strategies/runner.py:204-303` (plural _pending_orders rebuild)
- Audit-identified write sites: `K2Bi/execution/engine/main.py:2139` (`_reconcile_fill`) + `K2Bi/execution/engine/main.py:2278` (`_reconcile_terminal`)
- Runner gate location: `K2Bi/execution/strategies/runner.py:139` (`SKIP_POSITION_HELD` return)
