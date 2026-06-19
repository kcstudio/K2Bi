# Stage 15 Trade Retro MVP Spec

## Objective

Build the missing K2Bi Stage 15 retro capability for K2B orchestrator Phase B:
read a closed trade from `raw/journal/*.jsonl`, draft a structured retro, and
log one concrete lesson to `/learn`.

## Exact MVP Behavior

Add a deterministic local command:

```bash
python3 -m scripts.lib.invest_trade_retro run \
  --strategy <strategy-id> \
  --vault-root <K2Bi-Vault> \
  --repo-root <K2Bi repo> \
  --as-of 2026-06-16
```

Behavior:

1. Read only `<vault-root>/raw/journal/*.jsonl`.
2. Find the newest `strategy_stopped_out` event for `--strategy` by parsed
   close timestamp, falling back to scan order only for ties.
3. Join the newest strictly prior `order_filled` buy event for the same strategy
   by parsed fill timestamp when present. Equal fill and close timestamps are
   not prior.
4. Emit a structured retro JSON object to stdout.
5. Write a Markdown retro to
   `<vault-root>/wiki/insights/YYYY-MM-DD_trade-retro_<strategy-id>.md`.
6. Append exactly one `/learn` entry to
   `<vault-root>/System/memory/self_improve_learnings.md`.
7. Be idempotent by closure event id: re-running for the same
   `strategy_stopped_out.journal_entry_id` returns the existing retro and does
   not append a duplicate learning, even if `--as-of` is later.

The generated retro must name at least one concrete change. MVP rule:

- If entry fill exists and the stop-out exit is below entry, recommend reviewing
  entry timing and stop distance before re-approval.
- If entry fill exists and the stop-out exit is at or above entry, recommend
  documenting whether the stop was loss-protection or profit-protection before
  re-approval.
- If entry fill is missing, recommend adding complete entry-fill linkage before
  using the retro for P&L (profit and loss) judgment.

## Closed-Trade Source

Use a test fixture under `tests/fixtures/stage15_journal/`, not real CDNS.

The fixture should model a closed stopped-out trade with:

- one `order_filled` buy event
- one `order_terminal` filled event
- one later `strategy_stopped_out` event

Do not block on a real CDNS fill or exit. Do not use live broker state. The
fixture can be patterned after the existing G stopped-out journal shape, but it
must be synthetic test data.

## Retro Output Schema

The command stdout JSON and Markdown frontmatter should share this schema:

```json
{
  "retro_version": 1,
  "retro_date": "2026-06-16",
  "strategy_id": "fixture-stage15-breakout",
  "ticker": "TST",
  "source": {
    "closure_event_type": "strategy_stopped_out",
    "closure_journal_entry_id": "01RETROEXIT000000000000000",
    "journal_files": ["raw/journal/2026-06-16.jsonl"],
    "closure_source_file": "raw/journal/2026-06-16.jsonl",
    "closure_source_line": 3,
    "closure_source_index": 2,
    "entry_source_file": "raw/journal/2026-06-16.jsonl",
    "entry_source_line": 1,
    "entry_source_index": 0
  },
  "entry": {
    "found": true,
    "journal_entry_id": "01RETROENTRY0000000000000",
    "filled_at": "2026-06-10T14:00:00+00:00",
    "side": "buy",
    "qty": 10,
    "price": "100.00",
    "trade_id": "01RETROTRADE000000000000"
  },
  "exit": {
    "closed_at": "2026-06-16T15:00:00+00:00",
    "exit_type": "stopped_out",
    "price": "92.00",
    "fill_perm_id": 123456789
  },
  "outcome": {
    "realized_pnl_usd": "-80.00",
    "return_pct": "-8.00",
    "holding_days": 6
  },
  "concrete_changes": [
    {
      "target": "strategy",
      "change": "Review entry timing and stop distance before re-approval.",
      "rationale": "The stopped-out exit closed below the recorded entry fill.",
      "priority": "medium"
    }
  ],
  "learn": {
    "learning_id": "L-2026-06-16-001",
    "path": "System/memory/self_improve_learnings.md",
    "distilled_rule": "After a stopped-out trade, review entry timing and stop distance before re-approving the same strategy.",
    "policy_ledger_added": false
  },
  "retro_path": "wiki/insights/2026-06-16_trade-retro_fixture-stage15-breakout.md"
}
```

