# P4-4 Final Disposition

Review log: `.code-reviews/2026-05-31T00-31-43Z_8660e5.log`

Verdict: NEEDS-ATTENTION, not cleared by reviewer

PM ruling: accepted under non-capital-path single-pass discipline. P4-4 remains
display-layer only. The valid malformed-quantity finding was fixed. The
remaining reviewer recommendations would expand P4-4 into alarm redesign,
journal cross-checking, or detection behavior that the kickoff explicitly
forbids.

## Dispositions

1. Held missing-stop should get new conspicuous alarm formatting - REJECT AS
   SCOPE EXPANSION. The P4-4 kickoff required held missing-stop to remain loud,
   specifically preserving `STP $? STP missing`. Adding emoji, exit-code
   changes, anomaly injection, or a new alarm format is a heartbeat alarm
   redesign, not the named label fix.

2. Malformed quantity treated as flat - ACCEPT. Fixed by making malformed
   non-null quantities fail the flat check. Current behavior keeps malformed
   quantity loud as `G: ? @ avg $31.33, STP $? STP missing`.

3. Broker-unreachable plus stopped-out file interaction - ACCEPT. Covered by
   `test_broker_unreachable_suppresses_stopped_out_calm_label`; broker errors
   suppress stopped-out relabeling and keep broker-unreachable output.

4. Cross-check stopped-out frontmatter against journal events - REJECT AS SCOPE
   EXPANSION. The kickoff allowed reading existing lifecycle metadata only as a
   display annotation and explicitly forbade detection, recovery, and
   reconciliation changes. A journal-history proof would create new detection
   behavior beyond P4-4.

5. Split or rename test and add new alarm assertions - ACCEPT MATERIAL PART.
   The named test intentionally contains both required MVP directions:
   flat-stopped-out renders calm, and held missing-stop still renders
   `STP $? STP missing`. The new alarm-format assertion requested by the
   reviewer is rejected with finding 1.

## Verification Evidence

- Red test first failed on the old `G: ? @ avg $?, STP $? STP missing` output.
- `python3 -m pytest tests/test_burn_in_heartbeat.py -q` passed, 18 tests.
- `python3 -m pytest tests/test_burn_in_heartbeat.py tests/test_invest_alert_lib.py -q` passed, 56 tests.
- `git diff --check` exited 0.

## Scope Preserved

- Display/rendering layer only.
- No engine code.
- No recovery code.
- No reconciliation code.
- No position detection change.
- No stop detection change.
- No broker connector change.
- No broker query change.
- No kill-switch change.
- No validator change.
- No strategy lifecycle change.
- No `.killed` mutation.
- No engine restart.
- No P4-1, P4-2, or P4-3 rework.
