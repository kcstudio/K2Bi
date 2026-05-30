# P4-3 Final Disposition

Review log: `.code-reviews/2026-05-30T23-37-14Z_db6758.log`

Verdict: NEEDS-ATTENTION, not cleared by reviewer

PM ruling: accepted under non-capital-path single-pass discipline. P4-3 remains
runbook-only. Material findings were fixed without expanding scope. Review was
not rerun by instruction.

## Dispositions

1. Exit `78` contract ambiguity - ACCEPT. Fixed by stating that
   `scripts/ssh-vps.sh` exits `78` when the local wrapper circuit is open, and
   that this is the wrapper exit code, not a remote command value printed in
   stdout. The test now requires that wording.

2. Hardcoded vault path in test - ACCEPT. Fixed by supporting
   `K2BI_VAULT_ROOT`, with the existing `~/Projects/K2Bi-Vault` path retained
   as the local default.

3. Position-dependent process filter - ACCEPT. Fixed by changing the process
   inspection command to user-first parsing: `ps -eo user=,...` with
   `awk '$1 == "k2bi"'`.

4. Static no-auto-kill validation weakness - ACCEPT MATERIAL PART. Fixed by
   narrowing shell-block validation to wrapper-only commands, requiring
   `bash -n` parse success, and retaining explicit forbidden-surface checks.
   Remaining theoretical obfuscation risk is accepted because P4-3 ships no
   executable cleanup path or diagnostic script.

5. Hung SSH handling missing - ACCEPT. Fixed with a 90-second attended stop
   rule: press Ctrl-C once, record `hung-ssh`, and stop. No timeout wrapper was
   added because P4-3 remains a runbook, not an implementation surface.

6. Active-session age heuristic undefined - ACCEPT. Fixed by defining high
   confidence stale as `notty` or `?`, `etimes > 300`, and older than the active
   operator `pts/` session.

## Verification Evidence

- Red test first: `python3 -m pytest tests/test_p4_3_ssh_stale_session_runbook.py`
  failed before the runbook existed.
- Focused P4-3 tests: `python3 -m pytest tests/test_p4_3_ssh_stale_session_runbook.py`
  passed, 2 tests.
- SSH discipline regression: `python3 -m pytest tests/test_engine_gateway_discipline.py`
  passed, 10 tests.
- SSH circuit breaker regression: `bash tests/ssh_circuit_breaker_test.sh`
  passed the default fast checks.
- Deploy coverage preflight: `python3 scripts/lib/deploy_config.py preflight`
  exited 0.
- Em-dash scan on changed files found no matches.

## Scope Preserved

- Runbook-only.
- No diagnostic script.
- No scheduler.
- No auto-kill.
- No SSH wrapper change.
- No broker query.
- No engine restart.
- No `.killed` mutation.
- No P4-4 merge.

Vault files are Syncthing-managed and outside git. The repo commit stages only
the P4-3 runbook acceptance test and this disposition note.