`source.journal_files` records every `raw/journal/*.jsonl` file scanned for the
retro, not only the entry and closure files that matched the trade. The schema
example uses one file because the MVP fixture is single-file. The
`*_source_file`, `*_source_line`, and `*_source_index` fields identify the exact
matched entry and closure events.

## `/learn` Feed

The MVP feeds `/learn` by appending a normal learning entry to
`System/memory/self_improve_learnings.md` using the existing
`invest-feedback` format:

```markdown
## 2026-06-16 -- Trade retro: fixture-stage15-breakout stopped out

### L-2026-06-16-001
distilled-rule: "After a stopped-out trade, review entry timing and stop distance before re-approving the same strategy."

- **Area:** workflow
- **Distilled rule:** After a stopped-out trade, review entry timing and stop distance before re-approving the same strategy.
- **Learning:** ...
- **Context:** Stage 15 trade retro for `fixture-stage15-breakout`; source closure journal entry `01RETROEXIT000000000000000`.
- **Source:** stage15-retro `01RETROEXIT000000000000000`
- **Reinforced:** 1
- **Confidence:** low
- **Date:** 2026-06-16
- **Status:** pending
```

Do not add a policy-ledger guard in the MVP. One closed trade should create a
low-confidence learning, not a hard guard. A later operator-reviewed promotion
can turn repeated trade-retro lessons into active rules or policy-ledger guards.

The retro writer uses `System/memory/.stage15_trade_retro.lock` while scanning
the journal, scanning for existing retros, repairing partial states, appending
`/learn`, and writing the retro note. If a prior run left a retro without a
learning, the next run repairs the missing learning while preserving the
original retro date/path.

Malformed JSON lines in unrelated journal files are skipped with a warning so
one partial/bad line cannot block retros for other closed trades. Non-object
JSON lines are skipped the same way. Entry quantity conflicts or non-numeric
entry quantities refuse the retro before writing any artifact. Retro Markdown
writes are constrained to `wiki/insights/`, including refusal when that
directory resolves through a symlink outside the vault.

The lock is POSIX `flock` on the local vault filesystem. The MVP verifies
cross-process exclusion on this local machine, but it is not a distributed lock
for NFS or multi-host writes. Lock acquisition uses bounded exponential backoff
with jitter. The write sequence is retro-first, then `/learn`; if the learning
write fails, the next run repairs the missing learning from the existing retro
using the retro-embedded learning id. The lock file is created owner-only and
symlink lock paths are refused with atomic `O_NOFOLLOW` creation where the
platform supports it. Learning-write gaps leave a
`.stage15_pending_<closure-id>.json` marker until the repair succeeds.
Journal scans fail hard above 100 MB or 1,000,000 lines per JSONL file.

Before writing Markdown, the helper validates the structured retro shape for the
MVP-required keys, ticker presence, and price field types. Retro writes also
refuse symlinked vault roots and symlinked `wiki/insights` paths.

## Files And Modules Likely Touched

- Create `scripts/lib/invest_trade_retro.py`
  - journal reader
  - retro builder
  - Markdown writer
  - learning appender
  - CLI
- Create `tests/test_invest_trade_retro.py`
  - fixture-driven red/green tests
- Create `tests/fixtures/stage15_journal/2026-06-16.jsonl`
  - synthetic closed trade fixture
- Create `.agents/skills/invest-retro/SKILL.md`
- Create `.claude/skills/invest-retro/SKILL.md`
  - keep Codex and Claude skill surfaces in parity
