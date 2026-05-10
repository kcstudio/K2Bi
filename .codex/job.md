# Codex Job: spec-b-engine-discipline-cleanup

Generated: 2026-05-10 21:15 CST
Target repo: /Users/keithmbpm2/Projects/K2Bi
Ship manager: Mastra Code session
Route decision: Codex builder because this is safety-sensitive K2Bi capital-path work touching engine submission, recovery, journaling, gateway discipline, and review policy.

## Goal

Implement Spec B for K2Bi after incident E-2026-05-09-001. Ship the four P0 engine defenses, the MasterClientID follow-up documentation, and the deferred discipline findings selected for this pass. Keep the engine disabled until all preconditions pass. Do not process new strategy approvals until Spec B lands.

Status of this amendment: prerequisite/spec-only. This commit does not implement Spec B §1-§6.

## Hard prerequisite

Before any Spec B code commit lands, verify the operator-only §0 recheck is recorded AND fresh:

- Operator runs `scripts/gateway-query.sh -f <snippet.py>` with clientId=99
- Expected broker state: G qty=71 shares; cost basis ≤ 0.5% deviation from $32.8295 baseline is informational and must be recorded; cost basis > 0.5% or any qty mismatch = STOP and investigate corporate action; exactly 1 open G order, SELL STP 71 @ $30 GTC, permId 1981941503, clientId=98; no orphans
- `k2bi-engine` systemd unit is `inactive` AND `disabled`
- Operator writes one `wiki/log.md` line via `scripts/wiki-log-append.sh` containing broker state, observed avgCost, explicit no-orphans result, and `k2bi-engine.service inactive AND disabled`
- **Freshness:** the most recent §0 line in `wiki/log.md` must be within 24h from the verify timestamp AND before the next NYSE regular-session open (09:30 America/New_York on the next exchange trading day), whichever comes first. If stale, run a fresh §0 first.

If the line is absent, stale, or the state drifted, stop and return a prerequisite failure. Do not run live broker queries from this coding session.

## Source material

- `AGENTS.md`
- `CLAUDE.md`
- `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md`
- `wiki/concepts/feature_k2bi-discipline-cleanup.md`
- `execution/strategies/runner.py`
- `execution/engine/main.py`
- `execution/engine/recovery.py`
- `execution/connectors/ibkr.py`
- `execution/connectors/types.py`
- `execution/journal/schema.py`
- `execution/journal/writer.py`
- `scripts/gateway-query.sh`
- `.claude/skills/invest-ship/SKILL.md`
- `.gitignore`

## Current working tree notes

At handoff time, these files were already dirty or untracked:

- Modified by ship manager for locked F4/F7 decisions:
  - `wiki/concepts/feature_k2bi-discipline-cleanup.md`
- New Spec B source material:
  - `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md`
- Existing user or prior-session untracked paths. Preserve them unless the user explicitly routes them into this job:
  - `.claude/skills/invest-memo/`
  - `handoffs/`

Run `git status --short --branch` first and preserve unrelated user changes.

## Files in scope

Engine and tests:

- `execution/engine/main.py`
- `execution/strategies/runner.py`
- `execution/engine/recovery.py`
- `execution/connectors/ibkr.py`
- `execution/connectors/types.py`
- `execution/journal/schema.py`
- `execution/journal/writer.py`
- `execution/validators/config.yaml`, only if needed for the rapid-fire threshold block. Inspect Check C before touching it. Do not bypass the proposal gate.
- `tests/test_engine_main.py`
- `tests/test_engine_recovery.py`
- `tests/test_journal.py`
- `tests/test_journal_v2.py`
- New focused tests under `tests/` if cleaner than expanding giant files.

Discipline and docs:

- `.gitignore`
- `.claude/skills/invest-ship/SKILL.md`
- `scripts/gateway-query.sh`
- `scripts/lib/clientid_allocator.py`
- Tests for gateway discipline under `tests/`
- `CLAUDE.md`
- `wiki/context/context_ibkr-secondary-user-vps.md`
- `wiki/concepts/feature_k2bi-discipline-cleanup.md`
- `DEVLOG.md`

Vault memory for F7, if accessible and approved by repo rules:

- `~/Projects/K2Bi-Vault/System/memory/self_improve_learnings.md`
- `~/Projects/K2Bi-Vault/wiki/context/policy-ledger.jsonl`

If vault memory is inaccessible, stop and surface that blocker. Do not fake the F7 update in a repo-only file.

## Out of scope

- Re-enabling the engine with `sudo systemctl enable --now k2bi-engine`.
- Deleting or editing `~/Projects/K2Bi-Vault/System/.killed`.
- Live broker queries from the coding agent. Use only operator-recorded evidence.
- New strategy approvals.
- Phase 3.8b first paper trade.
- Phase 3.10 5-day burn-in.
- Engine snapshot pipeline implementation. Spec B only writes F2/F5 pointers.
- Kill-switch semantic changes.
- Auto-flatten on rapid-fire trip.
- Direct session-side `ib_async` connections.

## Required read order

1. `git status --short --branch`
2. `AGENTS.md`
3. `CLAUDE.md`
4. `proposals/2026-05-10_spec-b-engine-discipline-cleanup.md`
5. `wiki/concepts/feature_k2bi-discipline-cleanup.md`
6. Engine submission path: `execution/engine/main.py`, especially `_process_strategies`, `_submit`, `_poll_awaiting`, recovery init, and breaker flow.
7. Strategy runner path: `execution/strategies/runner.py`.
8. Connector bracket path: `execution/connectors/ibkr.py`.
9. Journal schema and writer.
10. Existing relevant tests.

## Locked decisions

- F4: option A. Drop `review/` from `.gitignore` and stage proposals normally. Do not add a Syncthing read path for Check C.
- F7: augment L-2026-05-08-002 with concrete examples. Do not demote it to a hypothesis.
- MasterClientID: 99.
- Position-aware skip: strict skip-at-or-above-target. Partial position also skips. Do not top up.

## Implementation constraints

- Follow existing Python 3.12 style and unittest plus pytest test patterns.
- No new runtime dependencies.
- Fail closed on safety paths.
- Preserve cash-only invariant. Do not bypass validator cascade or the engine sell-side pre-submit backstop.
- Keep engine defenses before broker submit. Defense logic must run before `connector.submit_order()`.
- The normal cycle must not call the recovery-only protective-stop attachment verb.
- New journal event types must be accepted by schema validation and covered by tests.
- If touching `execution/validators/config.yaml`, satisfy the existing limits-proposal gate. Do not use `K2BI_ALLOW_CONFIG_EDIT=1` unless the operator explicitly approves it.
- Commit red tests first for each defense, then implementation. Use the spec's commit pattern unless repo hooks require a different trailer.
- No doc-only Codex skip for F3 or any architectural rule change.

## Spec B work units

### §1 Position-aware skip

Before BUY submit, query broker positions for the symbol. If current qty > 0, skip under strict semantics, including partial and at-or-above-target positions. If current qty is zero, permit the normal pre-submit flow to continue. If the position query errors, skip and alert via journal detail.

Required journal events:

- `cycle_skipped_position_at_target`
- `cycle_skipped_position_query_failed`

Required tests:

- G1 existing position blocks BUY.
- G2 zero position permits BUY.
- G3 partial position skips, no top-up.
- G4 position query failure fails closed.

### §2 Order dedup via journaled order_id

Before BUY submit, block duplicate submit when a prior `(strategy_id, symbol)` has a non-terminal broker order id. Rebuild in-memory state on startup from journal replay. Update state on submission and terminal events.

Required journal events:

- `cycle_skipped_pending_prior_submission`
- `order_terminal`

Required tests:

- D1 pending order blocks submit.
- D2 terminal filled does not block.
- D3 terminal rejected does not block.
- D4 map rebuilds on startup.
- D5 cross-strategy isolation.

### §3 Rapid-fire circuit breaker

Track order submission timestamps per `(strategy_id, symbol)` in a rolling window. Default threshold: trip on more than 3 orders in 60 seconds. Halt further submits for that key. Operator clears through `.rapid-fire-cleared.json`. Malformed sentinel fails closed and does not clear.

Required journal events:

- `circuit_breaker_tripped_rapid_fire`
- `circuit_breaker_cleared`
- `cycle_skipped_rapid_fire_halt`