- Update `DEVLOG.md` only during ship, not during the MVP implementation pass.

## Tests To Add First

1. `test_run_writes_retro_and_learning_from_closed_trade_fixture`
   - Builds a temp vault with fixture journal + memory file.
   - Runs the module.
   - Asserts stdout schema has `concrete_changes`.
   - Asserts retro Markdown exists.
   - Asserts one learning entry exists with `Source: stage15-retro <closure id>`.

2. `test_run_is_idempotent_for_same_closure_event`
   - Runs the module twice.
   - Asserts only one retro file and one learning entry exist.
   - Covers re-running with a later `--as-of` and reusing the existing retro for
     the same closure event.

3. `test_run_refuses_when_no_closed_trade_exists`
   - Fixture has only an entry fill.
   - Asserts non-zero exit and no learning write.

4. `test_missing_entry_fill_still_retroes_but_marks_pnl_unknown`
   - Fixture has only `strategy_stopped_out`.
   - Asserts `entry.found=false`, `outcome.realized_pnl_usd=null`, and a
     concrete data-quality change.

5. `test_source_journal_files_include_all_scanned_jsonl`
   - Adds an unrelated journal file.
   - Asserts provenance includes all scanned JSONL files.

6. `test_missing_prices_remain_null_not_string_none`
   - Removes entry and exit fill prices from the fixture.
   - Asserts the structured retro uses JSON null, not the string `"None"`.

7. `test_run_refuses_closed_trade_without_closure_ticker`
   - Removes the close-event ticker.
   - Asserts no retro or learning is produced from an ambiguous closure event.

8. `test_existing_retro_missing_learning_repairs_without_new_retro_date`
   - Deletes the learning after a first run.
   - Asserts the next run repairs `/learn` without rewriting the retro under a
     later date.

9. `test_conflicting_entry_quantities_refuse_retro`
   - Creates a fixture where `record.qty` and `payload.fill_qty` disagree.
   - Asserts no retro or learning is produced from inconsistent fill data.

10. `test_malformed_unrelated_journal_line_is_skipped`
    - Adds a malformed unrelated JSONL line.
    - Asserts the target retro still runs and includes the scanned file in
      provenance.

11. `test_non_numeric_entry_quantity_refuses_retro`
    - Sets both quantity fields to a non-numeric value.
    - Asserts no retro or learning is produced.

12. `test_retro_write_refuses_path_outside_insights`
    - Attempts to write a retro path outside `wiki/insights`.
    - Asserts the write is refused.

13. `test_run_is_idempotent_across_processes`
    - Launches multiple Python processes against the same fixture.
    - Asserts one retro and one learning entry are produced.

14. `test_existing_retro_learning_id_mismatch_is_repaired`
    - Simulates a learning entry whose id differs from the retro JSON.
    - Asserts the retro JSON and frontmatter are repaired.

15. `test_existing_retro_with_malformed_date_refuses_repair`
    - Corrupts an existing retro's `retro_date` and removes the learning.
    - Asserts repair refuses rather than minting a new dated learning id.

16. `test_newest_closure_is_selected_by_timestamp_not_file_order`
    - Adds an older close event in a lexicographically later journal file.
    - Asserts the newer close timestamp wins.

17. `test_run_refuses_closed_trade_without_closure_timestamp`
    - Removes both closure timestamps.
    - Asserts no retro or learning is produced.

18. `test_retro_write_refuses_symlinked_insights_directory`
    - Symlinks `wiki/insights` outside the vault.
    - Asserts no retro or learning is produced.

19. `test_entry_fill_selected_by_timestamp_not_file_order`
    - Places an earlier fill in a lexicographically later journal file.
    - Asserts the entry is joined by timestamp.

20. `test_learning_rolls_back_if_retro_write_fails`
    - Forces the retro write to fail after learning allocation.
    - Asserts the learning file is unchanged because the retro write happens
      before `/learn`.

21. `test_equal_timestamp_entry_is_not_prior_fill`
    - Gives the entry and close the same timestamp.
    - Asserts the entry is not joined because it is not strictly prior.

22. `test_single_source_non_numeric_entry_quantity_refuses_retro`
    - Leaves only one quantity field present and makes it non-numeric.
    - Asserts no retro or learning is produced.

23. `test_existing_retro_repairs_learning_after_learning_write_failure`
    - Forces `/learn` write failure after retro write.
    - Asserts the next run repairs the missing learning.

24. `test_retro_write_refuses_symlinked_vault_root`
    - Runs through a symlinked vault root.
    - Asserts no retro or learning is produced.

25. `test_retro_schema_validation_requires_price_keys`
    - Removes `entry.price` from the structured retro.
    - Asserts the Markdown write refuses schema drift.

26. `test_retro_schema_validation_requires_ticker`
    - Removes the ticker value from the structured retro.
    - Asserts the Markdown write refuses schema drift.

27. `test_entry_fill_matches_payload_strategy_id`
    - Removes the entry fill's top-level strategy field while preserving
      `payload.strategy_id`.
    - Asserts entry linkage uses the same strategy extractor as closure
      matching.

28. `test_string_none_entry_price_refuses_retro`
    - Sets the entry fill price to the literal string `"None"`.
    - Asserts no retro or learning is produced.

29. `test_non_finite_exit_price_refuses_retro`
    - Sets the exit fill price to `NaN`.
    - Asserts no retro or learning is produced.

30. `test_lock_file_with_extra_hard_link_refuses_retro`
    - Creates a second hard link to the local lock file.
    - Asserts the retro transaction refuses to run.

31. `test_repair_learning_not_written_if_retro_rewrite_fails`
    - Corrupts an existing retro so learning repair requires a retro rewrite.
    - Forces the retro rewrite to fail and asserts `/learn` remains unchanged
      and the pending marker is cleared.

32. `test_pending_marker_remains_if_learning_write_does_not_persist`
    - Simulates a learning writer that returns without persisting the expected
      source entry.
    - Asserts the run refuses and leaves the pending marker for repair.

33. `test_existing_learning_without_retro_is_reused`
    - Seeds `/learn` with the closure source but no retro file.
    - Asserts the retro is rebuilt with the existing learning id and no
      duplicate learning entry.

## Verification Commands

```bash
pytest tests/test_invest_trade_retro.py
pytest tests/test_journal.py tests/test_strategy_frontmatter.py tests/test_invest_ship_strategy.py
pytest
```

Before `/ship`, run adversarial review with Codex as the builder family:

```bash
BUILDER_FAMILY=openai scripts/review.sh files \
  --primary minimax \
  --wait \
  --files "scripts/lib/invest_trade_retro.py tests/test_invest_trade_retro.py .agents/skills/invest-retro/SKILL.md .claude/skills/invest-retro/SKILL.md"
```

The reviewer key `minimax` routes to Kimi by default per K2B/K2Bi provider
policy. The review counts only if the returned state shows
`primary_used=minimax` and `fallback_used=false`; same-family fallback is not an
official review.

## Explicit Non-Goals

- No live broker mutation.
- No broker reads through `ib_async`, `scripts/gateway-query.sh`, or any
  broker connector.
- No waiting for real CDNS fill.
- No waiting for real CDNS exit.
- No engine lifecycle edits.
- No strategy approval, re-approval, retirement, or stop-out frontmatter edits.
- No validator changes.
- No policy-ledger guard promotion from a single trade retro.
- No automatic PR merge, commit, or push in the implementation pass.

## Ambiguity Check

The implementation path is clear for MVP:

- source = synthetic closed-trade journal fixture
- closed-trade detector = `strategy_stopped_out`
- retro writer = vault Markdown under `wiki/insights`
- `/learn` feed = append to `System/memory/self_improve_learnings.md`
- safety boundary = read-only journal/vault read plus memory/retro artifact write,
  no broker or engine mutation