Required tests:

- R1 four orders in 10 seconds trips.
- R2 three in 60 seconds does not trip.
- R3 four over 90 seconds does not trip.
- R4 cross-strategy isolation.
- R5 sentinel clears halt.
- R6 malformed sentinel rejected.
- R7 halt persists across restart.

### §4 Child-stop attachment via parentId

Verify and strengthen the bracket behavior already present in `execution/connectors/ibkr.py`: BUY parent uses `transmit=False`, child STP uses `parentId=parent.orderId` and `transmit=True`. Normal cycle must not place standalone stops.

Add an explicit recovery-only verb for existing positions. It must assert broker position qty exactly matches requested qty before placing a standalone STP with `parentId=0` and `transmit=True`.

Required journal event:

- `protective_stop_attached_to_existing_position`

Required tests:

- C1 BUY with stop creates parent plus child with parentId, no standalone STP.
- C2 parent cancellation behavior is documented or simulated in connector-level mock tests.
- C3 recovery-only verb refuses on position drift.
- C4 recovery-only verb succeeds on exact qty match.
- C5 static-grep test proves normal cycle path does not call the recovery-only verb.

### §5 MasterClientID follow-up

No live VPS config change from this coding session. Update docs only:

- `CLAUDE.md` clientId convention: clientId 1 is engine reserved; 90-98 are ad-hoc or backtest; 99 is MasterClientID with read plus cancel privileges across clients.
- `wiki/context/context_ibkr-secondary-user-vps.md`: document Hostinger config path `/home/ibgateway/ibc/config.ini`, `MasterClientID=99`, and the manual test plan.

### §6 Deferred findings

- F1: add `flock`-based clientId allocator in `scripts/lib/clientid_allocator.py`; `scripts/gateway-query.sh` calls it on entry and releases on exit.
- F2: write pointer only to the engine-vault-snapshots build session. Do not implement snapshot pipeline.
- F3: amend `.claude/skills/invest-ship/SKILL.md` Checkpoint 2 exception clause to exclude new architectural principles, conventions, or invariants from doc-only skip.
- F4: implement locked option A by dropping `review/` from `.gitignore`.
- F5: write pointer only to the engine-vault-snapshots build session.
- F6: add runtime caller-context guard to `scripts/gateway-query.sh` so skill misuse cannot silently call it.
- F7: augment L-2026-05-08-002 with IBKR migration liveness and Syncthing liveness examples.

## Test plan

Run focused tests after each work unit, then the full suite at the end.

Suggested focused commands:

```bash
pytest tests/test_engine_main.py -q
pytest tests/test_engine_recovery.py -q
pytest tests/test_journal.py tests/test_journal_v2.py -q
pytest tests/test_pre_commit_hook.py tests/test_commit_msg_hook.py -q
pytest tests/test_post_commit_hook.py -q
pytest tests/ -q
```

If adding new test files, run them directly before the broader commands.

For shell scripts, add or update tests rather than relying only on manual runs. If a script has no harness coverage, use a temp HOME and temp repo where possible. Do not connect to the real gateway.

## Review and ship contract

Codex is the builder. Official adversarial review must be Kimi-backed or another non-Codex reviewer. Do not accept Codex self-review as the official gate.

Preferred review command for this repo because `scripts/review.sh` can fall back to Codex when primary is minimax:

```bash
CHANGED_FILES="$(git diff --name-only HEAD -- | paste -sd, -)"
scripts/minimax-review.sh --scope diff --files "$CHANGED_FILES" --focus "Spec B engine discipline cleanup, incident E-2026-05-09-001, P0 order-safety defenses" --json
```

If you use `scripts/review.sh --primary minimax --wait`, inspect the resulting state or log. If it falls back to Codex, stop and report that the official review is unavailable.

Before commit, run `git diff --check`, focused tests, full `pytest tests/`, and the Kimi-backed review. Append `DEVLOG.md` when the implementation is ready to ship.

## Return format

When done, report:

- Summary of behavior changed.
- Files changed.
- Tests run with pass or fail results.
- Review result and reviewer used.
- Commit, push, deploy status if performed.
- Any blockers, especially missing §0 operator verification, vault access, or live-state drift.
